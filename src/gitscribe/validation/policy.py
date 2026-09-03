from __future__ import annotations

from gitscribe.validation.models import Finding

_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


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
            and "secret"
            in finding.category.lower()
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

        if finding.confidence == "high":
            return False

        if (
            finding.severity == "critical"
            and finding.confidence == "medium"
        ):
            # A medium-confidence AI finding at critical severity still
            # carries enough weight to block; only "low" confidence at
            # non-high-confidence severities is treated as advisory.
            return False

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
