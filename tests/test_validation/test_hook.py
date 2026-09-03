from pathlib import Path

import pytest

from gitscribe.validation.hook import (
    HOOK_TEXT,
    install_pre_push_hook,
)


def test_hook_installation_is_idempotent(
    tmp_path: Path,
) -> None:
    git_hooks = (
        tmp_path
        / ".git"
        / "hooks"
    )

    git_hooks.mkdir(
        parents=True
    )

    first = install_pre_push_hook(
        str(tmp_path)
    )

    second = install_pre_push_hook(
        str(tmp_path)
    )

    hook = (
        git_hooks
        / "pre-push"
    )

    assert first == "installed"
    assert second == "already installed"
    assert hook.read_text(
        encoding="utf-8"
    ) == HOOK_TEXT


def test_unrelated_hook_is_never_overwritten(
    tmp_path: Path,
) -> None:
    git_hooks = (
        tmp_path
        / ".git"
        / "hooks"
    )

    git_hooks.mkdir(
        parents=True
    )

    hook = git_hooks / "pre-push"

    hook.write_text(
        "#!/bin/sh\n"
        "echo unrelated\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError):
        install_pre_push_hook(
            str(tmp_path)
        )

    assert "unrelated" in hook.read_text(
        encoding="utf-8"
    )


def test_existing_gitscribe_hook_is_upgraded(
    tmp_path: Path,
) -> None:
    git_hooks = (
        tmp_path
        / ".git"
        / "hooks"
    )

    git_hooks.mkdir(
        parents=True
    )

    hook = git_hooks / "pre-push"

    hook.write_text(
        "#!/bin/sh\n"
        "gitscribe pre-push\n",
        encoding="utf-8",
    )

    result = install_pre_push_hook(
        str(tmp_path)
    )

    assert result == "installed"
    assert (
        "gitscribe verify --pre-push"
        in hook.read_text(
            encoding="utf-8"
        )
    )
