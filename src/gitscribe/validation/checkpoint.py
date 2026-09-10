"""
Insertion Point C: checkpointer backing the validation StateGraph.

**Why AsyncSqliteSaver**: matches the local-first pattern
`core/hooks.py`'s file cache (`.git/gitscribe-cache/`) already uses - no
server dependency (same reasoning the Roadmap gives for rejecting Neo4j).
Async, not the sync `SqliteSaver`, because `validation/graph.py` invokes
via `ainvoke()`. New dependency: `langgraph-checkpoint-sqlite`.

**Scoping**: `SerializerProtocol` only sees the object being serialized,
not its channel name, so scoping is type-based.
`ScopedCheckpointSerializer` truncates `ChangeContext.diff` before it's
persisted; everything else passes through to `JsonPlusSerializer`
unchanged. `ValidationState.context` isn't scoped - it has no real shape
yet. The AI node's own message history isn't scoped either -
`validation/ai.py` still runs entirely inside `orchestrator.py`'s sync
call and never touches this checkpointer; revisit once Insertion Point B
puts it in the graph.

**Burn-in**: must run in parallel with the file cache before cutover, not
replace it immediately. `dual_write_burn_in()` writes to both and records
whether the checkpoint write succeeded, without ever failing the run on
that basis. Still not wired into `verify` - the graph now produces real
`ValidationResult`s (see validation/graph.py), so the comparison is
meaningful, but cutover should still wait for an explicit burn-in period
rather than happening in the same change that made it possible.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from gitscribe.core.hooks import CACHE_DIR, store_cached_validation
from gitscribe.validation.models import ChangeContext

logger = logging.getLogger("gitscribe.validation.checkpoint")

_DIFF_PREVIEW_CHARS = 500


class ScopedCheckpointSerializer:
    """Wraps `JsonPlusSerializer`; redacts `ChangeContext.diff` down to a
    short preview before it's persisted. See module docstring.
    """

    def __init__(self, inner: SerializerProtocol | None = None) -> None:
        self._inner: SerializerProtocol = inner or JsonPlusSerializer()

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        if isinstance(obj, ChangeContext):
            obj = obj.model_copy(update={"diff": self._scope_diff(obj.diff)})

        return self._inner.dumps_typed(obj)

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        return self._inner.loads_typed(data)

    @staticmethod
    def _scope_diff(diff: str) -> str:
        if len(diff) <= _DIFF_PREVIEW_CHARS:
            return diff

        truncated = len(diff) - _DIFF_PREVIEW_CHARS
        return (
            diff[:_DIFF_PREVIEW_CHARS]
            + f"\n... [{truncated} chars truncated for checkpoint storage - "
            "re-resolve from git for the full diff, do not rely on a checkpointed copy]"
        )


def checkpoint_db_path(repo_root: str | Path = ".") -> Path:
    """`.git/gitscribe-cache/validation_checkpoints.sqlite` - same
    `CACHE_DIR` the file cache uses, so both coexist during burn-in.
    """
    return Path(repo_root) / CACHE_DIR / "validation_checkpoints.sqlite"


def build_checkpointer(repo_root: str | Path = "."):
    """Caller must call `dirty_tree_gate()` before using this for a
    read/write - this only constructs the saver. Use as
    `async with build_checkpointer() as saver:`.
    """
    db_path = checkpoint_db_path(repo_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return AsyncSqliteSaver.from_conn_string(str(db_path), serde=ScopedCheckpointSerializer())


async def dual_write_burn_in(
    cfg: dict,
    base: str,
    head: str,
    result_dict: dict,
    *extra: object,
) -> dict:
    """Writes to the file cache (unchanged, authoritative) and reports
    whether an equivalent checkpoint write would have succeeded, without
    ever failing the run on that basis. Returns a diagnostics dict for
    `ValidationState.diagnostics`, not a validation finding.
    """
    store_cached_validation(cfg, base, head, result_dict, *extra)

    diagnostics: dict[str, Any] = {"checkpoint_burn_in": {"attempted": True}}

    try:
        async with build_checkpointer() as saver:
            thread_id = f"burnin:{base}:{head}"
            config = {"configurable": {"thread_id": thread_id}}
            checkpoint = {
                "v": 1,
                "ts": "",
                "id": thread_id,
                "channel_values": {"result": result_dict},
                "channel_versions": {},
                "versions_seen": {},
            }
            await saver.aput(config, checkpoint, {}, {})

        diagnostics["checkpoint_burn_in"]["write_ok"] = True
    except Exception as exc:  # noqa: BLE001 - burn-in must never fail the run
        logger.warning("checkpoint burn-in write failed (file cache is unaffected): %s", exc)
        diagnostics["checkpoint_burn_in"]["write_ok"] = False
        diagnostics["checkpoint_burn_in"]["error"] = str(exc)

    return diagnostics
