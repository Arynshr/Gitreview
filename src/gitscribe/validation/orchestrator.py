from __future__ import annotations

import uuid

from gitscribe.validation.graph import run_validation_sync
from gitscribe.validation.models import ChangeContext, ValidationRequest, ValidationResult


def validate_change(
    context: ChangeContext,
    cfg: dict,
    force_ai: bool = False,
    force_mode: str | None = None,
    sandboxed: bool = False,
) -> ValidationResult:
    """Thin, backward-compatible facade over `validation/graph.py`'s
    execution DAG - kept so existing callers (`validation/cli.py`,
    hooks) don't need to build a `ValidationRequest` themselves. The
    actual pipeline (mode split, mandatory secret scan, deterministic
    bundle, conditional AI pass, policy evaluation) lives entirely in
    graph.py now; this function no longer duplicates it.

    `context` is passed straight through as the graph's pre-resolved
    `change` (see `graph.resolve_node`), so this doesn't re-resolve the
    diff from git a second time.
    """
    request = ValidationRequest(
        request_id=str(uuid.uuid4()),
        repo_root=".",
        base_ref=context.base,
        head_ref=context.head,
        paths=context.files,
        requested_mode=force_mode,
        sandboxed=sandboxed,
        force_ai=force_ai,
    )

    state = run_validation_sync(request, cfg, change=context)

    assert state.result is not None  # every graph path ends in policy or finalize_empty
    return state.result
