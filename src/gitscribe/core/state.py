"""
Shared state object passed between all LangGraph nodes.
Maps directly to the README's Context Management Strategy table.
"""
from typing import Literal

from pydantic import BaseModel, Field


class RetrievedPR(BaseModel):
    id: int
    branch: str
    title: str
    body: str


class GitScribeState(BaseModel):
    #  inputs
    branch_name: str = ""
    base_ref: str = "origin/main"
    head_ref: str = "HEAD"
    raw_diff: str = ""
    commit_messages: list[str] = Field(default_factory=list)
    style: str = "default"

    #  diff intelligence (deterministic)
    files_changed: list[str] = Field(default_factory=list)
    change_summary: list[str] = Field(default_factory=list)

    #  validation (bridges to validation/graph.py's own DAG/state - see
    #  core/validation_bridge.py; only this summary crosses the boundary,
    #  never a raw ValidationState/ValidationResult object)
    validation_status: Literal["PASS", "FAIL", "SKIPPED", "ERROR"] | None = None
    validation_findings_count: int = 0
    validation_summary: str = ""

    #  risk classification (agentic node)
    risk_score: float = 1.0                # 0.0 (trivial) - 1.0 (critical)
    risk_reasoning: str = ""
    skip_generation: bool = False

    #  retrieval (agentic node)
    retrieved_prs: list[RetrievedPR] = Field(default_factory=list)
    retrieval_depth_used: int = 0
    retrieval_stopped_reason: str = ""

    #  generation (deterministic chain)
    prompt: str = ""
    pr_title: str = ""
    pr_body: str = ""

    #  failure handling (agentic router)
    attempt_count: int = 0
    last_error: str | None = None
    failure_type: Literal["rate_limit", "bad_output", "timeout", "validation_blocked", "none"] | None = None
    fallback_used: bool = False

    # output
    final_output: str = ""
    status: Literal["pending", "success", "skipped", "failed"] = "pending"
