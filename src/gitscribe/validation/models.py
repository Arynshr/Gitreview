from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]
Confidence = Literal["high", "medium", "low"]
FindingSource = Literal["deterministic", "ai"]


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