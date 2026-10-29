"""Unit: phase/progress events and the inline UI that renders them."""

from __future__ import annotations

import io
import json

import pytest
import typer
from rich.console import Console

from gitscribe import ui
from gitscribe.core.progress import phase, progress
from gitscribe.events import Event


@pytest.fixture(autouse=True)
def _reset_console_routing():
    yield
    from gitscribe import console

    console.route_to_stderr(False)  # `--stream json` moves human output to stderr; don't leak it


class Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, e: Event) -> None:
        self.events.append(e)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


def test_phase_reports_start_finish_and_detail() -> None:
    sink = Sink()

    with phase(sink, "parse") as info:
        info["files"] = 3

    assert sink.types() == ["phase.started", "phase.finished"]
    done = sink.events[1].data
    assert done["name"] == "parse" and done["files"] == 3 and done["seconds"] >= 0


def test_phase_reports_failure_and_reraises() -> None:
    sink = Sink()

    with pytest.raises(ValueError), phase(sink, "embed"):
        raise ValueError("boom")

    assert sink.types() == ["phase.started", "phase.failed"]
    assert sink.events[1].data["error"] == "boom"


def test_phase_and_progress_are_noops_without_an_emitter() -> None:
    with phase(None, "x") as info:
        info["k"] = 1
    progress(None, "x", 1, 2)  # must not raise


def _view() -> ui.PhaseView:
    return ui.PhaseView(Console(file=io.StringIO(), width=120, force_terminal=False, color_system=None), "index", "src")


def test_phase_view_tracks_progress_completion_and_scanner_children() -> None:
    v = _view()
    for kind, data in [
        ("phase.started", {"name": "parse"}),
        ("phase.progress", {"name": "parse", "current": 2, "total": 4, "label": "a.py"}),
    ]:
        v(Event(kind, data))
    assert v.phases["parse"]["state"] == "running" and (v.phases["parse"]["cur"], v.phases["parse"]["total"]) == (2, 4)

    v(Event("phase.finished", {"name": "parse", "seconds": 0.5, "files": 4}))
    assert v.phases["parse"]["state"] == "done" and v.phases["parse"]["detail"] == {"files": 4}

    v(Event("phase.started", {"name": "validate"}))
    v(Event("scanner.started", {"scanner": "ruff"}))
    v(Event("scanner.finished", {"scanner": "ruff", "findings": 2, "seconds": 0.1, "error": None}))
    assert v.phases["validate"]["children"]["ruff"] == {"state": "done", "findings": 2, "seconds": 0.1}

    v(Event("phase.failed", {"name": "validate", "seconds": 1.0, "error": "x"}))
    assert v.phases["validate"]["state"] == "failed"


def test_phase_view_renders_labels_and_bar() -> None:
    v = _view()
    v(Event("phase.started", {"name": "parse"}))
    v(Event("phase.progress", {"name": "parse", "current": 2, "total": 4, "label": "pkg/a.py"}))
    v.console.print(v._render())

    out = v.console.file.getvalue()
    assert "parse symbols" in out and "2/4" in out and "pkg/a.py" in out and "█" in out and "░" in out


@pytest.mark.parametrize("kwargs,mode", [({"stream": "off"}, "off"), ({"machine": True}, "off"), ({"stream": "json"}, "json")])
def test_open_run_modes(kwargs, mode) -> None:
    run = ui.open_run("t", **kwargs)

    assert run.mode == mode and (run.emitter is not None) is (mode == "json")


def test_open_run_auto_is_silent_when_not_a_tty(monkeypatch) -> None:
    monkeypatch.setattr(ui, "Console", lambda **kw: Console(file=io.StringIO(), force_terminal=False))

    assert ui.open_run("t", "auto").emitter is None


def test_open_run_rejects_unknown_stream() -> None:
    with pytest.raises(typer.Exit):
        ui.open_run("t", "bogus")


def test_run_result_only_writes_in_json_mode(capsys) -> None:
    ui.open_run("t", "off").result(a=1)
    assert capsys.readouterr().out == ""

    ui.open_run("t", "json").result(a=1)
    assert json.loads(capsys.readouterr().out) == {"type": "result", "a": 1}
