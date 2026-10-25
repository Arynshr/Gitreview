"""
core/merge_preview/models.py
Typed request/response shapes for the merge-preview feature, consistent
with index_store.py's and rag.py's pattern (Pydantic across every public
boundary, no raw dicts).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]


class ConflictHunk(BaseModel):
    """One `<<<<<<<`/`=======`/`>>>>>>>` block inside one conflicted file."""

    file: str
    hunk_index: int  # 0-based position of this hunk within the file
    start_line: int  # line in the conflicted working-tree file where <<<<<<< appears
    end_line: int  # line where >>>>>>> appears
    ours_label: str  # ref/branch name from the <<<<<<< marker
    theirs_label: str  # ref/branch name from the >>>>>>> marker
    ours_text: str
    theirs_text: str
    base_text: str | None = None  # None when the merge-base blob is unavailable


class BranchIntent(BaseModel):
    """Recovered intent for a branch, sourced from GitScribe's own PR
    memory (memory.py) rather than the diff alone — this is the signal a
    plain diff-based conflict resolver doesn't have access to.
    """

    branch: str
    pr_titles: list[str] = Field(default_factory=list)
    pr_summaries: list[str] = Field(default_factory=list)

    @property
    def has_history(self) -> bool:
        return bool(self.pr_titles)


class HunkContext(BaseModel):
    """Everything gathered for one hunk before it's handed to the LLM."""

    hunk: ConflictHunk
    symbol_name: str | None = None
    blast_radius_summary: list[str] = Field(default_factory=list)
    ours_intent: BranchIntent
    theirs_intent: BranchIntent

    def as_prompt_block(self) -> str:
        """Plain-text rendering for prompt injection — mirrors
        rag.RAGContext.as_prompt_block()'s "no framework coupling" pattern.
        """
        lines = [f"File: {self.hunk.file} (lines {self.hunk.start_line}-{self.hunk.end_line})"]
        if self.symbol_name:
            lines.append(f"Enclosing symbol: {self.symbol_name}")
        if self.blast_radius_summary:
            lines.append("Blast radius: " + "; ".join(self.blast_radius_summary))
        if self.ours_intent.has_history:
            lines.append(
                f"'{self.ours_intent.branch}' branch intent (past PRs): "
                + "; ".join(self.ours_intent.pr_titles)
            )
        if self.theirs_intent.has_history:
            lines.append(
                f"'{self.theirs_intent.branch}' branch intent (past PRs): "
                + "; ".join(self.theirs_intent.pr_titles)
            )
        return "\n".join(lines)


class HunkResolution(BaseModel):
    hunk_index: int
    resolved_text: str = Field(
        description="The proposed merged content for this hunk, no conflict markers"
    )
    rationale: str = Field(description="Why this resolution preserves both branches' intent")
    confidence: Confidence
    escalated: bool = (
        False  # True if this result came from per-hunk re-resolution, not the batch pass
    )


class FileReport(BaseModel):
    file: str
    resolutions: list[HunkResolution]
    batch_call_count: int = 0
    escalation_call_count: int = 0

    @property
    def safe_count(self) -> int:
        return sum(1 for r in self.resolutions if r.confidence == "high")

    @property
    def needs_manual_review(self) -> list[HunkResolution]:
        return [r for r in self.resolutions if r.confidence != "high"]


class MergePreviewReport(BaseModel):
    ours_branch: str
    theirs_branch: str
    clean: bool  # True if the merge produced no conflicts
    files: list[FileReport] = Field(default_factory=list)

    @property
    def total_hunks(self) -> int:
        return sum(len(f.resolutions) for f in self.files)

    @property
    def total_safe(self) -> int:
        return sum(f.safe_count for f in self.files)
