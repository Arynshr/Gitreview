"""Typed progress events for the validation pipeline.

The graph emits; renderers (`report.py`) consume. `emit` is optional
everywhere - with no emitter, validation behaves exactly as before.

Event types and their `data` keys:
  plan.ready       jobs: list[str]
  scanner.started  scanner
  scanner.finished scanner, findings, seconds, error (str | None)
  finding.found    scanner, severity, category, file, line, message
  gate.decision    status, reason, findings, errors, severities: dict
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    type: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "ts": round(self.ts, 3), **self.data})


Emitter = Callable[[Event], None]


def emit(emitter: Emitter | None, type_: str, **data: object) -> None:
    """Never lets a renderer failure break validation. Every event is also
    logged at DEBUG as a structured record (`record.event` is the payload
    dict), so `--debug`/log handlers and tests get the same trace the
    renderers see, even with no emitter attached."""
    logger.debug("validation.event %s", type_, extra={"event": {"type": type_, **data}})
    if emitter is None:
        return
    try:
        emitter(Event(type_, data))
    except Exception:
        logger.debug("event emitter failed for %s", type_, exc_info=True)
