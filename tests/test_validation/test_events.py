"""Unit: the event primitive. Renderers must never be able to break validation."""

from __future__ import annotations

import json

from gitscribe.validation.events import Event, emit


def test_event_serializes_flat_json() -> None:
    payload = json.loads(
        Event("scanner.finished", {"scanner": "ruff", "findings": 2}, ts=1.5).to_json()
    )

    assert payload == {"type": "scanner.finished", "ts": 1.5, "scanner": "ruff", "findings": 2}


def test_emit_without_emitter_is_a_noop_but_still_logged(trace) -> None:
    emit(None, "plan.ready", jobs=["ruff"])

    assert trace.events == []
    assert [r["event"] for r in trace.records if "event" in r] == [
        {"type": "plan.ready", "jobs": ["ruff"]}
    ]


def test_emit_delivers_to_the_emitter(trace) -> None:
    emit(trace, "scanner.started", scanner="ruff")

    assert trace.types() == ["scanner.started"]
    assert trace.of("scanner.started") == [{"scanner": "ruff"}]


def test_a_failing_emitter_never_propagates(trace) -> None:
    def broken(_event: Event) -> None:
        raise RuntimeError("renderer crashed")

    emit(broken, "gate.decision", status="PASS")  # must not raise

    assert any(r.get("event", {}).get("type") == "gate.decision" for r in trace.records)
