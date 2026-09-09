from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]
Confidence = Literal["high", "medium", "low"]
FindingSource = Literal["deterministic", "ai"]
ValidationTrigger = Literal["cli", "pre-push", "pre-merge", "ci", "pr", "manual"]


class ValidationRequest(BaseModel):
    """Per validation_layer.md §5.1: the one typed request object every
    validation entrypoint (`verify`, a hook, CI) builds, so no scanner or
    graph node ever receives raw CLI arguments directly. `request_id` is
    caller-assigned (e.g. a uuid4 hex from `cli.py`) so a single run can
    be traced end to end across lifecycle states/diagnostics.
    """

    request_id: str
    repo_root: str
    base_ref: str
    head_ref: str
    trigger: ValidationTrigger = "cli"
    paths: list[str] = Field(default_factory=list)
    requested_mode: str | None = None
    policy_name: str = "default"
    policy_version: str = "1"
    output_formats: list[str] = Field(default_factory=lambda: ["cli"])
    sandboxed: bool = False
    dry_run: bool = False


class Finding(BaseModel):
    id: str
    source: FindingSource
    category: str
    rule_id: str
    severity: Severity
    confidence: Confidence
    file: str | None = None
    line: int | None = None
    message: str
    evidence: str
    remediation: str
    sources: list[str] = Field(default_factory=list)


class ChangeContext(BaseModel):
    base: str
    head: str
    diff: str
    files: list[str]


class AnalysisError(BaseModel):
    source: str
    message: str


class ValidationResult(BaseModel):
    status: Literal["PASS", "FAIL"]
    findings: list[Finding] = Field(default_factory=list)
    errors: list[AnalysisError] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "PASS"
