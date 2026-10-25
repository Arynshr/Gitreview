"""
Runs a speculative merge in a disposable git worktree so `gitscribe
merge-preview` can inspect conflicts before the user's real working tree
or index is touched.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from gitscribe.core.diff_parser import GitCommandError, _run_git
from gitscribe.core.hooks import conflicted_files

logger = logging.getLogger("gitscribe.merge_preview")


class WorktreeError(RuntimeError):
    """Raised when worktree setup/teardown or the merge attempt itself
    fails for a reason other than a conflict (bad ref, dirty base, etc.).
    """


@contextmanager
def disposable_worktree(base_ref: str = "HEAD", cleanup: bool = True):
    """Creates a detached worktree at `base_ref` in a temp directory and
    yields its path. Removes it on exit — success, conflict, or exception.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="gitscribe-merge-preview-"))
    logger.debug("creating disposable worktree at %s (base=%s)", tmp_dir, base_ref)
    try:
        _run_git(["worktree", "add", "--detach", str(tmp_dir), base_ref])
    except GitCommandError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise WorktreeError(f"could not create preview worktree: {e}") from e

    try:
        yield tmp_dir
    finally:
        if not cleanup:
            logger.info("worktree_cleanup=false - leaving preview worktree at %s", tmp_dir)
            return
        logger.debug("removing disposable worktree at %s", tmp_dir)
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(tmp_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Best-effort fallback: worktree metadata removal failed, but
            # don't leave a stray checkout on disk if we can help it.
            logger.warning(
                "`git worktree remove` failed (%s) - falling back to rmtree",
                result.stderr.strip(),
            )
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _run_git(["worktree", "prune"])


def attempt_merge(worktree_path: Path, branch: str) -> list[str]:
    """Runs `git merge --no-commit --no-ff <branch>` inside the worktree.
    Returns the list of conflicted files (empty if the merge was clean).
    A non-zero exit with no conflicted files means the merge failed for
    an unrelated reason (unknown ref, unrelated histories, etc.) and is
    raised rather than silently treated as "clean".
    """
    logger.info("attempting merge of %r into worktree %s", branch, worktree_path)
    result = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", branch],
        capture_output=True,
        text=True,
        cwd=worktree_path,
    )
    if result.returncode == 0:
        logger.info("merge of %r is clean, no conflicts", branch)
        return []

    conflicts = conflicted_files(cwd=worktree_path)
    if conflicts:
        logger.info("merge of %r produced %d conflicted file(s)", branch, len(conflicts))
        return conflicts

    raise WorktreeError(
        f"`git merge {branch}` failed for a reason other than a conflict: "
        f"{result.stderr.strip() or result.stdout.strip() or 'no error output'}"
    )


def merge_base(base_ref: str, branch: str) -> str | None:
    """Best-effort merge-base lookup for reconstructing each hunk's base
    text. Returns None (rather than raising) when it can't be determined —
    unrelated histories, shallow clone, etc. — since base text is a nice-
    to-have for context, not required for a resolution attempt.
    """
    try:
        return _run_git(["merge-base", base_ref, branch]).strip() or None
    except GitCommandError:
        logger.debug("could not determine merge-base(%s, %s)", base_ref, branch, exc_info=True)
        return None


def read_blob(ref: str, path: str, cwd: str | Path | None = None) -> str | None:
    """`git show <ref>:<path>`, returning None instead of raising when the
    path doesn't exist at that ref (added in one branch only, etc.).
    """
    try:
        return _run_git(["show", f"{ref}:{path}"], cwd=cwd)
    except GitCommandError:
        return None
