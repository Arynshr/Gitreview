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


def aggregate(findings: list[Finding]) -> list[Finding]:
    """Merges findings that land on the same (file, line, category).

    Every merge produces a *new* Finding via model_copy rather than
    mutating either input in place - the previous version mutated
    `existing.sources`/`existing.evidence` directly, which is unsafe if a
    caller holds another reference to the same object (e.g. for reporting
    counts before aggregation runs).

    Two other correctness issues this fixes:
    - Traceability: the previous version only ever added `finding.source`
      (the second item's own source label) to the merged `sources` list,
      never `existing.source` (the first item's). It happened to work out
      because every Finding constructor already seeds `sources` with its
      own tool/model identifier - but that made it accidental, not
      guaranteed, and fragile to any future finding source that didn't.
      This version adds both labels explicitly.
    - Data loss: the previous version kept the deterministic finding's
      `message`/`remediation` wholesale when a deterministic and an AI
      finding merged, silently discarding the AI finding's own
      description/remediation. Two independent detectors flagging the
      same location is exactly the corroboration this design counts on -
      the merged finding now says what the non-winning source reported
      too, instead of dropping it.
    """
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

        combined_sources = sorted(
            {
                *existing.sources,
                *finding.sources,
                existing.source,
                finding.source,
            }
        )
        combined_evidence = f"{existing.evidence} | {finding.evidence}"

        # Deterministic findings are precise (rule-id backed); an AI
        # finding at the same location corroborates it rather than
        # replacing it. When both sources are the same type, keep
        # whichever arrived second (matches prior de-dup-by-latest
        # behavior for same-source repeats).
        winner = existing if existing.source == "deterministic" else finding
        loser = finding if winner is existing else existing

        merged_message = winner.message
        merged_remediation = winner.remediation

        if loser.source != winner.source:
            merged_message = (
                f"{winner.message} (also flagged by {loser.source}, "
                f"rule {loser.rule_id}: {loser.message})"
            )
            if loser.remediation and loser.remediation != winner.remediation:
                merged_remediation = (
                    f"{winner.remediation} | {loser.source}: {loser.remediation}"
                )

        merged[key] = winner.model_copy(
            update={
                "sources": combined_sources,
                "evidence": combined_evidence,
                "message": merged_message,
                "remediation": merged_remediation,
            }
        )

    return list(merged.values())
