from typing import Literal

from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    provider: str = "groq"
    model: str
    fallback_model: str
    base_url: str | None = None
    temperature: float = Field(ge=0.0, le=2.0, default=0.2)
    max_tokens: int = Field(gt=0, default=1000)


class RetrievalConfig(BaseModel):
    min_prs: int = Field(gt=0, default=3)
    max_prs: int = Field(gt=0, default=10)
    branch_prefix_match: bool = True


class RiskClassifierConfig(BaseModel):
    enabled: bool = True
    trivial_threshold: float = Field(ge=0.0, le=1.0, default=0.15)
    structural_weight: float = Field(ge=0.0, le=1.0, default=0.3)


class FailureHandlingConfig(BaseModel):
    max_retries: int = Field(ge=0, default=2)
    retry_backoff_seconds: float = Field(ge=0, default=2)


class EmbeddingConfig(BaseModel):
    provider: str = "local"
    model: str = "all-MiniLM-L6-v2"


class MergePreviewConfig(BaseModel):
    escalate_below_confidence: Literal["high", "medium"] = "high"
    blast_radius_depth: int = Field(gt=0, default=2)
    worktree_cleanup: bool = True


class LintReviewConfig(BaseModel):
    enabled: bool = True


class AgenticReviewConfig(BaseModel):
    enabled: bool = True
    max_context_tokens: int = Field(gt=0, default=6000)
    hops: int = Field(gt=0, default=2)
    sandboxed_timeout_seconds: int | None = Field(
        default=None,
        description="Overrides validation.ai.timeout_seconds specifically "
        "for `gitscribe review --sandboxed`. That path batches several "
        "files into one local-model call and can legitimately take much "
        "longer than a single-diff validation.ai review, so reusing the "
        "same budget for both is wrong. See "
        "core/analysis/rag.py:_sandboxed_ai_config.",
    )
    min_blast_radius_for_review: int | None = Field(
        default=None,
        description="Optional secondary gate: also escalate a file to the "
        "agentic pass if its max blast radius meets/exceeds this, even "
        "with no lint error findings. Off by default — a fixed threshold "
        "doesn't generalize across repos of different size/connectivity "
        "(e.g. one real repo measured had a MEDIAN blast radius of 5 "
        "across all functions, so a naive default of 5 gated nothing). "
        "If you enable this, measure your own repo's blast-radius "
        "distribution first and set it well above the median/p90, not a "
        "guessed number. Lint error findings remain the primary, "
        "repo-agnostic gate regardless of this setting.",
    )


class ReviewConfig(BaseModel):
    lint: LintReviewConfig = LintReviewConfig()
    agentic: AgenticReviewConfig = AgenticReviewConfig()


class PrePushHookConfig(BaseModel):
    block_on_risk: bool = False


class MergeCheckHookConfig(BaseModel):
    block_on_risk: bool = False


class PostMergeHookConfig(BaseModel):
    auto_tag: bool = False
    push_tag: bool = False


class CommitMsgHookConfig(BaseModel):
    enabled: bool = True


class HooksConfig(BaseModel):
    pre_push: PrePushHookConfig = PrePushHookConfig()
    merge_check: MergeCheckHookConfig = MergeCheckHookConfig()
    post_merge: PostMergeHookConfig = PostMergeHookConfig()
    commit_msg: CommitMsgHookConfig = CommitMsgHookConfig()


class GitScribeConfig(BaseModel):
    llm: LLMConfig
    retrieval: RetrievalConfig = RetrievalConfig()
    risk_classifier: RiskClassifierConfig = RiskClassifierConfig()
    failure_handling: FailureHandlingConfig = FailureHandlingConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    merge_preview: MergePreviewConfig = MergePreviewConfig()
    review: ReviewConfig = ReviewConfig()
    hooks: HooksConfig = HooksConfig()
    ignore_patterns: list[str] = Field(default_factory=list)

    def as_dict(self) -> dict:
        return self.model_dump()
