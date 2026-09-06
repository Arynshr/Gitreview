from __future__ import annotations

import os
import stat
from pathlib import Path

# Single source of truth for both hook bodies. `gitscribe init` writes these
# same two commands (via the bundled hooks/pre-push.sh and
# hooks/pre-merge-commit.sh) so a fresh repo and a repo bootstrapped through
# `gitscribe verify --install-hook` end up byte-identical.
#
# `--sandboxed` is not optional here: validation.ai defaults to enabled with
# every file defaulting to "both" mode, so without --sandboxed the agentic
# pass is skipped-with-error on essentially every push/merge and
# fail_closed (default true) turns that into an unconditional FAIL. Every
# place that emits one of these commands - fresh install below, the append
# branch below, and the two bundled hooks/*.sh files - must include it.
PRE_PUSH_VERIFY_CALL = "gitscribe verify --pre-push --sandboxed"
PRE_MERGE_VERIFY_CALL = "gitscribe verify --pre-merge --sandboxed"

PRE_PUSH_HOOK_TEXT = f"""#!/bin/sh
set -eu
command -v gitscribe >/dev/null 2>&1 || exit 1
gitscribe pre-push
{PRE_PUSH_VERIFY_CALL}
exit $?
"""

PRE_MERGE_HOOK_TEXT = f"""#!/usr/bin/env bash
set -uo pipefail
command -v gitscribe >/dev/null 2>&1 || exit 0
[ -f .git/MERGE_HEAD ] || exit 0
gitscribe merge-check
{PRE_MERGE_VERIFY_CALL}
exit $?
"""

# Backward-compat alias for any existing callers/imports of the old name.
HOOK_TEXT = PRE_PUSH_HOOK_TEXT


def _install_hook(
    hook_name: str,
    hook_text: str,
    verify_call: str,
    legacy_markers: tuple[str, ...],
    repo_root: str,
) -> str:
    hook = Path(repo_root) / ".git" / "hooks" / hook_name
    hook.parent.mkdir(parents=True, exist_ok=True)

    if hook.exists():
        existing = hook.read_text(encoding="utf-8")

        if verify_call in existing:
            return "already installed"

        if not any(marker in existing for marker in legacy_markers):
            raise RuntimeError(
                f"existing {hook_name} hook was not created by GitScribe: "
                f"{hook}; refusing to overwrite it"
            )

        # Append rather than replace: an existing GitScribe call (the
        # legacy risk-classifier soft gate) must keep running alongside the
        # new validation gate, not be dropped by it. This must be the exact
        # same command as the fresh-install text above (including
        # --sandboxed) or the two install paths silently diverge again.
        #
        # Note: if this hook already contains an *older* `gitscribe verify`
        # line installed before --sandboxed was required, that stale line
        # runs first and (with `set -e`/`pipefail`) will abort the hook
        # before reaching the corrected line below. Delete the hook file
        # and re-run `gitscribe verify --install-hook` to get a clean
        # rewrite rather than relying on the append path in that case.
        hook.write_text(
            existing.rstrip("\n") + f"\n{verify_call}\n",
            encoding="utf-8",
        )
    else:
        hook.write_text(hook_text, encoding="utf-8")

    if os.name == "posix":
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    return "installed"


def install_pre_push_hook(repo_root: str = ".") -> str:
    return _install_hook(
        "pre-push",
        PRE_PUSH_HOOK_TEXT,
        verify_call=PRE_PUSH_VERIFY_CALL,
        legacy_markers=("gitscribe pre-push", "gitscribe verify"),
        repo_root=repo_root,
    )


def install_pre_merge_hook(repo_root: str = ".") -> str:
    """Mirrors install_pre_push_hook for the pre-merge-commit hook, so the
    validation layer's push and merge triggers are installed the same way
    and stay in sync (see PRE_MERGE_VERIFY_CALL above).
    """
    return _install_hook(
        "pre-merge-commit",
        PRE_MERGE_HOOK_TEXT,
        verify_call=PRE_MERGE_VERIFY_CALL,
        legacy_markers=("gitscribe merge-check", "gitscribe verify"),
        repo_root=repo_root,
    )
