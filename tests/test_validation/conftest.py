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
