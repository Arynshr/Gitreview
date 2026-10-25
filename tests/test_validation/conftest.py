"""Shared fixtures for the validation-layer suite.

Nothing here spawns a real scanner, touches git, or talks to a model: every
external boundary (`shutil.which`, `DETERMINISTIC_RUNNERS`, the AI agent, the
checkpoint cache dir) is replaced per-test so failures are isolated to the
unit under test.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable

import pytest

from gitscribe.validation.deterministic import DETERMINISTIC_RUNNERS
from gitscribe.validation.graph import ValidationState
from gitscribe.validation.models import ChangeContext, Finding, ValidationRequest

DIFF = "diff --git a/a.py b/a.py\n+x = 1\n"


@pytest.fixture
def make_finding() -> Callable[..., Finding]:
    def _make(**overrides) -> Finding:
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
            "evidence": "test evidence",
            "remediation": "test remediation",
            "sources": ["deterministic"],
        }
        data.update(overrides)
        return Finding(**data)

    return _make


@pytest.fixture
def make_change() -> Callable[..., ChangeContext]:
    def _make(files=("a.py",), diff: str = DIFF) -> ChangeContext:
        return ChangeContext(base="base", head="head", diff=diff, files=list(files))

    return _make


@pytest.fixture
def make_request() -> Callable[..., ValidationRequest]:
    def _make(**overrides) -> ValidationRequest:
        return ValidationRequest(
            request_id="t",
            repo_root=".",
            base_ref="base",
            head_ref="head",
            **overrides,
        )

    return _make


@pytest.fixture
def make_state(make_request) -> Callable[..., ValidationState]:
    def _make(change: ChangeContext | None, **request_overrides) -> ValidationState:
        return ValidationState(request=make_request(**request_overrides), change=change)

    return _make


@pytest.fixture
def base_cfg() -> dict:
    return {
        "deterministic": {"enabled": True, "timeout_seconds": 5, "max_workers": 2},
        "ai": {"enabled": False},
        "fail_on": ["critical", "high"],
        "block_secrets": True,
        "fail_closed": True,
        "file_rules": [],
    }


@pytest.fixture
def all_tools_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every scanner binary 'exists' - preflight() all pass."""
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")


@pytest.fixture
def stub_runners(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Replaces every job runner with a clean no-op; returns the call log."""
    calls: list[tuple[str, list[str]]] = []

    def make(name: str):
        def runner(ctx, _timeout, _cfg):
            calls.append((name, list(ctx.files)))
            return []

        return runner

    for name in list(DETERMINISTIC_RUNNERS):
        monkeypatch.setitem(DETERMINISTIC_RUNNERS, name, make(name))

    return calls


# --- structured tracing ------------------------------------------------------
# `trace` is both an event emitter (pass it as `emitter=`) and a capture of
# every `gitscribe.*` log record. When a test fails, the full trace is
# attached to the failure report as NDJSON, so a red test shows the exact
# event/log sequence that led to it - no re-run with extra logging needed.

import json
import logging

from gitscribe.validation.events import Event


class Trace:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.records: list[dict] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, type_: str) -> list[dict]:
        return [e.data for e in self.events if e.type == type_]

    def dump(self) -> str:
        lines = [json.dumps({"kind": "event", **json.loads(e.to_json())}) for e in self.events]
        lines += [json.dumps({"kind": "log", **r}, default=str) for r in self.records]
        return "\n".join(lines) or "(empty trace)"


class _Capture(logging.Handler):
    def __init__(self, sink: list[dict]) -> None:
        super().__init__(level=logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        entry = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if hasattr(record, "event"):
            entry["event"] = record.event
        self.sink.append(entry)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    trace = getattr(item, "funcargs", {}).get("trace")
    if report.when == "call" and report.failed and trace is not None:
        report.sections.append(("structured trace (NDJSON)", trace.dump()))


@pytest.fixture
def trace() -> Trace:
    t = Trace()
    handler = _Capture(t.records)
    root = logging.getLogger("gitscribe")
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield t
    root.removeHandler(handler)
    root.setLevel(previous)
