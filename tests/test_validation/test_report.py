"""Unit: terminal rendering (summary, diff view, live view, NDJSON).

Output is rendered to an in-memory, colourless rich Console and asserted on
as plain text - no TTY, no ANSI.
"""

from __future__ import annotations

import io
import json
import re

import pytest
from rich.console import Console

from gitscribe.validation import report
from gitscribe.validation.events import Event
from gitscribe.validation.models import AnalysisError, ValidationResult


def _console() -> Console:
    return Console(file=io.StringIO(), width=140, force_terminal=False, color_system=None)


def _text(console: Console) -> str:
    return console.file.getvalue()


def _result(findings=(), errors=(), status="FAIL", **diagnostics) -> ValidationResult:
    return ValidationResult(status=status, findings=list(findings), errors=list(errors), timings={"total_s": 1.2},
                            diagnostics=diagnostics)


# --- summary ---------------------------------------------------------------


def test_banner_shows_status_reason_and_counts(make_finding) -> None:
    c = _console()
    result = _result([make_finding(severity="high"), make_finding(severity="low", line=2)],
                     gate_reason="high finding from TEST")

    report.render_summary(result, c)

    out = _text(c)
    assert "FAIL" in out and "high finding from TEST" in out
    assert "1 high" in out and "1 low" in out


def test_clean_run_has_no_finding_sections() -> None:
    c = _console()

    report.render_summary(_result(status="PASS", jobs=["ruff"], scanner_health={"ruff": None}), c)

    out = _text(c)
    assert "PASS" in out and "no findings" in out
    assert "Top-risk files" not in out and "Severity × category" not in out


def test_findings_are_grouped_by_file_and_ordered_by_severity(make_finding) -> None:
    c = _console()
    findings = [
        make_finding(file="b.py", severity="low", message="minor"),
        make_finding(file="a.py", severity="critical", message="severe"),
    ]

    report.render_summary(_result(findings), c)

    out = _text(c)
    assert out.index("severe") < out.index("minor")
    assert "a.py" in out and "b.py" in out and "fix: test remediation" in out


def test_top_risk_files_rank_by_weighted_severity(make_finding) -> None:
    c = _console()
    findings = [make_finding(file="low.py", severity="low")] + [make_finding(file="hot.py", severity="critical")]

    report.render_summary(_result(findings), c)

    table = _text(c).split("Top-risk files")[1]
    assert table.index("hot.py") < table.index("low.py")


@pytest.mark.parametrize("debug,truncated", [(False, True), (True, False)])
def test_long_evidence_is_truncated_unless_debug(make_finding, debug, truncated) -> None:
    c = _console()

    report.render_summary(_result([make_finding(evidence="Q" * 400)]), c, debug=debug)

    # rich wraps long unbroken text, so count characters instead of matching a run
    assert (_text(c).count("Q") < 400) is truncated


def test_scanner_health_distinguishes_ok_error_unavailable_and_not_needed(make_finding) -> None:
    c = _console()
    result = _result(
        errors=[AnalysisError(source="semgrep", message="timed out")],
        jobs=["ruff", "semgrep"],
        scanner_health={"ruff": None, "semgrep": None, "osv-scanner": "binary missing", "betterleaks": None},
    )

    report.render_summary(result, c)

    lines = _text(c).splitlines()
    rows = {n: next(line for line in lines if "│" in line and re.search(rf"\b{n}\b", line))
            for n in ("ruff", "semgrep", "osv-scanner", "betterleaks")}
    assert "ok" in rows["ruff"]
    assert "error" in rows["semgrep"] and "timed out" in rows["semgrep"]
    assert "unavailable" in rows["osv-scanner"] and "binary missing" in rows["osv-scanner"]
    assert "not needed" in rows["betterleaks"]  # healthy but nothing to scan: still reported


# --- diff view -------------------------------------------------------------

DIFF = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@
 keep
-old
+new
+added
 tail
