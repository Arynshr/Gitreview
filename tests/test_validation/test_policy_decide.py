"""Unit: `policy.decide` explains *why* the gate failed; `evaluate` stays in lockstep."""

from __future__ import annotations

import pytest

from gitscribe.validation.policy import decide, evaluate

CFG = {
    "fail_on": ["critical", "high"],
    "block_secrets": True,
    "fail_closed": True,
    "block_new_vulnerabilities": False,
}


def test_pass_has_no_reason(make_finding) -> None:
    assert decide([make_finding(severity="low")], CFG) == (True, "")


def test_scanner_error_reason() -> None:
    assert decide([], CFG, errors=True) == (False, "scanner error with fail_closed enabled")


def test_scanner_error_ignored_when_not_fail_closed() -> None:
    assert decide([], {**CFG, "fail_closed": False}, errors=True) == (True, "")


def test_secret_category_reason(make_finding) -> None:
    ok, reason = decide([make_finding(severity="low", category="hardcoded api key")], CFG)

    assert (ok, reason) == (False, "secret-like finding (hardcoded api key)")


def test_deterministic_reason_names_severity_and_rule(make_finding) -> None:
    assert decide([make_finding(severity="critical", rule_id="S101")], CFG) == (
        False,
        "critical finding from S101",
    )


def test_ai_critical_reason(make_finding) -> None:
    ok, reason = decide([make_finding(source="ai", severity="critical", confidence="low")], CFG)

    assert (ok, reason) == (False, "critical AI finding")


def test_ai_high_reason_includes_confidence(make_finding) -> None:
    ok, reason = decide([make_finding(source="ai", severity="high", confidence="medium")], CFG)

    assert (ok, reason) == (False, "high AI finding (medium confidence)")


def test_low_confidence_ai_high_is_advisory(make_finding) -> None:
    assert decide([make_finding(source="ai", severity="high", confidence="low")], CFG) == (True, "")


@pytest.mark.parametrize(
    "overrides,errors",
    [
        ({"severity": "low"}, False),
        ({"severity": "high"}, False),
        ({"category": "secret"}, False),
        ({}, True),
    ],
)
def test_evaluate_matches_decide(make_finding, overrides, errors) -> None:
    findings = [make_finding(**overrides)]

    assert evaluate(findings, CFG, errors=errors) is decide(findings, CFG, errors=errors)[0]
