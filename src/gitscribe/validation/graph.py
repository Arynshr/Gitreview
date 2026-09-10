"""
GitScribe's second, independent LangGraph StateGraph - validation.

Separate from `core/graph.py`'s PR-generation graph/state - different
state, no shared fields. The two are wired together via `core/graph.py`'s
`validate_node`, a thin translation boundary, not a merge.

Full execution DAG:

    resolve -> plan -> execute -> correlate -> [assess] -> policy -> END
           \\-> finalize_empty -> END (no-op change)

`execute` runs the mandatory secret scan (Gitleaks) and the deterministic
bundle (ruff/bandit/pip-audit via `run_deterministic`) concurrently -
they have no data dependency. `correlate` aggregates and routes: `assess`
(the AI pass) only runs when there are agentic-mode files and AI is
enabled/forced, since it depends on the deterministic findings gathered
so far for de-dup context - that dependency is why it can't also run
concurrently with `execute`.

This reuses the exact same functions `validation/orchestrator.py` used to
own directly (run_gitleaks, run_deterministic, review_locally_async,
aggregate, evaluate) instead of reimplementing scanning - see
orchestrator.py, which now delegates to this graph rather than
duplicating the pipeline.
"""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from gitscribe.core.diff_parser import get_raw_diff
from gitscribe.core.hooks import _clean_working_tree, _validation_cache_key
from gitscribe.validation.aggregator import aggregate
from gitscribe.validation.ai import AIReviewError, review_locally_async
from gitscribe.validation.analysis.models import AnalysisScope, ExecutionPlan, ScannerJob
from gitscribe.validation.deterministic import (
    DeterministicAnalysisError,
    run_deterministic,
    run_gitleaks,
)
from gitscribe.validation.mode import DEFAULT_MODE, resolve_file_modes, split_by_mode
from gitscribe.validation.models import (
    AnalysisError,
    ChangeContext,
    Finding,
    ValidationRequest,
    ValidationResult,
)
from gitscribe.validation.policy import evaluate
from gitscribe.validation.resolver import resolve_change

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


class DirtyWorkingTreeError(RuntimeError):
    """Raised by `dirty_tree_gate()` - a checkpointer has no native
    concept of git working-tree state, so this is an explicit check.
    """


def dirty_tree_gate() -> None:
    """Call before any checkpoint read/write. Delegates to
    `core.hooks._clean_working_tree()`.
    """
    if not _clean_working_tree():
        raise DirtyWorkingTreeError(
            "working tree is dirty; refusing to read or write a "
            "validation checkpoint against it"
        )


