from __future__ import annotations

from gitscribe.validation.models import Finding

_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

_SECRET_CATEGORY_KEYWORDS = (
    "secret",
    "credential",
    "password",
    "apikey",
    "api key",
    "api_key",
    "private key",
    "access key",
    "token",
)


def _looks_like_secret(category: str) -> bool:
    """block_secrets matches on Finding.category, which is free text (both
    ruff's fixed categories and the AI reviewer's model-chosen ones) - not
    a closed enum. A single keyword ("secret") is trivially missed by a
    finding categorized e.g. "credential exposure" or "hardcoded api key".
    This is still a substring match on free text, so it's not airtight,
    but it closes the specific gap of a differently-worded secret category
    silently bypassing the hard block.
    """
    lowered = category.lower()
    return any(keyword in lowered for keyword in _SECRET_CATEGORY_KEYWORDS)


def _changed_lines(diff: str) -> dict[str, set[int]]:
    """
    Map each touched file to the set of line numbers actually added by
    the diff (new-file line numbers), by parsing unified diff hunks.

    Used to distinguish findings on lines this change actually introduced
    from pre-existing findings elsewhere in a touched file (e.g. ruff
    scans the whole changed file, not just changed lines). Best-effort:
    a file with no parseable hunk is simply absent from the result, and
    callers must treat "absent" as "don't filter" so parsing gaps fail
    toward blocking, not silently passing.
    """
    changed: dict[str, set[int]] = {}
    current_file: str | None = None
    new_line = 0

    for raw_line in diff.splitlines():
        if raw_line.startswith("Binary files "):
            # No text hunks will follow for this file - don't let a
            # previous file's line-tracking state leak into whatever
            # comes next in the diff.
            current_file = None
            continue

        if raw_line.startswith("rename to "):
            # Pure rename (no content change) never emits +++/--- lines,
            # so without this the file would be absent from `changed`
            # entirely and _is_new() would (correctly, but overly
            # conservatively) treat every finding in it as new. Record it
            # with an empty changed-line set instead: a rename introduces
            # no new lines.
            current_file = raw_line[len("rename to ") :].strip()
            changed.setdefault(current_file, set())
            continue

        if raw_line.startswith("+++ "):
            path = raw_line[4:].strip()

            if path == "/dev/null":
                current_file = None
                continue

            if path.startswith("b/"):
                path = path[2:]

            current_file = path
            changed.setdefault(current_file, set())
            continue

        if raw_line.startswith("@@ "):
            parts = raw_line.split("@@")

            if len(parts) < 2:
                continue

            tokens = parts[1].strip().split(" ")

            if len(tokens) < 2:
                continue

            new_spec = tokens[1].lstrip("+").split(",")[0]

            try:
                new_line = int(new_spec)
            except ValueError:
                continue

            continue

        if current_file is None:
            continue

        if raw_line.startswith("---"):
            continue

        if raw_line.startswith("+"):
            changed[current_file].add(new_line)
            new_line += 1
        elif raw_line.startswith("-"):
            continue
        else:
            new_line += 1

    return changed


def _is_new(
    finding: Finding,
    changed: dict[str, set[int]],
) -> bool:
    if not finding.file or not finding.line:
        # Findings with no attributable line (e.g. dependency
        # vulnerabilities from pip-audit) describe the current state of
        # the change as a whole and are always in scope.
        return True

    lines = changed.get(finding.file)

    if lines is None:
        # File not found in the diff we parsed (e.g. parsing gap) -
        # fail toward treating it as new/in-scope rather than
        # silently exempting it from the gate.
        return True

    return finding.line in lines


def evaluate(
    findings: list[Finding],
    cfg: dict,
    errors: bool = False,
    diff: str = "",
) -> bool:
    """
    Returns True when the change is OK to pass, False when it should
    fail the gate.
    """
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

    new_only = bool(
        cfg.get(
            "block_new_vulnerabilities",
            True,
        )
    )

    changed = _changed_lines(diff) if new_only else {}

    for finding in findings:
        if (
            block_secrets
            and _looks_like_secret(finding.category)
        ):
            return False

        if finding.severity not in fail_on:
            continue

        if new_only and not _is_new(finding, changed):
            # Real, but pre-existing in this file and outside the lines
            # this change touched - don't fail the gate on it.
            continue

        if finding.source == "deterministic":
            return False

        # AI-sourced finding. Per the advisory/gate rule (an ambiguous or
        # low-confidence signal makes the gate default to the conservative
        # path, never to "proceed") a *critical* severity finding blocks
        # regardless of the model's own self-reported confidence. Without
        # this, a model that hedges toward "low" confidence - whether by
        # genuine uncertainty or because the diff content nudged it to -
        # can make a real critical finding invisible to the gate entirely.
        if finding.severity == "critical":
            return False

        if finding.confidence in ("high", "medium"):
            return False

        # confidence == "low" at a fail_on severity below critical:
        # surfaced in the report, but treated as advisory-only for the
        # gate itself.

    return True


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
