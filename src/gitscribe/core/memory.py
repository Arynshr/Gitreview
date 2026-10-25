"""
Local sqlite memory layer -- one DB per user directory
"""

import sqlite3
from pathlib import Path

DB_PATH = Path.cwd() / "Storage" / "gitscribe.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
    id  INTEGER PRIMARY KEY,
    branch TEXT,
    title TEXT,
    body TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """Idempotent ALTER TABLE for a single column.

    CREATE TABLE IF NOT EXISTS in SCHEMA handles brand-new tables fine, but
    is a no-op against a `prs` table that already exists on disk from an
    older version -- it can't add a column to it. Same gap that
    indexer/merkle.py's ensure_migrated() closes for the index DB; this is
    the equivalent for the PR-memory DB.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        conn.commit()


# (table, column, coltype) triples for columns added after the original
# SCHEMA shipped. Empty today -- append here, not as ad-hoc ALTERs
# elsewhere, whenever `prs` gains a column on an existing table.
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = []


def ensure_migrated(conn: sqlite3.Connection) -> None:
    """Apply any column migrations not covered by CREATE TABLE IF NOT EXISTS.

    Safe to call on a fresh DB (no-op) or an existing one (adds only what's
    missing); never touches existing rows.
    """
    for table, column, coltype in COLUMN_MIGRATIONS:
        _add_column_if_missing(conn, table, column, coltype)


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    ensure_migrated(conn)
    return conn


def init_schema():
    conn = get_connection()
    conn.executescript(SCHEMA)
    ensure_migrated(conn)
    conn.commit()
    conn.close()


def save_pr(branch: str, title: str, body: str) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO prs (branch, title, body) VALUES (?, ?, ?)",
        (branch, title, body),
    )
    conn.commit()
    pr_id = cur.lastrowid
    conn.close()
    return pr_id


def fetch_prs_by_branch_prefix(prefix: str, limit: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, branch, title, body FROM prs WHERE branch LIKE ? "
        "ORDER BY created_at DESC LIMIT ?",
        (f"{prefix}%", limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_recent_prs(limit: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, branch, title, body FROM prs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
