from unittest.mock import patch

import numpy as np
import pytest

from gitscribe.core.indexer import index_store


@pytest.fixture
def temp_repo_with_index(tmp_path, monkeypatch):
    """A real on-disk Python repo + a real index DB, mirroring
    test_index_store.py's temp_index_db but with actual files to parse
    (reindex() needs real files, not just seeded DB rows)."""
    db_path = tmp_path / "index.db"
    monkeypatch.setattr(index_store, "INDEX_DB_PATH", db_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(
        "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n"
    )
    (repo / "b.py").write_text("from a import main\n\n\ndef run():\n    return main()\n")
    return repo


CFG = {"embedding": {"provider": "local", "model": "test"}}


def _reindex(repo, force=False):
    with patch(
        "gitscribe.core.indexer.index_store.embed_symbols",
        side_effect=lambda syms, cfg: [np.zeros(4) for _ in syms],
    ):
        return index_store.reindex(str(repo), CFG, force=force)


def test_full_reindex_populates_symbols_and_edges(temp_repo_with_index):
    result = _reindex(temp_repo_with_index)
    assert result.files_changed == 2
    assert result.files_skipped == 0
    assert not result.skipped_entirely

    conn = index_store._get_connection()
    symbols = conn.execute("SELECT name, kind FROM symbols").fetchall()
    functions = {r["name"] for r in symbols if r["kind"] in ("function", "method")}
    assert functions == {"helper", "main", "run"}
    conn.close()


def test_reindex_skips_entirely_when_nothing_changed(temp_repo_with_index):
    _reindex(temp_repo_with_index)
    result = _reindex(temp_repo_with_index)
    assert result.skipped_entirely
    assert result.files_changed == 0
    assert result.files_skipped == 2


def test_reindex_only_reprocesses_touched_file(temp_repo_with_index):
    _reindex(temp_repo_with_index)
    (temp_repo_with_index / "a.py").write_text(
        "def helper():\n    return 2  # changed\n\n\ndef main():\n    return helper()\n"
    )
    result = _reindex(temp_repo_with_index)
    assert result.files_changed == 1
    assert result.files_skipped == 1


def test_stale_edge_survives_and_reresolves_on_target_file_change(temp_repo_with_index):
    """Spec §2.4 step 6 — the case explicitly flagged as highest bug risk.
    b.py calls main() in a.py. Touching a.py must not silently drop the
    edge from b.py; it must be revalidated against main()'s new symbol id.
    """
    _reindex(temp_repo_with_index)

    conn = index_store._get_connection()
    before = conn.execute(
        """SELECT e.id, e.target_id FROM edges e
           JOIN symbols s1 ON s1.id = e.source_id
           JOIN symbols s2 ON s2.id = e.target_id
           WHERE s1.file LIKE '%b.py' AND s2.name = 'main' AND s2.kind = 'function'
             AND e.resolved = 1"""
    ).fetchone()
    conn.close()
    assert before is not None, (
        "expected a resolved cross-file edge b.py -> main() before the change"
    )

    # touch a.py (the edge's target file) without touching b.py at all
    (temp_repo_with_index / "a.py").write_text(
        "def helper():\n    return 99\n\n\ndef main():\n    return helper() + 1\n"
    )
    _reindex(temp_repo_with_index)

    conn = index_store._get_connection()
    after = conn.execute(
        """SELECT e.resolved, e.target_id FROM edges e WHERE e.id = ?""", (before["id"],)
    ).fetchone()
    conn.close()

    assert after is not None, (
        "edge should not be deleted by cascade when only the target file changes"
    )
    assert after["resolved"] == 1
    assert after["target_id"] != before["target_id"], (
        "should point at main()'s NEW symbol id, not the old one"
    )


def test_force_reindex_survives_existing_review_findings(temp_repo_with_index):
    """Regression test for the FK bug: review_findings.symbol_id must be
    ON DELETE SET NULL, or a --force reindex crashes once any finding
    references a symbol that gets deleted and re-inserted with a new id.
    """
    _reindex(temp_repo_with_index)

    conn = index_store._get_connection()
    sid = conn.execute("SELECT id FROM symbols WHERE name = 'helper'").fetchone()["id"]
    conn.execute(
        """INSERT INTO review_findings (symbol_id, source, severity, rule_or_reason, message)
           VALUES (?, 'lint', 'error', 'TEST', 'synthetic finding')""",
        (sid,),
    )
    conn.commit()
    conn.close()

    # must not raise sqlite3.IntegrityError
    result = _reindex(temp_repo_with_index, force=True)
    assert not result.skipped_entirely

    conn = index_store._get_connection()
    finding = conn.execute("SELECT symbol_id FROM review_findings").fetchone()
    conn.close()
    assert finding is not None, "finding should survive the reindex, not be deleted"
    assert finding["symbol_id"] is None, "should be nulled out, not left dangling"


def test_needs_reindex_reflects_current_state(temp_repo_with_index):
    assert index_store.needs_reindex(str(temp_repo_with_index)) is True
    _reindex(temp_repo_with_index)
    assert index_store.needs_reindex(str(temp_repo_with_index)) is False
    (temp_repo_with_index / "a.py").write_text("def helper():\n    return 3\n")
    assert index_store.needs_reindex(str(temp_repo_with_index)) is True
