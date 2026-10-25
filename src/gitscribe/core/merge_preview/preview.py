"""
Public entry point for the merge-preview feature. Wires together
worktree, hunk_parser, context, and resolver
"""

from __future__ import annotations

import logging

from gitscribe.core.merge_preview.context import gather_context
from gitscribe.core.merge_preview.hunk_parser import hunks_for_file
from gitscribe.core.merge_preview.models import FileReport, MergePreviewReport
from gitscribe.core.merge_preview.resolver import resolve_file
from gitscribe.core.merge_preview.worktree import WorktreeError, attempt_merge, disposable_worktree

logger = logging.getLogger("gitscribe.merge_preview")

__all__ = ["FileReport", "MergePreviewReport", "WorktreeError", "run_merge_preview"]


def run_merge_preview(
    cfg: dict, ours_branch: str, theirs_branch: str, base_ref: str = "HEAD"
) -> MergePreviewReport:
    """Speculatively merges `theirs_branch` into `base_ref` inside a
    disposable worktree, and — if it conflicts — proposes a grounded
    resolution per hunk. Never touches the caller's real working tree,
    index, or branches; the worktree is always removed before returning
    or raising.
    """
    logger.info("merge preview starting: %s -> %s (base=%s)", theirs_branch, ours_branch, base_ref)
    cleanup = cfg.get("merge_preview", {}).get("worktree_cleanup", True)

    with disposable_worktree(base_ref, cleanup=cleanup) as worktree_path:
        conflicted = attempt_merge(worktree_path, theirs_branch)

        if not conflicted:
            logger.info(
                "merge preview clean: no conflicts between %s and %s", ours_branch, theirs_branch
            )
            return MergePreviewReport(
                ours_branch=ours_branch, theirs_branch=theirs_branch, clean=True
            )

        file_reports: list[FileReport] = []
        for file in conflicted:
            hunks = hunks_for_file(worktree_path, file, base_ref, theirs_branch)
            if not hunks:
                # Binary file conflict, or a file we couldn't read/parse -
                # still worth surfacing to the user, just with no proposed
                # resolution to show.
                logger.info(
                    "no parseable text hunks in conflicted file %s (binary or unreadable)", file
                )
                file_reports.append(FileReport(file=file, resolutions=[]))
                continue

            contexts = [gather_context(h, cfg, ours_branch, theirs_branch) for h in hunks]
            file_reports.append(resolve_file(contexts, cfg))

    report = MergePreviewReport(
        ours_branch=ours_branch, theirs_branch=theirs_branch, clean=False, files=file_reports
    )
    logger.info(
        "merge preview finished: %d file(s), %d hunk(s), %d high-confidence",
        len(report.files),
        report.total_hunks,
        report.total_safe,
    )
    return report
