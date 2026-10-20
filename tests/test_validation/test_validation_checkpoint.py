"""Latest-result persistence (`validation/checkpoint.py`).

Invariants under test: exactly one bounded file; reuse only for a clean tree,
an error-free whole-change result and a matching key; and persistence
failures never propagate into a validation run (fault isolation), leaving no
temp-file residue.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from gitscribe.validation import checkpoint
from gitscribe.validation.checkpoint import latest_path, load_reusable, save_latest
from gitscribe.validation.models import AnalysisError, ValidationResult

CHECKPOINT_LOGGER = "gitscribe.validation.checkpoint"
CFG: dict = {}


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Redirects the cache dir to tmp and controls tree-cleanliness / key."""
    state = {"clean": True, "key": "k1"}
    monkeypatch.setattr(checkpoint, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(checkpoint, "_clean_working_tree", lambda: state["clean"])
    monkeypatch.setattr(checkpoint, "derive_thread_id", lambda cfg, base, head, *extra: state["key"])
    return SimpleNamespace(state=state, dir=tmp_path / "cache")


def _pass(**kw) -> ValidationResult:
    return ValidationResult(status="PASS", **kw)


def _stored() -> dict:
    return json.loads(latest_path().read_text(encoding="utf-8"))


def test_roundtrip_reuses_result_for_identical_inputs(store, make_finding) -> None:
    result = _pass(findings=[make_finding(severity="low")], diagnostics={"jobs": ["ruff"]})

    save_latest(result, CFG, "b", "h", cacheable=True)

    assert load_reusable(CFG, "b", "h") == result


def test_key_mismatch_is_a_miss(store) -> None:
    save_latest(_pass(), CFG, "b", "h", cacheable=True)
    store.state["key"] = "k2"  # diff/config/flags changed

    assert load_reusable(CFG, "b", "h") is None


def test_dirty_tree_at_write_is_persisted_but_never_reusable(store) -> None:
    store.state["clean"] = False
    save_latest(_pass(), CFG, "b", "h", cacheable=True)
    store.state["clean"] = True  # tree later cleaned: must not replay a stale PASS

    assert _stored()["reusable"] is False
    assert load_reusable(CFG, "b", "h") is None


def test_dirty_tree_at_read_is_a_miss(store) -> None:
    save_latest(_pass(), CFG, "b", "h", cacheable=True)
    store.state["clean"] = False

    assert load_reusable(CFG, "b", "h") is None


def test_result_with_errors_is_never_reusable(store) -> None:
    result = ValidationResult(status="FAIL", errors=[AnalysisError(source="ruff", message="down")])

    save_latest(result, CFG, "b", "h", cacheable=True)

    assert _stored()["reusable"] is False
    assert load_reusable(CFG, "b", "h") is None


def test_partial_run_is_never_reusable(store) -> None:
    save_latest(_pass(), CFG, "b", "h", cacheable=False)  # --path / --mode run

    assert load_reusable(CFG, "b", "h") is None


def test_only_the_latest_result_is_kept(store) -> None:
    for i in range(5):
        store.state["key"] = f"k{i}"
        save_latest(_pass(timings={"n": float(i)}), CFG, "b", "h", cacheable=True)

    assert [p.name for p in store.dir.iterdir()] == ["validation_latest.json"]  # bounded, no tmp residue
    assert _stored()["key"] == "k4"
    assert load_reusable(CFG, "b", "h").timings == {"n": 4.0}


@pytest.mark.parametrize(
    "content",
    ["", "{not json", "[]", '{"key": "k1", "reusable": true}', '{"key": "k1", "reusable": true, "result": {"status": "MAYBE"}}'],
)
def test_corrupt_or_drifted_file_is_a_miss_not_an_error(store, content) -> None:
    store.dir.mkdir(parents=True)
    latest_path().write_text(content, encoding="utf-8")

    assert load_reusable(CFG, "b", "h") is None


def test_missing_file_is_a_miss(store) -> None:
    assert load_reusable(CFG, "b", "h") is None


def test_git_failure_while_reading_is_a_miss(store, monkeypatch) -> None:
    save_latest(_pass(), CFG, "b", "h", cacheable=True)

    def boom(*a, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(checkpoint, "derive_thread_id", boom)

    assert load_reusable(CFG, "b", "h") is None


def test_git_failure_while_writing_is_logged_and_isolated(store, monkeypatch, caplog) -> None:
    def boom(*a, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(checkpoint, "derive_thread_id", boom)

    with caplog.at_level(logging.WARNING, logger=CHECKPOINT_LOGGER):
        save_latest(_pass(), CFG, "b", "h", cacheable=True)  # must not raise

    assert any("could not persist" in r.getMessage() for r in caplog.records)
    assert not latest_path().exists()


def test_failed_atomic_replace_is_logged_and_leaves_no_temp_file(store, monkeypatch, caplog) -> None:
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(checkpoint.os, "replace", boom)

    with caplog.at_level(logging.WARNING, logger=CHECKPOINT_LOGGER):
        save_latest(_pass(), CFG, "b", "h", cacheable=True)  # must not raise

    assert any("disk full" in r.getMessage() for r in caplog.records)
    assert list(store.dir.iterdir()) == []


def test_failed_overwrite_preserves_previous_result(store, monkeypatch) -> None:
    save_latest(_pass(timings={"n": 1.0}), CFG, "b", "h", cacheable=True)

    def boom(src, dst):
        raise OSError("nope")

    with monkeypatch.context() as scoped:
        scoped.setattr(checkpoint.os, "replace", boom)
        save_latest(_pass(timings={"n": 2.0}), CFG, "b", "h", cacheable=True)  # must not raise

    assert _stored()["result"]["timings"] == {"n": 1.0}  # atomicity: old result intact
    assert [p.name for p in store.dir.iterdir()] == ["validation_latest.json"]


def test_persisted_payload_contains_no_diff_text(store, make_finding) -> None:
    save_latest(_pass(findings=[make_finding()]), CFG, "b", "h", cacheable=True)

    assert set(_stored()) == {"key", "reusable", "result"}
    assert "diff" not in _stored()["result"]
