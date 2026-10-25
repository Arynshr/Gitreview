"""Integration: `gitscribe verify` output modes (--stream, --view, --json).

`_run_one` is stubbed (it has its own tests in test_validation_cli.py); what
is exercised here is the real command wiring: option validation, emitter
selection, report rendering, and exit codes.
"""

from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from gitscribe.validation import cli, report
from gitscribe.validation.events import Event
from gitscribe.validation.models import ChangeContext, Finding, ValidationResult

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n"


def _finding(**overrides) -> Finding:
    """Local on purpose: this module has no conftest dependency, so it works
    wherever it is placed (tests/ or tests/test_validation/)."""
    data = {
        "id": "t",
        "source": "deterministic",
        "category": "security",
        "rule_id": "TEST",
        "severity": "high",
        "confidence": "high",
        "file": "a.py",
        "line": 1,
        "message": "test finding",
        "evidence": "e",
        "remediation": "r",
        "sources": ["deterministic"],
    }
    return Finding(**{**data, **overrides})


EVENTS = [("plan.ready", {"jobs": ["ruff"]}), ("gate.decision", {"status": "FAIL", "reason": "r"})]


@pytest.fixture
def app(monkeypatch) -> typer.Typer:
    monkeypatch.setattr(cli, "find_config_path", lambda *a, **k: None)
    a = typer.Typer()

    @a.command()
    def noop() -> None:  # keeps Typer in sub-command mode so "verify" is addressable
        ...

    cli.register_verify_command(a, config_loader=lambda *args, **kw: {"enabled": True})
    return a


@pytest.fixture
def stub_run(monkeypatch):
    seen: dict = {}

    def install(result: ValidationResult, events=EVENTS) -> dict:
        def fake(base, head, cfg, **kw):
            seen["emitter"] = kw.get("emitter")
            for kind, data in events:
                if kw.get("emitter"):
                    kw["emitter"](Event(kind, data))
            return result

        monkeypatch.setattr(cli, "_run_one", fake)
        return seen

    return install


def _invoke(app, *args):
    return CliRunner().invoke(app, ["verify", *args])


def test_stream_json_emits_events_then_a_result_line(app, stub_run) -> None:
    stub_run(
        ValidationResult(status="FAIL", findings=[_finding()], diagnostics={"gate_reason": "r"})
    )

    result = _invoke(app, "--stream", "json")

    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [line["type"] for line in lines] == ["plan.ready", "gate.decision", "result"]
    assert lines[-1]["status"] == "FAIL" and lines[-1]["gate_reason"] == "r"
    assert lines[-1]["findings"][0]["message"] == "test finding"
    assert result.exit_code == 1


def test_piped_default_has_no_live_view_but_prints_the_summary(app, stub_run) -> None:
    seen = stub_run(ValidationResult(status="PASS"))

    result = _invoke(app)

    assert seen["emitter"] is None  # CliRunner is not a TTY -> auto resolves to off
    assert "GitScribe Verification" in result.stdout and "PASS" in result.stdout
    assert result.exit_code == 0


def test_stream_live_attaches_the_live_renderer(app, stub_run) -> None:
    seen = stub_run(ValidationResult(status="PASS"), events=[])

    _invoke(app, "--stream", "live")

    assert isinstance(seen["emitter"], report.LiveRenderer)


def test_json_output_stays_machine_readable_and_disables_streaming(app, stub_run) -> None:
    seen = stub_run(ValidationResult(status="PASS"))

    result = _invoke(app, "--json", "--stream", "live")

    assert seen["emitter"] is None
    assert json.loads(result.stdout)["status"] == "PASS"


def test_diff_view_annotates_findings_inline(app, stub_run, monkeypatch) -> None:
    stub_run(ValidationResult(status="FAIL", findings=[_finding(line=1, message="inline-me")]))
    monkeypatch.setattr(
        cli,
        "resolve_change",
        lambda **kw: ChangeContext(base="base", head="head", diff=DIFF, files=["a.py"]),
    )

    result = _invoke(app, "--view", "diff")

    assert "▲ [HIGH] security: inline-me" in result.stdout
    assert result.exit_code == 1


@pytest.mark.parametrize("args", [("--stream", "bogus"), ("--view", "bogus")])
def test_invalid_option_values_exit_with_a_clear_message(app, stub_run, args) -> None:
    stub_run(ValidationResult(status="PASS"))

    result = _invoke(app, *args)

    assert result.exit_code == 1 and "--stream must be" in result.output
