from __future__ import annotations

import os
import stat
from pathlib import Path

# Version marker embedded as a comment in installed hooks. Bump this
# whenever PRE_PUSH_HOOK_TEXT/PRE_MERGE_HOOK_TEXT's *content* changes in a
# way that matters (e.g. the --sandboxed drift fixed previously). Without
# a marker, an already-installed hook's text is frozen forever - the
# append-idempotency check only ever looks for "is our command present at
# all", so a future template change would silently never reach existing
# installs. With it, _install_hook() can tell "installed, current" apart
# from "installed, stale" and re-append the corrected command instead of
# treating the stale hook as done.
HOOK_VERSION = 2

PRE_PUSH_VERIFY_CALL = "gitscribe verify --pre-push --sandboxed"
PRE_MERGE_VERIFY_CALL = "gitscribe verify --pre-merge --sandboxed"

_VERSION_MARKER = "# gitscribe-hook-version: {version}"


def _version_marker(version: int = HOOK_VERSION) -> str:
    return _VERSION_MARKER.format(version=version)


PRE_PUSH_HOOK_TEXT = f"""#!/bin/sh
{_version_marker()}
set -eu
command -v gitscribe >/dev/null 2>&1 || exit 1
gitscribe pre-push
{PRE_PUSH_VERIFY_CALL}
exit $?
"""

PRE_MERGE_HOOK_TEXT = f"""#!/usr/bin/env bash
{_version_marker()}
set -uo pipefail
command -v gitscribe >/dev/null 2>&1 || exit 0
[ -f .git/MERGE_HEAD ] || exit 0
gitscribe merge-check
{PRE_MERGE_VERIFY_CALL}
exit $?
"""

# Backward-compat alias for any existing callers/imports of the old name.
HOOK_TEXT = PRE_PUSH_HOOK_TEXT


def _installed_version(existing: str) -> int | None:
    """Returns the hook-version marker found in an installed hook's text,
    or None if the hook predates versioning entirely (installed before
    this marker existed) or wasn't installed by GitScribe.
    """
    for line in existing.splitlines():
        line = line.strip()
        if line.startswith("# gitscribe-hook-version:"):
            try:
                return int(line.rsplit(":", 1)[1].strip())
            except ValueError:
                return None
    return None


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
        installed_version = _installed_version(existing)

        if verify_call in existing and installed_version == HOOK_VERSION:
            return "already installed (current)"

        if not any(marker in existing for marker in legacy_markers):
            raise RuntimeError(
                f"existing {hook_name} hook was not created by GitScribe: "
                f"{hook}; refusing to overwrite it"
            )

        if verify_call in existing and installed_version is None:
            # Correct command, but predates version-marker tracking -
            # backfill the marker without re-appending a duplicate call.
            hook.write_text(
                existing.rstrip("\n") + f"\n{_version_marker()}\n",
                encoding="utf-8",
            )
        else:
            # Append rather than replace: an existing GitScribe call (the
            # legacy risk-classifier soft gate) must keep running
            # alongside the new validation gate, not be dropped by it.
            # Includes the version marker so a *future* template change
            # can detect this install as stale and upgrade it the same
            # way, instead of the drift this replaced silently recurring.
            #
            # Caveat: if the existing content has an older, differently
            # *worded* verify call (e.g. missing --sandboxed from before
            # that was fixed), that stale line runs first and - under
            # `set -e`/`pipefail` - can abort the hook before reaching the
            # newly appended line. Delete the hook file and re-run
            # `gitscribe verify --install-hook` for a clean rewrite in
            # that case rather than relying on this append path.
            hook.write_text(
                existing.rstrip("\n") + f"\n{verify_call}\n{_version_marker()}\n",
                encoding="utf-8",
            )
    else:
        hook.write_text(hook_text, encoding="utf-8")

    if os.name == "posix":
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    return "installed"


def _uninstall_hook(
    hook_name: str,
    verify_call: str,
    repo_root: str,
) -> str:
    """Removes only the GitScribe-authored line(s) this module added,
    preserving anything else in the hook file (e.g. the legacy
    `gitscribe pre-push`/`gitscribe merge-check` calls installed by
    `gitscribe init`, or content from a hook a human hand-edited further).
    If nothing but GitScribe-authored content remains, removes the file
    entirely rather than leaving a hook that's just a shebang and `exit
    $?` with nothing in between.
    """
    hook = Path(repo_root) / ".git" / "hooks" / hook_name

    if not hook.exists():
        return "not installed"

    lines = hook.read_text(encoding="utf-8").splitlines()
    kept = [
        line
        for line in lines
        if line.strip() != verify_call
        and not line.strip().startswith("# gitscribe-hook-version:")
    ]

    if verify_call not in "\n".join(lines):
        return "not installed"

    meaningful = [
        line
        for line in kept
        if line.strip()
        and not line.strip().startswith("#!")
        and line.strip() not in ("exit $?", "set -eu", "set -uo pipefail", "[ -f .git/MERGE_HEAD ] || exit 0")
        and not line.strip().startswith("command -v gitscribe")
    ]

    if not meaningful:
        hook.unlink()
        return "removed (hook file deleted, nothing else was in it)"

    hook.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return "removed (other hook content preserved)"


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


def uninstall_pre_push_hook(repo_root: str = ".") -> str:
    return _uninstall_hook("pre-push", PRE_PUSH_VERIFY_CALL, repo_root=repo_root)


def uninstall_pre_merge_hook(repo_root: str = ".") -> str:
    return _uninstall_hook("pre-merge-commit", PRE_MERGE_VERIFY_CALL, repo_root=repo_root)
