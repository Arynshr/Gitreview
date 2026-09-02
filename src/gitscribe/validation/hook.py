from __future__ import annotations

import os
import stat
from pathlib import Path


HOOK_TEXT = """#!/bin/sh
set -eu
command -v gitscribe >/dev/null 2>&1 || exit 1
gitscribe verify --pre-push
exit $?
"""


def install_pre_push_hook(
    repo_root: str = ".",
) -> str:
    hook = (
        Path(repo_root)
        / ".git"
        / "hooks"
        / "pre-push"
    )

    hook.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if hook.exists():
        existing = hook.read_text(
            encoding="utf-8"
        )

        if (
            "gitscribe verify --pre-push"
            in existing
        ):
            return "already installed"

        if "gitscribe pre-push" not in existing:
            raise RuntimeError(
                f"existing pre-push hook was not "
                f"created by GitScribe: {hook}; "
                "refusing to overwrite it"
            )

        hook.write_text(
            existing.replace(
                "gitscribe pre-push",
                "gitscribe verify --pre-push",
            ),
            encoding="utf-8",
        )
    else:
        hook.write_text(
            HOOK_TEXT,
            encoding="utf-8",
        )

    if os.name == "posix":
        hook.chmod(
            hook.stat().st_mode
            | stat.S_IEXEC
        )

    return "installed"