"""Unit: graph nodes report phases only when an emitter is attached."""

from __future__ import annotations

from gitscribe.core.graph import _traced
from gitscribe.events import Event


def test_no_emitter_returns_the_node_untouched() -> None:
    def fn(state):
        return {}

    assert _traced("generator", fn, None) is fn


def test_traced_node_emits_a_phase_pair_and_returns_the_node_result() -> None:
    seen: list[Event] = []

    wrapped = _traced("generator", lambda state: {"status": "success"}, seen.append)

    assert wrapped(object()) == {"status": "success"}
    assert [(e.type, e.data["name"]) for e in seen] == [("phase.started", "generator"), ("phase.finished", "generator")]
