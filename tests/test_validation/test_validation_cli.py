"""`gitscribe verify` run orchestration (`validation/cli.py::_run_one`):
cache consult/persist decisions. Git, the graph and the cache are faked.
"""

from __future__ import annotations

import pytest

from gitscribe.validation import cli
from gitscribe.validation.models import ValidationResult


@pytest.fixture
def harness(monkeypatch, make_change):
    calls = {"load": 0, "resolve": 0, "validate": 0, "saved": []}
    fresh = ValidationResult(status="PASS", timings={"fresh": 1.0})
    state = {"cached": None}

    def load(cfg, base, head, *extra):
        calls["load"] += 1
        return state["cached"]

    def resolve(**kwargs):
        calls["resolve"] += 1
        return make_change()

    def validate(context, cfg, **kwargs):
        calls["validate"] += 1
        return fresh

    def save(result, cfg, base, head, *extra, cacheable):
        calls["saved"].append((result, extra, cacheable))

    monkeypatch.setattr(cli, "load_reusable", load)
    monkeypatch.setattr(cli, "resolve_change", resolve)
    monkeypatch.setattr(cli, "validate_change", validate)
    monkeypatch.setattr(cli, "save_latest", save)

    return calls, state, fresh


def test_cache_hit_skips_resolution_validation_and_persistence(harness) -> None:
    calls, state, _ = harness
    state["cached"] = ValidationResult(status="PASS", timings={"cached": 1.0})

    result = cli._run_one("b", "h", {})

    assert result.timings == {"cached": 1.0}
    assert (calls["resolve"], calls["validate"], calls["saved"]) == (0, 0, [])


def test_cache_miss_validates_then_persists_as_cacheable(harness) -> None:
    calls, _, fresh = harness

    result = cli._run_one("b", "h", {}, force_ai=True, sandboxed=False)

    assert result is fresh
    assert calls["validate"] == 1
    [(saved, extra, cacheable)] = calls["saved"]
    assert saved is fresh
    assert extra == (False, True)  # (sandboxed, force_ai) - the key inputs, in order
    assert cacheable is True


@pytest.mark.parametrize("narrowing", [{"paths": ["a.py"]}, {"force_mode": "static"}])
def test_partial_runs_never_consult_the_cache_and_are_not_cacheable(harness, narrowing) -> None:
    calls, state, _ = harness
    state["cached"] = ValidationResult(status="PASS", timings={"cached": 1.0})

    result = cli._run_one("b", "h", {}, **narrowing)

    assert result.timings == {"fresh": 1.0}  # the poisoned cache entry was ignored
    assert calls["load"] == 0
    assert calls["saved"][0][2] is False
