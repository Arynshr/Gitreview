"""Inline terminal UI shared by every service: a compact, in-place phase
list (no screen takeover) with progress bars, nested scanner rows and an
elapsed-time footer. Consumes the same events as `--stream json`.

Modes (`--stream`): auto (live on a TTY, silent when piped), live, json
(NDJSON events + a final `result` line, human output moved to stderr), off.
"""

from __future__ import annotations

import json
import sys
import time

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from gitscribe import console as plain
from gitscribe.events import Event
from gitscribe.validation.report import ndjson_emitter

ACCENT, OK, ERR, DIM = "cyan", "green", "red", "dim"
LABELS = {
    "diff_parser": "read diff", "summarizer": "summarize", "validate": "validate",
    "mark_validation_blocked": "validation gate", "risk_classifier": "assess risk",
    "retriever": "retrieve context", "generator": "generate", "failure_router": "recover",
    "template_fallback": "fallback template", "retry_same_model": "retry",
    "retry_fallback_model": "retry (fallback model)",
    "discover": "discover files", "hash": "hash files", "parse": "parse symbols",
    "link": "link edges", "embed": "embed symbols", "finalize": "finalize index",
    "collect": "read changes", "message": "write message", "stage": "stage files", "commit": "commit",
}
_BAR = 24


def _bar(current: int, total: int) -> str:
    filled = int(_BAR * current / total) if total else 0
    return "█" * filled + "░" * (_BAR - filled)


class PhaseView:
    """Callable `Emitter`; drives one `rich.Live` region, left on screen at the end."""

    def __init__(self, console: Console, title: str, subtitle: str = "") -> None:
        self.console = console
        self.title, self.subtitle = title, subtitle
        self.phases: dict[str, dict] = {}
        self.current: str | None = None
        self.started = time.perf_counter()
        self.live = Live(self._render(), console=console, refresh_per_second=12, transient=False)

    def start(self) -> None:
        self.live.start()

    def stop(self) -> None:
        self.live.update(self._render(final=True))
        self.live.stop()

    def __call__(self, ev: Event) -> None:
        kind, d = ev.type, ev.data
        if kind == "phase.started":
            self.phases[d["name"]] = {"state": "running", "children": {}}
            self.current = d["name"]
        elif kind == "phase.progress":
            self.phases.setdefault(d["name"], {"state": "running", "children": {}}).update(
                cur=d["current"], total=d["total"], label=d["label"])
        elif kind in ("phase.finished", "phase.failed"):
            p = self.phases.setdefault(d["name"], {"children": {}})
            detail = {k: v for k, v in d.items() if k not in ("name", "seconds", "error")}
            p.update(state="done" if kind == "phase.finished" else "failed", seconds=d.get("seconds"),
                     detail=detail, error=d.get("error"))
        elif kind == "scanner.started" and self.current in self.phases:
            self.phases[self.current]["children"][d["scanner"]] = {"state": "running"}
        elif kind == "scanner.finished" and self.current in self.phases:
            self.phases[self.current]["children"][d["scanner"]] = {
                "state": "failed" if d["error"] else "done", "findings": d["findings"], "seconds": d["seconds"]}
        else:
            return
        self.live.update(self._render())

    def _render(self, final: bool = False) -> Group:
        head = Text.assemble(("◆ ", ACCENT), (self.title, "bold"), (f"  {self.subtitle}" if self.subtitle else "", DIM))
        grid = Table.grid(padding=(0, 1))
        for name, p in self.phases.items():
            grid.add_row(*self._row(name, p))
            for cname, c in p["children"].items():
                grid.add_row(Text(""), *self._child(cname, c))
        foot = Text(f"{time.perf_counter() - self.started:.1f}s", style=DIM)
        return Group(head, grid, foot)

    def _row(self, name: str, p: dict):
        state = p.get("state", "running")
        icon = {"done": Text("✔", style=OK), "failed": Text("✘", style=ERR)}.get(state) or Spinner("dots", style=ACCENT)
        label = Text(LABELS.get(name, name))
        if state == "running" and p.get("total"):
            note = Text(f"{_bar(p['cur'], p['total'])} {p['cur']}/{p['total']} {p.get('label', '')[-40:]}", style=ACCENT)
        elif state == "done":
            bits = [f"{k} {v}" for k, v in (p.get("detail") or {}).items()]
            note = Text("  ".join(bits + [f"{p['seconds']:.1f}s"]), style=DIM)
        elif state == "failed":
            note = Text(p.get("error") or "failed", style=ERR)
        else:
            note = Text("")
        return icon, label, note

    @staticmethod
    def _child(name: str, c: dict):
        state = c["state"]
        icon = {"done": Text("✔", style=OK), "failed": Text("✘", style=ERR)}.get(state) or Spinner("dots", style=ACCENT)
        note = Text(f"{c['findings']} finding(s)  {c['seconds']:.1f}s", style=DIM) if "findings" in c else Text("")
        return icon, Text(name, style=DIM), note


class Run:
    def __init__(self, emitter, view: PhaseView | None, mode: str, to_stderr: bool) -> None:
        self.emitter, self.view, self.mode, self.to_stderr = emitter, view, mode, to_stderr

    @property
    def json_stream(self) -> bool:
        return self.mode == "json"

    def __enter__(self) -> Run:
        if self.view:
            self.view.start()
        return self

    def __exit__(self, *exc) -> None:
        if self.view:
            self.view.stop()
        return None

    def result(self, **data) -> None:
        """Final NDJSON line in `--stream json` mode; no-op otherwise."""
        if self.json_stream:
            sys.stdout.write(json.dumps({"type": "result", **data}, default=str) + "\n")
            sys.stdout.flush()


def open_run(title: str, stream: str = "auto", machine: bool = False, subtitle: str = "", to_stderr: bool = False) -> Run:
    """`machine` = the command already emits its own `--json`: no live view.
    `to_stderr` keeps stdout clean for commands whose output is the product."""
    if stream not in ("auto", "live", "json", "off"):
        plain.error("--stream must be auto|live|json|off.")
        raise typer.Exit(1)
    plain.route_to_stderr(stream == "json")
    if machine or stream == "off":
        return Run(None, None, "off", to_stderr)
    if stream == "json":
        return Run(ndjson_emitter(), None, "json", to_stderr)
    console = Console(stderr=to_stderr)
    if stream == "auto" and not console.is_terminal:
        return Run(None, None, "off", to_stderr)
    view = PhaseView(console, title, subtitle)
    return Run(view, view, "live", to_stderr)
