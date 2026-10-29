"""Unit: what the bridge surfaces to PR generation (regression: a FAIL
printed only a count, so users couldn't tell what to fix)."""

from __future__ import annotations

from types import SimpleNamespace

from gitscribe.core import validation_bridge
from gitscribe.validation.models import AnalysisError, ValidationResult


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        base_ref="b",
        head_ref="h",
        files_changed=["a.py"],
        validation_status="",
        validation_summary="",
    )


def _run(monkeypatch, result: ValidationResult) -> dict:
    monkeypatch.setattr(
        validation_bridge, "run_validation_sync", lambda req, cfg: SimpleNamespace(result=result)
    )
    return validation_bridge.validate_node(_state(), {"validation": {}})


def test_findings_are_surfaced_most_severe_first(monkeypatch, make_finding) -> None:
    result = ValidationResult(
        status="FAIL",
        findings=[
            make_finding(severity="low", file="a.py", line=1, message="minor"),
            make_finding(severity="critical", file="b.py", line=9, message="severe"),
        ],
    )

    out = _run(monkeypatch, result)

    assert out["validation_findings"] == [
        ("critical", "b.py:9 - severe"),
        ("low", "a.py:1 - minor"),
    ]


def test_scanner_errors_come_before_findings(monkeypatch, make_finding) -> None:
    result = ValidationResult(
        status="FAIL",
        findings=[make_finding(message="f")],
        errors=[AnalysisError(source="semgrep", message="timed out")],
    )

    out = _run(monkeypatch, result)

    assert out["validation_findings"][0] == ("error", "semgrep: timed out")
    assert out["validation_findings"][1][0] == "high"


def test_surfaced_list_is_capped_but_count_is_not(monkeypatch, make_finding) -> None:
    result = ValidationResult(status="FAIL", findings=[make_finding(line=i) for i in range(1, 14)])

    out = _run(monkeypatch, result)

    assert len(out["validation_findings"]) == validation_bridge._MAX_SURFACED == 10
    assert out["validation_findings_count"] == 13


def test_finding_without_a_file_is_labelled_repository(monkeypatch, make_finding) -> None:
    out = _run(
        monkeypatch,
        ValidationResult(
            status="FAIL", findings=[make_finding(file=None, line=None, message="dep")]
        ),
    )

    assert out["validation_findings"] == [("high", "(repository) - dep")]


def test_long_messages_are_truncated(monkeypatch, make_finding) -> None:
    out = _run(
        monkeypatch, ValidationResult(status="FAIL", findings=[make_finding(message="m" * 500)])
    )

    assert len(out["validation_findings"][0][1]) == 200


def test_passing_run_surfaces_nothing(monkeypatch) -> None:
    out = _run(monkeypatch, ValidationResult(status="PASS"))

    assert out["validation_findings"] == [] and out["validation_status"] == "PASS"
