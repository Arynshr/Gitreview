"""Terminal rendering for validation: live event view, summary report,
diff-annotated view. Pure presentation - consumes `Event`s and
`ValidationResult`; never runs analysis.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from gitscribe.validation.events import Event
from gitscribe.validation.models import Finding, ValidationResult
from gitscribe.validation.policy import sort_findings

SEVERITIES = ("critical", "high", "medium", "low", "info")
_STYLE = {"critical": "bold red", "high": "red", "medium": "yellow", "low": "cyan", "info": "dim"}
_WEIGHT = {"critical": 8, "high": 4, "medium": 2, "low": 1, "info": 0}
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def ndjson_emitter(stream=None):
    """`--stream json`: one JSON object per event, machine-consumable."""
    out = stream or sys.stdout

    def _emit(event: Event) -> None:
        out.write(event.to_json() + "\n")
        out.flush()

    return _emit


# --------------------------------------------------------------- live view


class LiveRenderer:
    """Callable `Emitter`. Starts on `plan.ready`, ends on `gate.decision`
    (transient: the summary report replaces it). A cache hit emits no
    events, so nothing is drawn."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.rows: dict[str, dict] = {}
        self.sev: dict[str, int] = defaultdict(int)
        self.live: Live | None = None

    def __call__(self, event: Event) -> None:
        kind, data = event.type, event.data

        if kind == "plan.ready":
            self.rows = {n: {"state": "queued", "findings": 0, "seconds": None} for n in data["jobs"]}
            self.live = Live(self._render(), console=self.console, refresh_per_second=10, transient=True)
            self.live.start()
            return

        if self.live is None:
            return

        if kind == "scanner.started":
            self.rows.setdefault(data["scanner"], {})["state"] = "running"
        elif kind == "scanner.finished":
            self.rows[data["scanner"]] = {
                "state": "error" if data["error"] else "done",
                "findings": data["findings"],
                "seconds": data["seconds"],
            }
        elif kind == "finding.found":
            self.sev[data["severity"]] += 1
        elif kind == "gate.decision":
            self.stop()
            return

        self.live.update(self._render())

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None

    def _render(self) -> RenderableType:
        table = Table.grid(padding=(0, 2))
        for name, row in self.rows.items():
            state = row.get("state", "queued")
            icon: RenderableType = {
                "queued": Text("·", style="dim"),
                "done": Text("✔", style="green"),
                "error": Text("✘", style="red"),
            }.get(state) or Spinner("dots", style="cyan")
            secs = f"{row['seconds']:.1f}s" if row.get("seconds") is not None else ""
            found = str(row.get("findings", 0)) if state in ("done", "error") else ""
            table.add_row(icon, Text(name), Text(found, style="bold"), Text(secs, style="dim"))

        counts = "  ".join(f"{n} {s}" for s, n in self._sorted_sev())
        footer = Text(f"findings so far: {counts}" if counts else "no findings yet", style="dim")
        return Panel(Group(table, footer), title="GitScribe verifying", border_style="cyan", expand=False)

    def _sorted_sev(self):
        return [(s, self.sev[s]) for s in SEVERITIES if self.sev.get(s)]


# ------------------------------------------------------------ summary view


def render_summary(
    result: ValidationResult, console: Console, debug: bool = False, scope_label: str = ""
) -> None:
    findings = sort_findings(result.findings)
    reason = str(result.diagnostics.get("gate_reason") or "")
    ok = result.status == "PASS"
    nothing_ran = not findings and not result.errors and not result.diagnostics.get("jobs")
    if ok and nothing_ran:
        reason = (
            f"nothing to verify: no changes in {scope_label or 'the selected range'} "
            "(use --staged or --working for uncommitted work)"
        )

    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    count_line = Text("  ".join(f"{n} {s}" for s, n in counts.items() if n) or "no findings")
    banner = Text.assemble((result.status, "bold green" if ok else "bold red"))
    if reason:
        banner.append(f"  —  {reason}")
    total = result.timings.get("total_s")
    if total is not None:
        banner.append(f"  ({total:.1f}s)", style="dim")
    console.print(Panel(Group(banner, count_line), title="GitScribe Verification",
                        border_style="green" if ok else "red"))

    if findings:
        console.print(_heatmap(findings))
        console.print(_top_files(findings))
        _print_by_file(findings, console, debug)

    health = _health(result)
    if health.row_count:
        console.print(health)


def _heatmap(findings: list[Finding]) -> Table:
    cats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for f in findings:
        cats[f.category][f.severity] += 1
    present = [s for s in SEVERITIES if any(cats[c].get(s) for c in cats)]
    table = Table(title="Severity × category", title_justify="left", header_style="bold")
    table.add_column("category")
    for s in present:
        table.add_column(s, justify="right", style=_STYLE[s])
    for cat in sorted(cats, key=lambda c: -sum(_WEIGHT[s] * n for s, n in cats[c].items())):
        table.add_row(cat, *[str(cats[cat].get(s, "")) for s in present])
    return table


def _top_files(findings: list[Finding], limit: int = 5) -> Table:
    score: dict[str, int] = defaultdict(int)
    for f in findings:
        score[f.file or "(repository)"] += _WEIGHT[f.severity]
    table = Table(title="Top-risk files", title_justify="left", header_style="bold")
    table.add_column("file")
    table.add_column("risk", justify="right")
    for path, pts in sorted(score.items(), key=lambda kv: -kv[1])[:limit]:
        table.add_row(path, str(pts))
    return table


