from __future__ import annotations

from gitscribe.validation.models import Finding


_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def evaluate(
    findings: list[Finding],
    cfg: dict,
    errors: bool = False,
) -> bool:
    if errors and cfg.get(
        "fail_closed",
        True,
    ):
        return False

    fail_on = set(
        cfg.get(
            "fail_on",
            ["critical", "high"],
        )
    )

    block_secrets = bool(
        cfg.get(
            "block_secrets",
            True,
        )
    )

    for finding in findings:
        if (
            block_secrets
            and "secret"
            in finding.category.lower()
        ):
            return False

        if finding.severity not in fail_on:
            continue

        if finding.source == "deterministic":
            return True

        if finding.confidence == "high":
            return True

    return False


def sort_findings(
    findings: list[Finding],
) -> list[Finding]:
    return sorted(
        findings,
        key=lambda item: (
            -_SEVERITY_RANK[item.severity],
            item.file or "",
            item.line or 0,
            item.category,
        ),
    )