from gitscribe.validation.models import Finding
from gitscribe.validation.policy import evaluate


def make_finding(
    source: str = "deterministic",
    severity: str = "high",
    confidence: str = "high",
    category: str = "security",
) -> Finding:
    return Finding(
        id="test",
        source=source,
        category=category,
        rule_id="TEST",
        severity=severity,
        confidence=confidence,
        file="fixture.py",
        line=1,
        message="test finding",
        evidence="test evidence",
        remediation="test remediation",
        sources=[source],
    )


def test_deterministic_high_fails() -> None:
    assert not evaluate(
        [make_finding()],
        {
            "fail_on": ["critical", "high"],
            "block_secrets": True,
            "fail_closed": True,
        },
    )


def test_low_confidence_ai_does_not_fail() -> None:
    assert evaluate(
        [
            make_finding(
                source="ai",
                confidence="low",
            )
        ],
        {
            "fail_on": ["critical", "high"],
            "block_secrets": True,
            "fail_closed": True,
        },
    )


def test_high_confidence_ai_high_fails() -> None:
    assert not evaluate(
        [
            make_finding(
                source="ai",
                confidence="high",
            )
        ],
        {
            "fail_on": ["critical", "high"],
            "block_secrets": True,
            "fail_closed": True,
        },
    )


def test_secret_is_blocked() -> None:
    assert not evaluate(
        [
            make_finding(
                category="secrets",
                severity="medium",
            )
        ],
        {
            "fail_on": ["critical", "high"],
            "block_secrets": True,
            "fail_closed": True,
        },
    )


def test_analysis_error_fails_closed() -> None:
    assert not evaluate(
        [],
        {
            "fail_on": ["critical", "high"],
            "block_secrets": True,
            "fail_closed": True,
        },
        errors=True,
    )