def _finding_lines(f: Finding, debug: bool) -> list[Text]:
    loc = f":{f.line}" if f.line else ""
    head = Text.assemble((f"[{f.severity.upper()}] ", _STYLE[f.severity]), f"{f.category}  ",
                         (f"({'/'.join(f.sources or [f.source])}{loc})", "dim"))
    evidence = f.evidence if debug or len(f.evidence) <= 160 else f.evidence[:157] + "..."
    lines = [head, Text(f"  {f.message}")]
    if evidence:
        lines.append(Text(f"  evidence: {evidence}", style="dim"))
    if f.remediation:
        lines.append(Text(f"  fix: {f.remediation}", style="green"))
    return lines


def _print_by_file(findings: list[Finding], console: Console, debug: bool) -> None:
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        grouped[f.file or "(repository)"].append(f)
    for path, items in grouped.items():
        console.print(Text(f"\n{path}", style="bold underline"))
        for f in items:
            for line in _finding_lines(f, debug):
                console.print(line)


def _health(result: ValidationResult) -> Table:
    health = result.diagnostics.get("scanner_health", {}) or {}
    jobs = set(result.diagnostics.get("jobs", []) or [])
    errors = {e.source: e.message for e in result.errors}
    names = list(dict.fromkeys([*jobs, *health, *errors]))

    table = Table(title="Scanner health", title_justify="left", header_style="bold")
    for col in ("scanner", "state", "findings", "time", "note"):
        table.add_column(col)
    for name in names:
        err = errors.get(name) or health.get(name)
        if name in errors:
            state = Text("error", style="red")
        elif err:
            state = Text("unavailable", style="yellow")
        elif name in jobs:
            state = Text("ok", style="green")
        else:
            state = Text("not needed", style="dim")
        count = sum(1 for f in result.findings if name in (f.sources or [f.source]))
        secs = result.timings.get(f"{name}_s")
        table.add_row(name, state, str(count) if name in jobs else "", f"{secs:.1f}s" if secs else "",
                      (err or "")[:100])
    other = [e for e in result.errors if e.source not in names]
    for e in other:
        table.add_row(e.source, Text("error", style="red"), "", "", e.message[:100])
    return table


# --------------------------------------------------------------- diff view


def _parse_diff(diff: str) -> dict[str, list[list[tuple[str, int | None, str]]]]:
    """path -> hunks; each hunk is a list of (kind, new_line_no, text)."""
    files: dict[str, list] = {}
    hunks: list | None = None
    hunk: list | None = None
    new_no = 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            hunks = hunk = None
        elif hunk is None and raw.startswith("+++ "):
            path = raw[4:].strip()
            if path == "/dev/null":
                hunks = None
            else:
                hunks = files.setdefault(path[2:] if path.startswith("b/") else path, [])
        elif (m := _HUNK.match(raw)) and hunks is not None:
            new_no = int(m.group(1))
            hunk = []
            hunks.append(hunk)
        elif hunk is not None and raw[:1] in ("+", "-", " "):
            kind = raw[0]
            if kind == "-":
                hunk.append((kind, None, raw[1:]))
            else:
                hunk.append((kind, new_no, raw[1:]))
                new_no += 1
    return files


def render_diff(result: ValidationResult, diff: str, console: Console, context: int = 3) -> None:
    """Findings printed inline beneath the changed lines they refer to."""
    files = _parse_diff(diff)
    by_loc: dict[tuple[str, int], list[Finding]] = defaultdict(list)
    shown: set[int] = set()
    for f in result.findings:
        if f.file and f.line:
            by_loc[(f.file, f.line)].append(f)

    for path, hunks in files.items():
        for hunk in hunks:
            hits = [i for i, (_, no, _) in enumerate(hunk) if no and (path, no) in by_loc]
            if not hits:
                continue
            keep = sorted({j for i in hits for j in range(max(0, i - context), min(len(hunk), i + context + 1))})
            console.print(Text(f"\n{path}", style="bold underline"))
            prev = None
            for i in keep:
                if prev is not None and i != prev + 1:
                    console.print(Text("   ⋮", style="dim"))
                kind, no, text = hunk[i]
                style = {"+": "green", "-": "red"}.get(kind, "dim")
                console.print(Text(f"{no if no else '':>5} {kind} {text}", style=style))
                for f in by_loc.get((path, no), []) if no else []:
                    shown.add(id(f))
                    console.print(Text(f"      ▲ [{f.severity.upper()}] {f.category}: {f.message}",
                                       style=_STYLE[f.severity]))
                    if f.remediation:
                        console.print(Text(f"        fix: {f.remediation}", style="green"))
                prev = i

    rest = [f for f in sort_findings(result.findings) if id(f) not in shown]
    if rest:
        console.print(Text("\nNot on changed lines", style="bold"))
        for f in rest:
            loc = f"{f.file}:{f.line}" if f.file and f.line else (f.file or "(repository)")
            console.print(Text(f"  [{f.severity.upper()}] {loc} — {f.message}", style=_STYLE[f.severity]))
