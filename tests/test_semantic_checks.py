import sqlite3
from pathlib import Path

import pytest

from gitscribe.core.analysis import semantic_checks
from gitscribe.core.indexer import index_store


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """semantic_checks opens its connection through
    `index_store._get_connection()`, which reads `index_store.INDEX_DB_PATH`
    at call time - so that is the one attribute to redirect (as the other
    index-backed tests do).
    """
    db_path = tmp_path / "test_index.db"
    monkeypatch.setattr(index_store, "INDEX_DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.executescript(Path(index_store.SCHEMA_PATH).read_text())
    conn.commit()
    conn.close()
    return db_path


def insert_symbol(conn, sid, name, kind="function", file="a.py"):
    conn.execute(
        "INSERT INTO symbols (id, name, kind, file, lineno) VALUES (?, ?, ?, ?, 1)",
        (sid, name, kind, file),
    )


def insert_edge(conn, source_id, target_id, edge_type="calls", resolved=True):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, target_name, edge_type, resolved) VALUES (?, ?, ?, ?, ?)",
        (source_id, target_id, f"target_{target_id}", edge_type, resolved),
    )


# --- find_cycles ---

def test_find_cycles_detects_direct_cycle(temp_db):
    """A -> B -> A."""
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "a")
    insert_symbol(conn, 2, "b")
    insert_edge(conn, 1, 2)
    insert_edge(conn, 2, 1)
    conn.commit()
    conn.close()

    cycles = semantic_checks.find_cycles()
    assert len(cycles) >= 1
    assert cycles[0].symbols == ["a", "b"]


def test_find_cycles_no_false_positive_on_dag(temp_db):
    """A -> B -> C, no cycle."""
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "a")
    insert_symbol(conn, 2, "b")
    insert_symbol(conn, 3, "c")
    insert_edge(conn, 1, 2)
    insert_edge(conn, 2, 3)
    conn.commit()
    conn.close()

    cycles = semantic_checks.find_cycles()
    assert cycles == []


def test_find_cycles_ignores_unresolved_edges(temp_db):
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "a")
    insert_symbol(conn, 2, "b")
    insert_edge(conn, 1, 2, resolved=False)
    insert_edge(conn, 2, 1, resolved=False)
    conn.commit()
    conn.close()

    assert semantic_checks.find_cycles() == []


def test_find_cycles_self_loop(temp_db):
    """A -> A directly."""
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "a")
    insert_edge(conn, 1, 1)
    conn.commit()
    conn.close()

    cycles = semantic_checks.find_cycles()
    assert len(cycles) == 1
    assert cycles[0].symbols == ["a"]


# --- find_dead_code ---

def test_find_dead_code_no_incoming_edges(temp_db):
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "used")
    insert_symbol(conn, 2, "unused")
    insert_symbol(conn, 3, "caller")
    insert_edge(conn, 3, 1)  # caller -> used
    conn.commit()
    conn.close()

    dead = semantic_checks.find_dead_code()
    names = {d.name for d in dead}
    assert "unused" in names
    assert "used" not in names


def test_find_dead_code_excludes_entry_points(temp_db):
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "main")  # no callers, but is an entry point
    insert_symbol(conn, 2, "orphan")
    conn.commit()
    conn.close()

    dead = semantic_checks.find_dead_code(entry_point_names=["main"])
    names = {d.name for d in dead}
    assert "main" not in names
    assert "orphan" in names


def test_find_dead_code_unresolved_incoming_edge_still_counts_as_dead(temp_db):
    """An edge that exists but is unresolved shouldn't count as 'used' --
    consistent with find_cycles/blast_radius only trusting resolved edges.
    """
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "maybe_used")
    insert_symbol(conn, 2, "caller")
    insert_edge(conn, 2, 1, resolved=False)
    conn.commit()
    conn.close()

    dead = semantic_checks.find_dead_code()
    names = {d.name for d in dead}
    assert "maybe_used" in names


def test_find_dead_code_ignores_classes_and_imports(temp_db):
    """Only function/method symbols are dead-code candidates -- classes
    and imports aren't 'called' the same way, so they'd all false-positive.
    """
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "SomeClass", kind="class")
    insert_symbol(conn, 2, "os", kind="import")
    conn.commit()
    conn.close()

    dead = semantic_checks.find_dead_code()
    assert dead == []


# --- fan_in_out ---

def test_fan_in_out_above_threshold(temp_db):
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "hub")
    for i in range(2, 14):  # 12 callers
        insert_symbol(conn, i, f"caller_{i}")
        insert_edge(conn, i, 1)
    conn.commit()
    conn.close()

    hotspots = semantic_checks.fan_in_out(threshold=10)
    names = {h.name for h in hotspots}
    assert "hub" in names
    hub = next(h for h in hotspots if h.name == "hub")
    assert hub.fan_in == 12
    assert hub.fan_out == 0


def test_fan_in_out_below_threshold_excluded(temp_db):
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "quiet")
    insert_symbol(conn, 2, "caller")
    insert_edge(conn, 2, 1)
    conn.commit()
    conn.close()

    hotspots = semantic_checks.fan_in_out(threshold=10)
    assert hotspots == []


def test_fan_in_out_counts_only_resolved_edges(temp_db):
    conn = sqlite3.connect(temp_db)
    insert_symbol(conn, 1, "target")
    for i in range(2, 14):
        insert_symbol(conn, i, f"caller_{i}")
        insert_edge(conn, i, 1, resolved=False)  # all unresolved
    conn.commit()
    conn.close()

    hotspots = semantic_checks.fan_in_out(threshold=10)
    assert hotspots == []
