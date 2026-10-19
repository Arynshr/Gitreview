"""
Latest-result persistence for `gitscribe verify` (spec §10.2, simplified).

Replaces both the unbounded per-key file cache in `core/hooks.py` and the
SQLite-backed LangGraph saver this module previously built. One small JSON
file - `.git/gitscribe-cache/validation_latest.json` - holds only the most
recent run and is overwritten (atomically) on every run, so disk and memory
stay bounded regardless of how many runs, refs or configs are seen.

The §10.2 invariants are kept:
- key = `graph.derive_thread_id()` (diff hash + cfg fingerprint + sandboxed/
  force_ai), i.e. the exact inputs the old cache key used;
- the dirty-working-tree gate guards every read and decides reusability at
  write time (a result computed over uncommitted edits is persisted but never
  marked reusable, so it can't be replayed as a stale PASS after the tree
  is cleaned);
- results with errors, or partial (--path/--mode) runs, are never reusable;
- no diff text is stored - `ValidationResult` carries findings only.

Persistence is best-effort: a failure here never fails a validation run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path

from gitscribe.core.hooks import CACHE_DIR, _clean_working_tree
from gitscribe.validation.graph import derive_thread_id
from gitscribe.validation.models import ValidationResult

logger = logging.getLogger("gitscribe.validation.checkpoint")

LATEST_FILE = "validation_latest.json"


def latest_path() -> Path:
    return CACHE_DIR / LATEST_FILE


def load_reusable(cfg: dict, base: str, head: str, *extra: object) -> ValidationResult | None:
    """The persisted result, only if it is safe to replay for these inputs.
    Any failure (git error, missing/corrupt file, schema drift) is a miss,
    never an exception - reading the cache must not fail a run.
    """
    try:
        if not _clean_working_tree():
            return None

        data = json.loads(latest_path().read_text(encoding="utf-8"))

        if not data.get("reusable") or data.get("key") != derive_thread_id(cfg, base, head, *extra):
            return None

        return ValidationResult.model_validate(data["result"])
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("ignoring unusable persisted validation result: %s", exc)
        return None


def save_latest(
    result: ValidationResult,
    cfg: dict,
    base: str,
    head: str,
    *extra: object,
    cacheable: bool,
) -> None:
    """Overwrites the single latest-result file. `cacheable` is False for
    partial runs; reusability is further limited to error-free results on a
    clean tree (see module docstring). Leaves no temp file behind on failure.
    """
    tmp: Path | None = None

    try:
        payload = {
            "key": derive_thread_id(cfg, base, head, *extra),
            "reusable": cacheable and not result.errors and _clean_working_tree(),
            "result": result.model_dump(mode="json"),
        }

        path = latest_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("could not persist latest validation result: %s", exc)

        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
