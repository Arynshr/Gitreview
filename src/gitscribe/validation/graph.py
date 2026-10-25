"""
GitScribe's second, independent LangGraph StateGraph - validation.

Separate from `core/graph.py`'s PR-generation graph/state - different
state, no shared fields. The two are wired together via `core/graph.py`'s
`validate_node`, a thin translation boundary, not a merge.

Full execution DAG:

    resolve -> plan -> execute -> correlate -> policy -> END
           \\-> finalize_empty -> END (no-op change)

`plan` emits one job per applicable scanner (registry `supports()`/
`preflight()` checked there; Betterleaks always, never skipped). `execute`
runs those jobs concurrently, bounded by `deterministic.max_workers` -
they share no mutable state. `correlate` de-duplicates findings.

The agentic/AI review pass (`validation/ai.py`) is deliberately not wired
into this graph; it is slated for removal from the codebase.

This reuses the same functions `validation/orchestrator.py` used to own
directly (run_betterleaks, run_deterministic, aggregate, evaluate) instead
of reimplementing scanning - orchestrator.py delegates to this graph.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Literal

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from gitscribe.core.hooks import _validation_cache_key
from gitscribe.validation.aggregator import aggregate
from gitscribe.validation.analysis.models import AnalysisScope, ExecutionPlan, ScannerJob
from gitscribe.validation.analysis.registry import build_default_registry
from gitscribe.validation.deterministic import (
    BUNDLE,
    DETERMINISTIC_RUNNERS,
    DeterministicAnalysisError,
)
from gitscribe.validation.events import Emitter, emit
from gitscribe.validation.models import (
    AnalysisError,
    ChangeContext,
    Finding,
    ValidationRequest,
    ValidationResult,
)
from gitscribe.validation.policy import decide
from gitscribe.validation.resolver import resolve_change

logger = logging.getLogger("gitscribe.validation.graph")

Lifecycle = Literal[
    "RECEIVED",
    "RESOLVING",
    "PLANNING",
    "EXECUTING",
    "CORRELATING",
    "ASSESSING",
    "POLICY_EVALUATION",
    "PARTIAL_FAILURE",
    "RECOVERY",
    "COMPLETED",
]


class ValidationState(BaseModel):
    """Nodes communicate only through this typed state."""

    request: ValidationRequest
    change: ChangeContext | None = None
    scope: AnalysisScope | None = None
    plan: ExecutionPlan | None = None
    scanner_results: dict[str, list[Finding]] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    context: dict | None = None  # RepoContext - deferred, no real shape yet
    assessment: list[Finding] = Field(default_factory=list)
    policy_decision: Literal["PASS", "FAIL"] | None = None
    errors: list[AnalysisError] = Field(default_factory=list)
    diagnostics: dict[str, object] = Field(default_factory=dict)
    timings: dict[str, float] = Field(default_factory=dict)
    lifecycle: Lifecycle = "RECEIVED"
    result: ValidationResult | None = None


def derive_thread_id(cfg: dict, base: str, head: str, *extra: object) -> str:
    """Persistence key for a run (see `validation/checkpoint.py`). Delegates to the existing
    `_validation_cache_key()` so it reproduces the same inputs by
    construction, not by two implementations happening to agree.
    """
    return _validation_cache_key(cfg, base, head, *extra)


def resolve_node(state: ValidationState, cfg: dict) -> dict:
    """RESOLVER node. If the caller already supplied a resolved `change`
    (e.g. `orchestrator.validate_change()`, which is handed a
    `ChangeContext` its caller already built), that's used as-is - this
    avoids re-resolving the same diff from git a second time. Otherwise
    resolves fresh via `validation.resolver.resolve_change`, mirroring
    `validation/cli.py::_run_one`'s ignore-pattern handling.
    """
    if state.change is not None:
        return {"lifecycle": "RESOLVING"}

    req = state.request
    change = resolve_change(
        base=req.base_ref,
        head=req.head_ref,
        extra_ignore_patterns=cfg.get("ignore_patterns", []),
        paths=req.paths or None,
    )

    return {"change": change, "lifecycle": "RESOLVING"}


def route_after_resolve(state: ValidationState) -> str:
    if not (state.change and state.change.diff.strip()):
        return "empty"
    return "plan"


def finalize_empty_node(state: ValidationState) -> dict:
    result = ValidationResult(status="PASS", timings=state.timings)
    return {"result": result, "policy_decision": "PASS", "lifecycle": "COMPLETED"}


def plan_node(state: ValidationState, cfg: dict, emitter: Emitter | None = None) -> dict:
    """PLANNER: builds the `ExecutionPlan`. Betterleaks is always planned
    (§10.3, unconditional); the deterministic bundle is planned when enabled.
    The agentic/AI pass is removed from the pipeline, so per-file mode
    (`file_rules`, `requested_mode`) no longer excludes anything from the
    static scanners. `static_files` is stashed in `diagnostics` rather than
    growing `AnalysisScope`'s schema.
    """
    change = state.change
    assert change is not None

    static_files = list(change.files)  # no agentic pass: every file gets the static scanners

    registry = build_default_registry()
    errors = list(state.errors)
    health: dict[str, str | None] = {}
    jobs: list[ScannerJob] = []

    # (scanner, files, required, reason). Betterleaks is required: no
    # supports() check may drop it (§10.3).
    candidates: list[tuple[str, list[str], bool, str]] = [
        (
            "betterleaks",
            change.files,
            True,
            "mandatory secret scan, independent of mode/capability (§10.3)",
        )
    ]
    if cfg.get("deterministic", {}).get("enabled", True) and static_files:
        candidates += [(name, static_files, False, "static-mode files") for name in BUNDLE]

    for name, files, required, reason in candidates:
        reg = registry.get(name)  # None for the in-process path check
        scanner = reg.scanner if reg and reg.implemented else None
        category = scanner.category if scanner else "security"

        if scanner is not None:
            if not required and not scanner.supports(AnalysisScope(files=files)):
                continue

            problem = getattr(scanner, "preflight", lambda: None)()
            health[name] = problem

            if problem:
                logger.warning("scanner unavailable scanner=%s: %s", name, problem)
                errors.append(AnalysisError(source=name, message=problem))
                continue

        jobs.append(
            ScannerJob(
                job_id=name,
                scanner=name,
                category=category,
                files=files,
                required=required,
                reason=reason,
            )
        )

    emit(emitter, "plan.ready", jobs=[job.scanner for job in jobs])

    return {
        "scope": AnalysisScope(files=change.files, secret_scan_scope=change.files),
        "plan": ExecutionPlan(jobs=jobs),
        "errors": errors,
        "diagnostics": {
            **state.diagnostics,
            "static_files": static_files,
            "scanner_health": health,
        },
        "lifecycle": "PLANNING",
    }


async def execute_node(state: ValidationState, cfg: dict, emitter: Emitter | None = None) -> dict:
    """EXECUTOR: runs every planned job concurrently (`asyncio.to_thread`,
    since each is a blocking subprocess/file call), at most
    `deterministic.max_workers` at a time. A failing job becomes its own
    `AnalysisError`; it no longer discards the other jobs' findings.
    """
    change = state.change
    assert change is not None

    det_cfg = cfg.get("deterministic", {})
    timeout_seconds = det_cfg.get("timeout_seconds", 60)
    semaphore = asyncio.Semaphore(int(det_cfg.get("max_workers", 4)))
    jobs = state.plan.jobs if state.plan else []

    async def _run(job: ScannerJob) -> tuple[str, list[Finding], AnalysisError | None, float]:
        job_context = change.model_copy(update={"files": job.files})

        async with semaphore:
            started = time.perf_counter()
            emit(emitter, "scanner.started", scanner=job.scanner)
            try:
                found = await asyncio.to_thread(
                    DETERMINISTIC_RUNNERS[job.scanner], job_context, timeout_seconds, det_cfg
                )
                return job.scanner, found, None, time.perf_counter() - started
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, DeterministicAnalysisError)
                    else f"{type(exc).__name__}: {exc}"
                )
                logger.warning("scanner job failed scanner=%s: %s", job.scanner, message)
                error = AnalysisError(source=job.scanner, message=message)
                return job.scanner, [], error, time.perf_counter() - started

    findings: list[Finding] = []
    errors = list(state.errors)
    scanner_results: dict[str, list[Finding]] = dict(state.scanner_results)
    timings = dict(state.timings)

    for name, result, error, duration in await asyncio.gather(*(_run(job) for job in jobs)):
        scanner_results[name] = result
        findings.extend(result)
        if error:
            errors.append(error)
        timings[f"{name}_s"] = duration
        emit(
            emitter,
            "scanner.finished",
            scanner=name,
            findings=len(result),
            seconds=round(duration, 3),
            error=error.message if error else None,
        )
        for f in result:
            emit(
                emitter,
                "finding.found",
                scanner=name,
                severity=f.severity,
                category=f.category,
                file=f.file,
                line=f.line,
                message=f.message,
            )

    return {
        "scanner_results": scanner_results,
        "findings": findings,
        "errors": errors,
        "timings": timings,
        "lifecycle": "EXECUTING",
    }


def correlate_node(state: ValidationState) -> dict:
    """CORRELATOR: dedups/merges findings from the deterministic phase.
    Re-run in `policy_node` - `aggregate()` is
    safe to call twice, it just re-merges already-merged entries.
    """
    return {"findings": aggregate(state.findings), "lifecycle": "CORRELATING"}


def _report_diagnostics(state: ValidationState, gate_reason: str = "") -> dict[str, object]:
    return {
        "scanner_health": state.diagnostics.get("scanner_health", {}),
        "jobs": [job.job_id for job in state.plan.jobs] if state.plan else [],
        "gate_reason": gate_reason,
    }


def policy_node(state: ValidationState, cfg: dict, emitter: Emitter | None = None) -> dict:
    """POLICY_EVALUATION: final aggregate + gate decision, per
    `validation.policy.evaluate`.
    """
    findings = aggregate(state.findings)
    diff = state.change.diff if state.change else ""

    passed, reason = decide(findings, cfg, errors=bool(state.errors), diff=diff)
    status: Literal["PASS", "FAIL"] = "PASS" if passed else "FAIL"

    result = ValidationResult(
        status=status,
        findings=findings,
        errors=state.errors,
        timings=state.timings,
        diagnostics=_report_diagnostics(state, reason),
    )

    severities: dict[str, int] = {}
    for f in findings:
        severities[f.severity] = severities.get(f.severity, 0) + 1
    emit(
        emitter,
        "gate.decision",
        status=status,
        reason=reason,
        findings=len(findings),
        errors=len(state.errors),
        severities=severities,
    )

    return {
        "findings": findings,
        "policy_decision": status,
        "result": result,
        "lifecycle": "COMPLETED",
    }


def build_validation_graph(cfg: dict, emitter: Emitter | None = None):
    """`cfg` is bound into nodes via `functools.partial`, mirroring
    `core/graph.py::build_graph(cfg)`'s own pattern.
    """
    g = StateGraph(ValidationState)

    g.add_node("resolve", functools.partial(resolve_node, cfg=cfg))
    g.add_node("plan", functools.partial(plan_node, cfg=cfg, emitter=emitter))
    g.add_node("execute", functools.partial(execute_node, cfg=cfg, emitter=emitter))
    g.add_node("correlate", correlate_node)
    g.add_node("policy", functools.partial(policy_node, cfg=cfg, emitter=emitter))
    g.add_node("finalize_empty", finalize_empty_node)

    g.set_entry_point("resolve")
    g.add_conditional_edges(
        "resolve", route_after_resolve, {"empty": "finalize_empty", "plan": "plan"}
    )
    g.add_edge("plan", "execute")
    g.add_edge("execute", "correlate")
    g.add_edge("correlate", "policy")
    g.add_edge("policy", END)
    g.add_edge("finalize_empty", END)

    return g.compile()


async def _ainvoke_validation(
    request: ValidationRequest,
    cfg: dict,
    change: ChangeContext | None = None,
    emitter: Emitter | None = None,
) -> ValidationState:
    graph = build_validation_graph(cfg, emitter)
    raw = await graph.ainvoke(ValidationState(request=request, change=change))

    return ValidationState.model_validate(raw)


def run_validation_sync(
    request: ValidationRequest,
    cfg: dict,
    change: ChangeContext | None = None,
    emitter: Emitter | None = None,
) -> ValidationState:
    """The one blocking entrypoint a synchronous caller may use -
    `asyncio.run()` appears only here. `change`, if the caller already
    has a resolved `ChangeContext`, skips re-resolving it in
    `resolve_node`.
    """
    return asyncio.run(_ainvoke_validation(request, cfg, change=change, emitter=emitter))
