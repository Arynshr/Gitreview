"""Merkle-hashed incremental indexing.

File-level leaves only (v1 — per-symbol hashing is a documented follow-up,
not built now; see spec §2.1). All DB access here goes through
index_store's connection helper, not a private one — this module owns the
hashing/diffing algorithm, index_store.py owns persistence + the public API.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HashDiff:
    changed: set[str] = field(default_factory=set)
    added: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not (self.changed or self.added or self.removed)

    @property
    def touched(self) -> set[str]:
        """Files that need re-parsing: changed + added (not removed)."""
        return self.changed | self.added


@dataclass
class IndexRunResult:
    files_changed: int
    files_skipped: int
    duration_s: float
    skipped_entirely: bool = False


def leaf_hash(path: str) -> str:
    """sha256 of a tracked file's raw bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compute_leaf_hashes(paths: list[str]) -> dict[str, str]:
    return {p: leaf_hash(p) for p in paths}


def compute_root_hash(leaf_hashes: dict[str, str]) -> str:
    """sha256(sorted(f"{path}:{leaf_hash}" for all files)) — spec §2.2."""
    joined = "\n".join(sorted(f"{path}:{h}" for path, h in leaf_hashes.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def diff_against_stored(conn: sqlite3.Connection, current: dict[str, str]) -> HashDiff:
    """Compare current leaf hashes against the file_hashes table."""
    rows = conn.execute("SELECT path, content_hash FROM file_hashes").fetchall()
    stored = {r["path"]: r["content_hash"] for r in rows}

    current_paths = set(current)
    stored_paths = set(stored)

    added = current_paths - stored_paths
    removed = stored_paths - current_paths
    changed = {p for p in current_paths & stored_paths if current[p] != stored[p]}

    return HashDiff(changed=changed, added=added, removed=removed)


def get_last_root_hash(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT root_hash FROM index_runs ORDER BY id DESC LIMIT 1").fetchone()
    return row["root_hash"] if row else None


def record_run(
    conn: sqlite3.Connection, root_hash: str, files_changed: int, files_skipped: int
) -> None:
    conn.execute(
        "INSERT INTO index_runs (root_hash, files_changed, files_skipped) VALUES (?, ?, ?)",
        (root_hash, files_changed, files_skipped),
    )
    conn.commit()


def sync_file_hashes(conn: sqlite3.Connection, diff: HashDiff, current: dict[str, str]) -> None:
    """Persist file_hashes after a (partial or full) reindex."""
    for path in diff.removed:
        conn.execute("DELETE FROM file_hashes WHERE path = ?", (path,))
    for path in diff.touched:
        conn.execute(
            """INSERT INTO file_hashes (path, content_hash, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(path) DO UPDATE SET
                 content_hash = excluded.content_hash,
                 updated_at = excluded.updated_at""",
            (path, current[path]),
        )
    conn.commit()


def ensure_migrated(conn: sqlite3.Connection) -> None:
    """Idempotent migration for DBs built before this feature existed.

    CREATE TABLE IF NOT EXISTS in schema.sql handles new tables fine, but
    can't add a column to an existing `symbols` table — do that here.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(symbols)")}
    if "file_hash" not in cols:
        conn.execute("ALTER TABLE symbols ADD COLUMN file_hash TEXT")
        conn.commit()
