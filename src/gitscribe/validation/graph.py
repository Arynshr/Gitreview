"""
GitScribe's *second*, independent LangGraph StateGraph - validation.

Per validation_layer.md §9.1: `core/graph.py`'s `StateGraph(GitScribeState)`
is scoped entirely to PR generation and shares no fields with validation.
This module is deliberately a separate graph over a separate state model;
it must never be merged with `core.graph`.

STATUS: this is "Insertion Point A" from the amended Required
Implementation Sequence (§8) - a no-op skeleton (`resolve -> plan -> END`)
built *before* any scanner becomes a graph node, to prove three things in
isolation:

  1. the sync CLI entrypoint / async graph invocation boundary works
     (§4a.4, §9.3) - see `run_validation_sync()` below.
  2. the checkpoint `thread_id` derivation reproduces the old file cache's
     key inputs exactly (§10.2) - see `derive_thread_id()`, which does not
     re-implement key derivation but calls the existing
     `core.hooks._validation_cache_key()` directly, so "exactly reproduces"
     is true by construction rather than by two implementations agreeing.
  3. the dirty-working-tree gate runs before any checkpoint read/write
     (§10.2) - see `dirty_tree_gate()`, which reuses
     `core.hooks._clean_working_tree()` rather than re-implementing the
     `git status --porcelain` check.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from gitscribe.core.hooks import _clean_working_tree, _validation_cache_key
from gitscribe.validation.analysis.models import AnalysisScope, ExecutionPlan
from gitscribe.validation.models import (
    AnalysisError,
    ChangeContext,
    Finding,
    ValidationRequest,
    ValidationResult,
)
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
    """Per validation_layer.md §4a.1. Nodes communicate only through this
    typed state - never globals, SQLite side effects, filesystem state,
    or ad hoc dicts.
    """

    request: ValidationRequest
    change: ChangeContext | None = None
    scope: AnalysisScope | None = None
    plan: ExecutionPlan | None = None
    scanner_results: dict[str, list[Finding]] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    context: dict | None = None  # RepoContext - deferred, see module docstring
    assessment: list[Finding] = Field(default_factory=list)
    policy_decision: Literal["PASS", "FAIL"] | None = None
    errors: list[AnalysisError] = Field(default_factory=list)
    diagnostics: dict[str, object] = Field(default_factory=dict)
    lifecycle: Lifecycle = "RECEIVED"
    result: ValidationResult | None = None


class DirtyWorkingTreeError(RuntimeError):
    """Raised by `dirty_tree_gate()` - a checkpointer has no native
    concept of git working-tree state (§10.2), so this must be an
    explicit, unskippable check ahead of any checkpoint read or write,
    not something inferred from graph state.
    """


def dirty_tree_gate() -> None:
    """Must be called before any checkpoint read/write once a
    checkpointer is attached (Insertion Point C). Reuses
    `core.hooks._clean_working_tree()` rather than re-implementing the
    `git status --porcelain` check - see §3.7's carried-forward
    invariant: validation results that inspect working-tree files must
    not be reused when the tree is dirty.
    """
    if not _clean_working_tree():
        raise DirtyWorkingTreeError(
            "working tree is dirty; refusing to read or write a "
            "validation checkpoint against it"
        )


def derive_thread_id(cfg: dict, base: str, head: str, *extra: object) -> str:
    """The checkpoint `thread_id` for a run. Deliberately calls the
    existing `_validation_cache_key()` rather than re-deriving the same
    inputs (diff hash, cfg fingerprint, sandboxed/force_ai flags,
    scanner/policy/ruleset versions) a second way - §10.2 requires this
    to *exactly reproduce* the old cache key's inputs, and delegating is
    the only way that's true by construction instead of by two
    implementations happening to agree today and drifting tomorrow.

    Same inputs -> same thread, deterministically; never a random ID.
    """
    return _validation_cache_key(cfg, base, head, *extra)


def resolve_node(state: ValidationState) -> dict:
    """RESOLVER node (§4a.2 diagram). Wraps the existing, canonical
    `validation.resolver.resolve_change` - per §3.1, no graph node may
    reimplement git diff resolution.
    """
    req = state.request

    change = resolve_change(
        base=req.base_ref,
        head=req.head_ref,
        paths=req.paths or None,
    )

    return {"change": change, "lifecycle": "RESOLVING"}


def plan_node(state: ValidationState) -> dict:
    """PLANNER node stub. Produces an empty `ExecutionPlan` - see the
    module docstring's "deliberately deferred" list for why real
    capability-based planning (and the mandatory Gitleaks job, §10.3)
    isn't implemented here.
    """
    files = state.change.files if state.change else []
    scope = AnalysisScope(files=files)

    return {
        "scope": scope,
        "plan": ExecutionPlan(jobs=[]),
        "lifecycle": "PLANNING",
    }


def build_validation_graph():
    """Compiles the no-op skeleton: START -> resolve -> plan -> END.

    No checkpointer is attached yet - see module docstring.
    """
    g = StateGraph(ValidationState)

    g.add_node("resolve", resolve_node)
    g.add_node("plan", plan_node)

    g.set_entry_point("resolve")
    g.add_edge("resolve", "plan")
    g.add_edge("plan", END)

    return g.compile()


async def _ainvoke_validation(request: ValidationRequest) -> ValidationState:
    graph = build_validation_graph()
    raw = await graph.ainvoke(ValidationState(request=request))
    return ValidationState.model_validate(raw)


def run_validation_sync(request: ValidationRequest) -> ValidationState:
    """The one blocking entrypoint a synchronous caller (a Typer command,
    a git hook shelling out to it) may use. Per §9.3, this is the only
    place `asyncio.run()` may appear for validation - the graph's async
    internals must be fully awaited here before any exit code is
    returned; a hook must never observe a partially-completed graph as
    final. `validation/cli.py::verify` does not call this yet (see module
    docstring) - this exists to prove the boundary in isolation first.
    """
    return asyncio.run(_ainvoke_validation(request))
