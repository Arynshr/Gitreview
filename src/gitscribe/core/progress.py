"""Phase/progress events for long-running services (index, generate, commit).

Same `Event`/`emit` primitives as the validation layer, so one renderer
(`gitscribe.ui`) and one NDJSON contract cover every service.

  phase.started   name
  phase.progress  name, current, total, label
  phase.finished  name, seconds, **detail
  phase.failed    name, seconds, error
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from gitscribe.events import Emitter, emit


@contextmanager
def phase(emitter: Emitter | None, name: str) -> Iterator[dict]:
    """Yields a dict; anything put in it is reported on `phase.finished`
    (e.g. `info["files"] = 12`)."""
    emit(emitter, "phase.started", name=name)
    started = time.perf_counter()
    info: dict = {}
    try:
        yield info
    except BaseException as exc:
        emit(emitter, "phase.failed", name=name, seconds=round(time.perf_counter() - started, 3), error=str(exc)[:200])
        raise
    emit(emitter, "phase.finished", name=name, seconds=round(time.perf_counter() - started, 3), **info)


def progress(emitter: Emitter | None, name: str, current: int, total: int, label: str = "") -> None:
    emit(emitter, "phase.progress", name=name, current=current, total=total, label=label)