def derive_thread_id(cfg: dict, base: str, head: str, *extra: object) -> str:
    """Checkpoint `thread_id` for a run. Delegates to the existing
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


def plan_node(state: ValidationState, cfg: dict) -> dict:
    """PLANNER: resolves per-file mode (static/agentic/both, or
    `request.requested_mode` overriding all files) and builds the
    `ExecutionPlan`. Gitleaks is always planned (§10.3, unconditional);
    the deterministic bundle is planned only when enabled and there are
    static-mode files. static_files/ai_files are stashed in
    `diagnostics` rather than growing `AnalysisScope`'s schema for a
    split that's internal bookkeeping, not part of the scope contract.
    """
    change = state.change
    assert change is not None

    if state.request.requested_mode is not None:
        modes = dict.fromkeys(change.files, state.request.requested_mode)
    else:
        modes = resolve_file_modes(
            change.files,
            cfg.get("file_rules", []),
            default_mode=DEFAULT_MODE,
        )

    static_files, ai_files = split_by_mode(modes)

    jobs = [
        ScannerJob(
            job_id="gitleaks",
            scanner="gitleaks",
            category="security",
            files=change.files,
            required=True,
            reason="mandatory secret scan, independent of mode/capability (§10.3)",
        )
    ]

    if cfg.get("deterministic", {}).get("enabled", True) and static_files:
        jobs.append(
            ScannerJob(
                job_id="deterministic",
                scanner="deterministic-bundle",
                category="quality",
                files=static_files,
                required=False,
                reason="static-mode files",
            )
        )

    return {
        "scope": AnalysisScope(files=change.files),
        "plan": ExecutionPlan(jobs=jobs),
        "diagnostics": {**state.diagnostics, "static_files": static_files, "ai_files": ai_files},
        "lifecycle": "PLANNING",
    }


async def execute_node(state: ValidationState, cfg: dict) -> dict:
    """EXECUTOR: runs the mandatory secret scan and the deterministic
    bundle concurrently (`asyncio.to_thread`, since both are blocking
    subprocess calls) - they're independent, unlike the AI pass.
    """
    change = state.change
    assert change is not None

    static_files = state.diagnostics.get("static_files", [])
    timeout_seconds = cfg.get("deterministic", {}).get("timeout_seconds", 60)

    async def _gitleaks() -> tuple[str, list[Finding], AnalysisError | None, float]:
        started = time.perf_counter()
        try:
            findings = await asyncio.to_thread(run_gitleaks, change, timeout_seconds)
            return "gitleaks", findings, None, time.perf_counter() - started
        except DeterministicAnalysisError as exc:
            return "gitleaks", [], AnalysisError(source="gitleaks", message=str(exc)), time.perf_counter() - started

    async def _deterministic() -> tuple[str, list[Finding], AnalysisError | None, float]:
        if not static_files:
            return "deterministic", [], None, 0.0
        started = time.perf_counter()
        det_context = change.model_copy(update={"files": static_files})
        try:
            findings = await asyncio.to_thread(run_deterministic, det_context, timeout_seconds)
            return "deterministic", findings, None, time.perf_counter() - started
        except DeterministicAnalysisError as exc:
            return "deterministic", [], AnalysisError(source="deterministic", message=str(exc)), time.perf_counter() - started

    tasks = [_gitleaks()]
    if cfg.get("deterministic", {}).get("enabled", True):
        tasks.append(_deterministic())

    findings: list[Finding] = []
    errors = list(state.errors)
    scanner_results: dict[str, list[Finding]] = dict(state.scanner_results)
    timings = dict(state.timings)

    for name, result, error, duration in await asyncio.gather(*tasks):
        scanner_results[name] = result
        findings.extend(result)
        if error:
            errors.append(error)
        timings[f"{name}_s"] = duration

    return {
        "scanner_results": scanner_results,
        "findings": findings,
        "errors": errors,
        "timings": timings,
        "lifecycle": "EXECUTING",
    }


def correlate_node(state: ValidationState) -> dict:
    """CORRELATOR: dedups/merges findings from the deterministic phase.
    Runs again after `assess` (see `policy_node`) - `aggregate()` is
    safe to call twice, it just re-merges already-merged entries.
    """
    return {"findings": aggregate(state.findings), "lifecycle": "CORRELATING"}


def route_after_correlate(state: ValidationState, cfg: dict) -> str:
    """Dynamic routing: the AI pass only runs for agentic-mode files
    when AI is enabled/forced. Mirrors `orchestrator.validate_change`'s
    `run_ai` condition exactly.
    """
    ai_files = state.diagnostics.get("ai_files", [])
    ai_cfg = cfg.get("ai", {})

    run_ai = bool(ai_files) and (
        state.request.force_ai or ai_cfg.get("enabled", True) or ai_cfg.get("force", False)
    )

    return "assess" if run_ai else "policy"


async def assess_node(state: ValidationState, cfg: dict) -> dict:
    """ASSESSOR: the AI pass. Requires `request.sandboxed=True` to
    actually execute - if False, the pass is skipped and recorded as an
    error (fails the gate under `fail_closed`), mirroring
    `orchestrator.validate_change`'s sandboxing gate.
    """
    change = state.change
    assert change is not None

    ai_files = state.diagnostics.get("ai_files", [])
    errors = list(state.errors)
    timings = dict(state.timings)
    started = time.perf_counter()
    ai_findings: list[Finding] = []

    if not state.request.sandboxed:
        errors.append(
            AnalysisError(
                source="ai",
                message=(
                    f"agentic review requested for {ai_files} but sandboxed=False; "
                    "the agentic pass only runs inside the isolated local-model sandbox."
                ),
            )
        )
    else:
        try:
            ai_diff = (
                change.diff
                if set(ai_files) == set(change.files)
                else await asyncio.to_thread(get_raw_diff, change.base, change.head, ai_files)
            )
            ai_context = change.model_copy(update={"files": ai_files, "diff": ai_diff})
            deterministic_findings = [f for f in state.findings if f.source == "deterministic"]
            ai_findings = await review_locally_async(ai_context, cfg.get("ai", {}), deterministic_findings)
        except AIReviewError as exc:
            errors.append(AnalysisError(source="ai", message=str(exc)))

    timings["ai_s"] = time.perf_counter() - started

    return {
        "findings": state.findings + ai_findings,
        "assessment": ai_findings,
        "errors": errors,
        "timings": timings,
        "lifecycle": "ASSESSING",
    }


def policy_node(state: ValidationState, cfg: dict) -> dict:
    """POLICY_EVALUATION: final aggregate + gate decision, per
    `validation.policy.evaluate`.
    """
    findings = aggregate(state.findings)
    diff = state.change.diff if state.change else ""

    passed = evaluate(findings, cfg, errors=bool(state.errors), diff=diff)
    status: Literal["PASS", "FAIL"] = "PASS" if passed else "FAIL"

    result = ValidationResult(
        status=status,
        findings=findings,
        errors=state.errors,
        timings=state.timings,
    )

    return {"findings": findings, "policy_decision": status, "result": result, "lifecycle": "COMPLETED"}


def build_validation_graph(cfg: dict, checkpointer: BaseCheckpointSaver | None = None):
    """`cfg` is bound into nodes via `functools.partial`, mirroring
    `core/graph.py::build_graph(cfg)`'s own pattern. `checkpointer`
    defaults to `None`; passing one requires the caller to have already
    called `dirty_tree_gate()`.
    """
    g = StateGraph(ValidationState)

    g.add_node("resolve", functools.partial(resolve_node, cfg=cfg))
    g.add_node("plan", functools.partial(plan_node, cfg=cfg))
    g.add_node("execute", functools.partial(execute_node, cfg=cfg))
    g.add_node("correlate", correlate_node)
    g.add_node("assess", functools.partial(assess_node, cfg=cfg))
    g.add_node("policy", functools.partial(policy_node, cfg=cfg))
    g.add_node("finalize_empty", finalize_empty_node)

    g.set_entry_point("resolve")
    g.add_conditional_edges("resolve", route_after_resolve, {"empty": "finalize_empty", "plan": "plan"})
    g.add_edge("plan", "execute")
    g.add_edge("execute", "correlate")
    g.add_conditional_edges(
        "correlate",
        functools.partial(route_after_correlate, cfg=cfg),
        {"assess": "assess", "policy": "policy"},
    )
    g.add_edge("assess", "policy")
    g.add_edge("policy", END)
    g.add_edge("finalize_empty", END)

    return g.compile(checkpointer=checkpointer)


async def _ainvoke_validation(
    request: ValidationRequest,
    cfg: dict,
    change: ChangeContext | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    thread_id: str | None = None,
) -> ValidationState:
    graph = build_validation_graph(cfg, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": thread_id}} if thread_id else None
    raw = await graph.ainvoke(ValidationState(request=request, change=change), config=config)

    return ValidationState.model_validate(raw)


def run_validation_sync(
    request: ValidationRequest,
    cfg: dict,
    change: ChangeContext | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    thread_id: str | None = None,
) -> ValidationState:
    """The one blocking entrypoint a synchronous caller may use -
    `asyncio.run()` appears only here. `change`, if the caller already
    has a resolved `ChangeContext`, skips re-resolving it in
    `resolve_node`. If `checkpointer` is passed, the caller must have
    already called `dirty_tree_gate()`.
    """
    return asyncio.run(
        _ainvoke_validation(request, cfg, change=change, checkpointer=checkpointer, thread_id=thread_id)
    )
