from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AnalysisScope(BaseModel):
    """What is actually being analyzed for one validation run.

    Per validation_layer.md section 5.3: describes the scope a scanner
    adapter operates on. No scanner should ever receive a raw
    ChangeContext or CLI arguments directly (section 5.1) - callers build
    an AnalysisScope from a resolved ChangeContext and hand that to
    adapters instead. Every field defaults to empty so existing callers
    (see validation.deterministic.run_ruff's migration wrapper) can build
    a minimal scope (just `files`) without needing a full planner yet -
    the planner itself is a later step in the Required Implementation
    Sequence, not part of this change.
    """

    language: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    file_types: list[str] = Field(default_factory=list)
    changed_lines: dict[str, list[int]] = Field(default_factory=dict)
    symbols: list[int] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] | None = None
    dependency_files: list[str] = Field(default_factory=list)
    iac_files: list[str] = Field(default_factory=list)
    container_files: list[str] = Field(default_factory=list)
    secret_scan_scope: list[str] = Field(default_factory=list)


class ScannerCommand(BaseModel):
    """What a scanner adapter wants executed, built by
    Scanner.build_command(). The ProcessRunner (runner.py) owns actually
    running it - see validation_layer.md section 6: "The generic
    subprocess runner owns: subprocess creation, timeout, output limits,
    process-group termination, environment isolation, exit-code
    interpretation, diagnostics, telemetry. Scanner adapters must not
    duplicate subprocess execution logic."
    """

    args: list[str]
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 60
    max_output_bytes: int = 10_000_000
    network_required: bool = False


class ProcessResult(BaseModel):
    """Raw outcome of running one ScannerCommand. Scanner.parse()
    interprets this into Findings; the runner itself never interprets
    scanner-specific exit codes or output."""

    args: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    duration_seconds: float = 0.0


class ScannerJob(BaseModel):
    """One ordered entry in an ExecutionPlan - validation_layer.md
    section 5.4. `command` is populated once the planner has asked the
    owning Scanner to build it; a job with `command=None` was planned but
    not yet (or could not be) built (e.g. `supports()` returned False for
    this scope, or preflight() reported the tool unavailable).
    """

    job_id: str
    scanner: str
    category: str
    files: list[str] = Field(default_factory=list)
    working_directory: str | None = None
    command: ScannerCommand | None = None
    required: bool = True
    reason: str = ""
    ruleset: str | None = None


class ExecutionPlan(BaseModel):
    """The explicit `exec()` plan from the target architecture
    (validation_layer.md section 4/5.4) - an ordered list of scanner
    jobs. Building one (the "planner") is a later step in the Required
    Implementation Sequence; this model is defined now so the contract
    exists ahead of that, per section 5's "all cross-layer data contracts
    must be Pydantic models".
    """

    jobs: list[ScannerJob] = Field(default_factory=list)    
