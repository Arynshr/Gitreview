"""PR-generation bridge: a blocked run must say *why* (regression: the CLI
printed `(validation_blocked: )` with an empty reason)."""

from __future__ import annotations

from types import SimpleNamespace

from gitscribe.core import validation_bridge
from gitscribe.core.graph import mark_validation_blocked_node
from gitscribe.validation.models import AnalysisError, ValidationResult


def _state(**kw) -> SimpleNamespace:
    return SimpleNamespace(
        base_ref="b",
        head_ref="h",
        files_changed=["a.py"],
        validation_status="",
        validation_summary="",
        **kw,
    )


def test_summary_names_the_failing_source_and_reason(monkeypatch) -> None:
    result = ValidationResult(
        status="FAIL", errors=[AnalysisError(source="ai", message="requires sandboxed=True")]
    )
    monkeypatch.setattr(
        validation_bridge, "run_validation_sync", lambda req, cfg: SimpleNamespace(result=result)
    )

    out = validation_bridge.validate_node(_state(), {"validation": {}})

    assert out["validation_status"] == "FAIL"
    assert (
        out["validation_summary"] == "FAIL: 0 finding(s), 1 error(s); ai: requires sandboxed=True"
    )


def test_blocked_node_carries_the_validation_summary_as_the_reason() -> None:
    out = mark_validation_blocked_node(SimpleNamespace(validation_summary="FAIL: 1 finding(s)"))

    assert out == {"failure_type": "validation_blocked", "last_error": "FAIL: 1 finding(s)"}


def test_blocking_is_opt_in() -> None:
    state = SimpleNamespace(validation_status="FAIL")

    assert validation_bridge.route_after_validate(state, {"validation": {}}) == "continue"
    assert (
        validation_bridge.route_after_validate(state, {"validation": {"block_pr_generation": True}})
        == "blocked"
    )
