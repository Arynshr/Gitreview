"""
Bridges `core/graph.py`'s PR-generation StateGraph to
`validation/graph.py`'s validation StateGraph.

The two graphs keep separate state models by design; this module is the
only place a `GitScribeState` is translated into a `ValidationRequest`
and a validation result translated back. Only a short summary crosses
back - never a raw `ValidationState`/`ValidationResult` - so
`GitScribeState` doesn't grow a dependency on validation's internal
shape.

`asyncio.run()` is used here, once, exactly like
`validation/graph.py::run_validation_sync` - `build_graph()`'s nodes are
sync (invoked via `.invoke()`, see `core/graph.py`), so this is the one
place that boundary is crossed for this node.
"""

from __future__ import annotations

import uuid

from gitscribe.core.state import GitScribeState
from gitscribe.validation.graph import run_validation_sync
from gitscribe.validation.models import ValidationRequest


def validate_node(state: GitScribeState, cfg: dict) -> dict:
    validation_cfg = cfg.get("validation", {})

    if not validation_cfg.get("enabled", True):
        return {"validation_status": "SKIPPED"}

    request = ValidationRequest(
        request_id=str(uuid.uuid4()),
        repo_root=".",
        base_ref=state.base_ref,
        head_ref=state.head_ref,
        trigger="pr",
        paths=state.files_changed,
    )

    try:
        result_state = run_validation_sync(request, validation_cfg)
    except Exception as exc:  # noqa: BLE001 - any resolver/graph failure, not ours to enumerate
        return {
            "validation_status": "ERROR",
            "validation_summary": f"validation did not complete: {exc}",
        }

    result = result_state.result

    if result is None:
        return {"validation_status": "ERROR", "validation_summary": "validation produced no result"}

    summary = (
        f"{result.status}: {len(result.findings)} finding(s)"
        + (f", {len(result.errors)} error(s)" if result.errors else "")
    )

    return {
        "validation_status": result.status,
        "validation_findings_count": len(result.findings),
        "validation_summary": summary,
    }


def route_after_validate(state: GitScribeState, cfg: dict) -> str:
    """Only routes to "blocked" when `validation.block_pr_generation` is
    explicitly enabled - default is "continue" regardless of
    validation_status, so existing `generate`/`create-pr` behavior is
    unchanged unless opted into.
    """
    block = cfg.get("validation", {}).get("block_pr_generation", False)

    if block and state.validation_status == "FAIL":
        return "blocked"

    return "continue"
