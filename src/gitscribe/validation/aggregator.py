from __future__ import annotations

from gitscribe.validation.models import Finding


def _key(
    finding: Finding,
) -> tuple[str | None, int | None, str]:
    return (
        finding.file,
        finding.line,
        finding.category.lower().strip(),
    )


def aggregate(
    findings: list[Finding],
) -> list[Finding]:
    merged: dict[
        tuple[str | None, int | None, str],
        Finding,
    ] = {}

    for finding in findings:
        key = _key(finding)
        existing = merged.get(key)

        if existing is None:
            merged[key] = finding
            continue

        existing.sources = sorted(
            set(
                existing.sources
                + finding.sources
                + [finding.source]
            )
        )

        existing.evidence = (
            f"{existing.evidence} | "
            f"{finding.evidence}"
        )

        if (
            existing.source == "ai"
            and finding.source == "deterministic"
        ):
            merged[key] = finding.model_copy(
                update={
                    "sources": existing.sources,
                    "evidence": existing.evidence,
                }
            )

    return list(merged.values())
