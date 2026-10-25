"""Finding de-duplication (`validation/aggregator.py`)."""

from __future__ import annotations

from gitscribe.validation.aggregator import aggregate


def test_distinct_locations_are_untouched(make_finding) -> None:
    findings = [make_finding(line=1), make_finding(line=2), make_finding(file="b.py", line=1)]

    assert aggregate(findings) == findings


def test_same_location_and_category_merge_case_insensitively(make_finding) -> None:
    merged = aggregate([make_finding(category="Security"), make_finding(category=" security ")])

    assert len(merged) == 1


def test_deterministic_wins_and_ai_corroboration_is_preserved(make_finding) -> None:
    det = make_finding(source="deterministic", rule_id="S1", message="det msg", sources=["semgrep"])
    ai = make_finding(
        source="ai", rule_id="AI-x", message="ai msg", sources=["model"], remediation="ai fix"
    )

    (merged,) = aggregate([ai, det])  # order must not matter for who wins

    assert merged.source == "deterministic"
    assert merged.rule_id == "S1"
    assert "also flagged by ai" in merged.message and "ai msg" in merged.message
    assert "ai fix" in merged.remediation
    assert merged.sources == ["ai", "deterministic", "model", "semgrep"]


def test_inputs_are_never_mutated(make_finding) -> None:
    a = make_finding(source="deterministic", sources=["x"])
    b = make_finding(source="ai", sources=["y"])
    before = (a.model_copy(deep=True), b.model_copy(deep=True))

    aggregate([a, b])

    assert (a, b) == before


def test_aggregation_is_idempotent(make_finding) -> None:
    findings = [make_finding(), make_finding(source="ai", sources=["m"]), make_finding(line=9)]

    once = aggregate(findings)

    assert aggregate(once) == once