"""


def test_parse_diff_tracks_new_line_numbers_and_removals() -> None:
    [hunk] = report._parse_diff(DIFF)["a.py"]

    assert hunk == [(" ", 1, "keep"), ("-", None, "old"), ("+", 2, "new"), ("+", 3, "added"), (" ", 4, "tail")]


def test_parse_diff_ignores_new_files_deleted_to_dev_null() -> None:
    diff = "diff --git a/x b/x\n--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"

    assert report._parse_diff(diff) == {}


def test_finding_is_annotated_directly_under_its_line(make_finding) -> None:
    c = _console()

    report.render_diff(_result([make_finding(line=3, message="bad call")]), DIFF, c)

    lines = _text(c).splitlines()
    anchor = next(i for i, line in enumerate(lines) if line.endswith("+ added"))
    assert "▲ [HIGH] security: bad call" in lines[anchor + 1]
    assert "fix: test remediation" in lines[anchor + 2]


def test_findings_outside_changed_lines_are_listed_separately(make_finding) -> None:
    c = _console()
    findings = [make_finding(file="other.py", line=9, message="elsewhere"), make_finding(line=None, message="repo-wide")]

    report.render_diff(_result(findings), DIFF, c)

    out = _text(c)
    assert "Not on changed lines" in out
    assert "other.py:9" in out and "elsewhere" in out and "repo-wide" in out


def test_distinct_findings_sharing_an_id_are_not_conflated(make_finding) -> None:
    c = _console()
    on_line = make_finding(line=3, message="annotated")
    off_diff = make_finding(file="z.py", line=1, message="listed")  # same default id as on_line

    report.render_diff(_result([on_line, off_diff]), DIFF, c)

    assert "Not on changed lines" in _text(c) and "listed" in _text(c)


def test_distant_context_is_elided() -> None:
    body = "".join(f" ctx{i}\n" for i in range(1, 21))
    diff = f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,20 +1,20 @@\n{body}"
    from gitscribe.validation.models import Finding

    finding = Finding(id="x", source="deterministic", category="c", rule_id="R", severity="high", confidence="high",
                      file="a.py", line=10, message="m", evidence="", remediation="", sources=["deterministic"])
    c = _console()

    report.render_diff(_result([finding]), diff, c)

    out = _text(c)
    assert "ctx10" in out and "ctx7" in out and "ctx13" in out
    assert "ctx1\n" not in out and "ctx20" not in out


# --- live view + ndjson ----------------------------------------------------


def _feed(renderer, *events) -> None:
    for kind, data in events:
        renderer(Event(kind, data))


def test_live_renderer_tracks_scanner_lifecycle_and_stops_at_gate() -> None:
    r = report.LiveRenderer(_console())

    _feed(r, ("plan.ready", {"jobs": ["ruff", "semgrep"]}))
    assert r.live is not None and {n: v["state"] for n, v in r.rows.items()} == {"ruff": "queued", "semgrep": "queued"}

    _feed(r, ("scanner.started", {"scanner": "ruff"}),
          ("finding.found", {"severity": "high"}),
          ("scanner.finished", {"scanner": "ruff", "findings": 1, "seconds": 0.4, "error": None}),
          ("scanner.finished", {"scanner": "semgrep", "findings": 0, "seconds": 0.1, "error": "down"}))
    assert r.rows["ruff"]["state"] == "done" and r.rows["semgrep"]["state"] == "error"
    assert r.sev["high"] == 1

    _feed(r, ("gate.decision", {"status": "FAIL"}))
    assert r.live is None


def test_live_renderer_ignores_events_when_nothing_was_planned() -> None:
    r = report.LiveRenderer(_console())  # e.g. a cache hit: no plan.ready ever arrives

    _feed(r, ("scanner.started", {"scanner": "ruff"}), ("gate.decision", {"status": "PASS"}))

    assert r.live is None and r.rows == {}


def test_ndjson_emitter_writes_one_parsable_object_per_event() -> None:
    sink = io.StringIO()
    emit = report.ndjson_emitter(sink)

    emit(Event("plan.ready", {"jobs": ["ruff"]}, ts=1.0))
    emit(Event("gate.decision", {"status": "PASS"}, ts=2.0))

    parsed = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [p["type"] for p in parsed] == ["plan.ready", "gate.decision"]
    assert parsed[0]["jobs"] == ["ruff"]


def test_empty_change_is_stated_explicitly_not_reported_as_silence() -> None:
    c = _console()

    report.render_summary(_result(status="PASS"), c, scope_label="origin/main...HEAD")

    out = _text(c)
    assert "nothing to verify" in out and "origin/main...HEAD" in out and "--staged" in out
    assert "Scanner health" not in out  # nothing ran, no empty table
