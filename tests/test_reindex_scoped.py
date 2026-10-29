"""Integration: `reindex(paths=...)` only touches its scope, emits phases,
and is deterministic."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from gitscribe.core.indexer import index_store
from gitscribe.events import Event

CFG = {"embedding": {"provider": "local", "model": "test"}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(index_store, "INDEX_DB_PATH", tmp_path / "index.db")
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "other").mkdir()
    (r / "pkg" / "a.py").write_text("def helper():\n    return 1\n")
    (r / "other" / "b.py").write_text("def run():\n    return 2\n")
    monkeypatch.chdir(r)
    return r


def _reindex(repo, **kw):
    with patch("gitscribe.core.indexer.index_store.embed_symbols", side_effect=lambda s, c: [np.zeros(4) for _ in s]):
        return index_store.reindex(str(repo), CFG, **kw)


def _names() -> set[str]:
    conn = index_store._get_connection()
    try:
        return {r["name"] for r in conn.execute("SELECT name FROM symbols")}
    finally:
        conn.close()


def test_scoped_run_indexes_only_the_selected_folder(repo) -> None:
    result = _reindex(repo, paths=["pkg"])

    assert _names() == {"helper"} and result.files_changed == 1


def test_scoped_run_never_removes_files_outside_its_scope(repo) -> None:
    _reindex(repo)  # full index: helper + run

    (repo / "pkg" / "a.py").write_text("def helper():\n    return 1\n\n\ndef extra():\n    return 3\n")
    _reindex(repo, paths=["pkg"])

    assert _names() == {"helper", "extra", "run"}


def test_scoped_run_with_nothing_changed_skips(repo) -> None:
    _reindex(repo, paths=["pkg"])

    assert _reindex(repo, paths=["pkg"]).skipped_entirely is True


def test_scoped_run_cannot_make_a_later_full_run_skip_wrongly(repo) -> None:
    _reindex(repo)                                              # full: H1 recorded
    original = (repo / "pkg" / "a.py").read_text()
    (repo / "pkg" / "a.py").write_text("def changed():\n    return 9\n")
    _reindex(repo, paths=["pkg"])                               # index now has `changed`
    (repo / "pkg" / "a.py").write_text(original)                # root hash equals H1 again

    result = _reindex(repo)

    assert result.skipped_entirely is False and "helper" in _names() and "changed" not in _names()


def test_phases_are_emitted_in_order_with_per_file_progress(repo) -> None:
    seen: list[Event] = []

    _reindex(repo, emitter=seen.append)

    started = [e.data["name"] for e in seen if e.type == "phase.started"]
    assert started == ["discover", "hash", "parse", "link", "embed", "finalize"]
    progress = [e for e in seen if e.type == "phase.progress"]
    assert [e.data["current"] for e in progress] == [1, 2] and all(e.data["total"] == 2 for e in progress)


def test_symbol_ids_follow_sorted_file_order(repo) -> None:
    _reindex(repo)

    conn = index_store._get_connection()
    rows = conn.execute("SELECT file FROM symbols ORDER BY id").fetchall()
    conn.close()
    files = [r["file"] for r in rows]
    assert files == sorted(files)
