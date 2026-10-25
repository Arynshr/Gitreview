"""
Structural signal from the code graph — cycles, dead code
(via reachability), high fan-in/out.
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel

from gitscribe.core.indexer.index_store import _get_connection as _index_conn


class CycleFinding(BaseModel):
    symbols: list[str]  # names, in cycle order


class DeadCodeFinding(BaseModel):
    symbol_id: int
    name: str
    file: str


class FanFinding(BaseModel):
    symbol_id: int
    name: str
    file: str
    fan_in: int
    fan_out: int


def _connect() -> sqlite3.Connection:
    """Delegates to index_store's connection helper -- keeps schema init
    and pragmas (foreign keys, WAL, busy_timeout) in one place instead of
    duplicating a second raw sqlite3.connect() against the same DB file,
    which is what this module's own docstring already promises."""
    return _index_conn()


def find_cycles(max_depth: int = 6) -> list[CycleFinding]:
    """Detects call cycles via recursive CTE path tracking. Bounded by
    max_depth to keep this cheap — a cycle deeper than that is rare and
    arguably not the actionable case this check exists for.
    """
    conn = _connect()
    rows = conn.execute(
        """
        WITH RECURSIVE path(start_id, current_id, depth, trail) AS (
            SELECT source_id, target_id, 1, CAST(source_id AS TEXT) || ',' || CAST(target_id AS TEXT)
            FROM edges WHERE edge_type = 'calls' AND resolved = 1
            UNION ALL
            SELECT p.start_id, e.target_id, p.depth + 1, p.trail || ',' || CAST(e.target_id AS TEXT)
            FROM edges e JOIN path p ON e.source_id = p.current_id
            WHERE e.edge_type = 'calls' AND e.resolved = 1 AND p.depth < ?
        )
        SELECT DISTINCT trail FROM path WHERE current_id = start_id
        """,
        (max_depth,),
    ).fetchall()

    cycles = []
    seen_sets = set()
    for row in rows:
        ids = [int(x) for x in row["trail"].split(",")]
        ids = ids[:-1]  # trail always loops back to the start node (trail[0] == trail[-1]);
        # drop the redundant closing entry so a 2-node cycle reads
        # ['a', 'b'] not ['a', 'b', 'a'], and a self-loop reads ['a'] not ['a', 'a']
        key = frozenset(ids)
        if key in seen_sets:
            continue
        seen_sets.add(key)
        names = conn.execute(
            f"SELECT id, name FROM symbols WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
        name_map = {r["id"]: r["name"] for r in names}
        cycles.append(CycleFinding(symbols=[name_map.get(i, str(i)) for i in ids]))

    conn.close()
    return cycles


def find_dead_code(entry_point_names: list[str] | None = None) -> list[DeadCodeFinding]:
    """Symbols with zero resolved incoming edges. Heuristic, not proof of
    deadness — CLI entry points, test functions, and dynamically-invoked
    code (decorators, __getattr__) will false-positive. `entry_point_names`
    lets callers exclude known roots (e.g. cli.py command functions).
    """
    conn = _connect()
    excluded = entry_point_names or []
    placeholders = ",".join("?" * len(excluded)) if excluded else "''"
    rows = conn.execute(
        f"""
        SELECT s.id, s.name, s.file FROM symbols s
        WHERE s.kind IN ('function', 'method')
        AND s.name NOT IN ({placeholders})
        AND NOT EXISTS (
            SELECT 1 FROM edges e
            WHERE e.target_id = s.id AND e.resolved = 1
        )
        """,
        excluded,
    ).fetchall()
    conn.close()
    return [DeadCodeFinding(symbol_id=r["id"], name=r["name"], file=r["file"]) for r in rows]


def fan_in_out(threshold: int = 10) -> list[FanFinding]:
    """High fan-in/out symbols — change-risk hotspots. Threshold is a
    starting heuristic, not tuned; expose it so risk_classifier can
    adjust per-repo.
    """
    conn = _connect()
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.file,
            (SELECT COUNT(*) FROM edges e WHERE e.target_id = s.id AND e.resolved = 1) AS fan_in,
            (SELECT COUNT(*) FROM edges e WHERE e.source_id = s.id AND e.resolved = 1) AS fan_out
        FROM symbols s
        WHERE s.kind IN ('function', 'method', 'class')
        """
    ).fetchall()
    conn.close()

    return [
        FanFinding(
            symbol_id=r["id"],
            name=r["name"],
            file=r["file"],
            fan_in=r["fan_in"],
            fan_out=r["fan_out"],
        )
        for r in rows
        if r["fan_in"] >= threshold or r["fan_out"] >= threshold
    ]
