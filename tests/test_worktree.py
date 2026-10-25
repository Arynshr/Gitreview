from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gitscribe.core.diff_parser import GitCommandError
from gitscribe.core.merge_preview.worktree import (
    WorktreeError,
    attempt_merge,
    disposable_worktree,
    merge_base,
    read_blob,
)


def _completed(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# --- disposable_worktree ---


def test_disposable_worktree_yields_path_and_cleans_up_on_success():
    with (
        patch("gitscribe.core.merge_preview.worktree._run_git") as mock_run_git,
        patch(
            "gitscribe.core.merge_preview.worktree.subprocess.run", return_value=_completed(0)
        ) as mock_run,
    ):
        with disposable_worktree("HEAD") as path:
            assert isinstance(path, Path)
        mock_run_git.assert_called_once()
        assert mock_run_git.call_args[0][0][:2] == ["worktree", "add"]
        # cleanup ran
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0][:3] == ["git", "worktree", "remove"]


def test_disposable_worktree_cleans_up_even_when_body_raises():
    with (
        patch("gitscribe.core.merge_preview.worktree._run_git"),
        patch(
            "gitscribe.core.merge_preview.worktree.subprocess.run", return_value=_completed(0)
        ) as mock_run,
    ):
        with pytest.raises(ValueError), disposable_worktree("HEAD"):
            raise ValueError("boom")
        mock_run.assert_called_once()  # cleanup still happened


def test_disposable_worktree_creation_failure_raises_worktree_error_and_no_leak():
    with (
        patch(
            "gitscribe.core.merge_preview.worktree._run_git",
            side_effect=GitCommandError("fatal: not a git repository"),
        ),
        patch("gitscribe.core.merge_preview.worktree.shutil.rmtree") as mock_rmtree,
    ):
        with pytest.raises(WorktreeError, match="could not create preview worktree"):
            with disposable_worktree("HEAD"):
                pytest.fail("body should never run when creation fails")
        mock_rmtree.assert_called_once()  # scratch dir cleaned up even though worktree add never ran


def test_disposable_worktree_cleanup_false_skips_removal():
    with (
        patch("gitscribe.core.merge_preview.worktree._run_git"),
        patch("gitscribe.core.merge_preview.worktree.subprocess.run") as mock_run,
    ):
        with disposable_worktree("HEAD", cleanup=False) as path:
            assert isinstance(path, Path)
        mock_run.assert_not_called()


def test_disposable_worktree_falls_back_to_rmtree_when_remove_fails():
    with (
        patch("gitscribe.core.merge_preview.worktree._run_git") as mock_run_git,
        patch(
            "gitscribe.core.merge_preview.worktree.subprocess.run",
            return_value=_completed(1, stderr="locked"),
        ),
        patch("gitscribe.core.merge_preview.worktree.shutil.rmtree") as mock_rmtree,
    ):
        with disposable_worktree("HEAD"):
            pass
        mock_rmtree.assert_called_once()
        # prune is called as part of the fallback path
        prune_calls = [c for c in mock_run_git.call_args_list if c[0][0] == ["worktree", "prune"]]
        assert len(prune_calls) == 1


# --- attempt_merge ---


def test_attempt_merge_clean_returns_empty_list():
    with patch("gitscribe.core.merge_preview.worktree.subprocess.run", return_value=_completed(0)):
        result = attempt_merge(Path("/fake/worktree"), "feature/x")
    assert result == []


def test_attempt_merge_conflict_returns_conflicted_files():
    with (
        patch("gitscribe.core.merge_preview.worktree.subprocess.run", return_value=_completed(1)),
        patch(
            "gitscribe.core.merge_preview.worktree.conflicted_files", return_value=["a.py", "b.py"]
        ),
    ):
        result = attempt_merge(Path("/fake/worktree"), "feature/x")
    assert result == ["a.py", "b.py"]


def test_attempt_merge_nonconflict_failure_raises_worktree_error():
    """Non-zero exit with NO conflicted files means the merge failed for
    an unrelated reason (bad ref, unrelated histories) - must be surfaced,
    not silently treated as a clean merge.
    """
    with (
        patch(
            "gitscribe.core.merge_preview.worktree.subprocess.run",
            return_value=_completed(128, stderr="fatal: 'nonexistent' does not point to a commit"),
        ),
        patch("gitscribe.core.merge_preview.worktree.conflicted_files", return_value=[]),
    ):
        with pytest.raises(WorktreeError, match="does not point to a commit"):
            attempt_merge(Path("/fake/worktree"), "nonexistent")


# --- merge_base / read_blob ---


def test_merge_base_returns_sha_on_success():
    with patch("gitscribe.core.merge_preview.worktree._run_git", return_value="abc123def\n"):
        assert merge_base("HEAD", "feature/x") == "abc123def"


def test_merge_base_returns_none_on_git_error():
    with patch(
        "gitscribe.core.merge_preview.worktree._run_git",
        side_effect=GitCommandError("no merge base"),
    ):
        assert merge_base("HEAD", "feature/x") is None


def test_read_blob_returns_content_on_success():
    with patch("gitscribe.core.merge_preview.worktree._run_git", return_value="file contents\n"):
        assert read_blob("abc123", "app.py") == "file contents\n"


def test_read_blob_returns_none_when_path_missing_at_ref():
    with patch(
        "gitscribe.core.merge_preview.worktree._run_git",
        side_effect=GitCommandError("fatal: path 'app.py' does not exist in 'abc123'"),
    ):
        assert read_blob("abc123", "app.py") is None
