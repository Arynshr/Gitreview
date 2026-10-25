"""
Structured logging for LLM calls: token usage + latency.
"""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

LOG_PATH = Path.cwd() / "Storage" / "logs" / "llm_calls.jsonl"

# General-purpose library logger for non-LLM diagnostics (e.g. parser.py
# skipping an unparseable file). Separate from _file_logger below, which is
# a structured JSONL sink specifically for LLM call records - conflating
# the two would mean free-text messages landing in a file meant to be
# machine-parsed one-record-per-line. Not configured with a handler here;
# callers/CLI entry points attach one if they want output (propagates to
# root by default, i.e. stderr via logging's lastResort handler).
logger = logging.getLogger("gitscribe")

_file_logger = logging.getLogger("gitscribe.core.telemetry")
_file_logger.propagate = False


def _ensure_file_handler() -> None:
    if _file_logger.handlers:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _file_logger.addHandler(handler)
    _file_logger.setLevel(logging.INFO)


class LLMCallRecord(BaseModel):
    node: str
    model: str
    latency_s: float
    success: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    timestamp: str = ""

    def summary_line(self) -> str:
        mark = "\u2713" if self.success else "\u2717"
        tokens = (
            f"{self.input_tokens}\u2192{self.output_tokens} tokens"
            if self.input_tokens is not None and self.output_tokens is not None
            else "tokens N/A"
        )
        return f"{mark} {self.node} via {self.model} \u2014 {self.latency_s:.2f}s {tokens}"


_session: list[LLMCallRecord] = []


def session_records() -> list[LLMCallRecord]:
    return list(_session)


def reset_session() -> None:
    _session.clear()


def log_llm_call(
    node: str, model: str, latency_s: float, usage: dict | None, success: bool
) -> LLMCallRecord:
    record = LLMCallRecord(
        node=node,
        model=model,
        latency_s=round(latency_s, 3),
        success=success,
        input_tokens=(usage or {}).get("input_tokens"),
        output_tokens=(usage or {}).get("output_tokens"),
        timestamp=datetime.now(UTC).isoformat(),
    )
    _session.append(record)
    try:
        _ensure_file_handler()
        _file_logger.info(record.model_dump_json())
    except Exception:
        logger.debug("failed to write LLM call record to %s", LOG_PATH, exc_info=True)
    return record


@contextmanager
def timed_llm_call(node: str, model: str):
    """Usage: with timed_llm_call("generator", model_name) as ctx: ai_msg = llm.invoke(...); ctx["usage"] = ai_msg.usage_metadata"""
    import time

    start = time.perf_counter()
    ctx: dict = {"usage": None, "success": False}
    try:
        yield ctx
        ctx["success"] = True
    finally:
        latency = time.perf_counter() - start
        log_llm_call(node, model, latency, ctx.get("usage"), ctx["success"])
