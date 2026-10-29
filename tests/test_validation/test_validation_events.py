"""Integration: the real validation graph emits a coherent event stream.

Scanners are stubbed (see conftest) - what is under test is the graph's
event contract: ordering, payloads, parity with the no-emitter path, and
that the gate reason surfaced in events matches the result.
"""

from __future__ import annotations

from gitscribe.validation.deterministic import DETERMINISTIC_RUNNERS, DeterministicAnalysisError
from gitscribe.validation.events import Event
from gitscribe.validation.graph import run_validation_sync


def _run(make_request, change, cfg, emitter=None):
    return run_validation_sync(
        make_request(requested_mode="static"), cfg, change=change, emitter=emitter
    ).result


def test_stream_is_bracketed_by_plan_and_gate_with_paired_scanner_events(
    all_tools_present, stub_runners, make_request, make_change, base_cfg, trace
) -> None:
    _run(make_request, make_change(), base_cfg, trace)

    types = trace.types()
    assert types[0] == "plan.ready" and types[-1] == "gate.decision"
    planned = trace.of("plan.ready")[0]["jobs"]
    started = [d["scanner"] for d in trace.of("scanner.started")]
    finished = [d["scanner"] for d in trace.of("scanner.finished")]
    assert sorted(started) == sorted(finished) == sorted(planned)
    order = [(e.type, e.data.get("scanner")) for e in trace.events]
    for name in planned:  # a scanner is always reported started before finished
        assert order.index(("scanner.started", name)) < order.index(("scanner.finished", name))


def test_finding_events_carry_the_finding_payload(
    all_tools_present,
    stub_runners,
    monkeypatch,
    make_request,
    make_change,
    make_finding,
    base_cfg,
    trace,
) -> None:
    monkeypatch.setitem(
        DETERMINISTIC_RUNNERS, "ruff", lambda c, t, g: [make_finding(severity="low", line=7)]
    )

    _run(make_request, make_change(), base_cfg, trace)

    [found] = trace.of("finding.found")
    assert (found["scanner"], found["severity"], found["file"], found["line"]) == (
        "ruff",
        "low",
        "a.py",
        7,
    )
    ruff_done = next(d for d in trace.of("scanner.finished") if d["scanner"] == "ruff")
    assert ruff_done["findings"] == 1 and ruff_done["error"] is None


def test_scanner_failure_is_reported_and_explains_the_gate(
    all_tools_present, stub_runners, monkeypatch, make_request, make_change, base_cfg, trace
) -> None:
    def boom(c, t, g):
        raise DeterministicAnalysisError("semgrep down")

    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "semgrep", boom)

    result = _run(make_request, make_change(), base_cfg, trace)

    failed = next(d for d in trace.of("scanner.finished") if d["scanner"] == "semgrep")
    assert failed["error"] == "semgrep down"
    [gate] = trace.of("gate.decision")
    assert gate["status"] == "FAIL" and gate["errors"] == 1
    assert gate["reason"] == "scanner error with fail_closed enabled"
    assert result.diagnostics["gate_reason"] == gate["reason"]


def test_gate_reason_names_the_blocking_finding(
    all_tools_present,
    stub_runners,
    monkeypatch,
    make_request,
    make_change,
    make_finding,
    base_cfg,
    trace,
) -> None:
    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "ruff", lambda c, t, g: [make_finding(line=None)])

    _run(make_request, make_change(), base_cfg, trace)

    [gate] = trace.of("gate.decision")
    assert gate["reason"] == "high finding from TEST"
    assert gate["severities"] == {"high": 1}


def test_passing_run_has_empty_reason(
    all_tools_present, stub_runners, make_request, make_change, base_cfg, trace
) -> None:
    _run(make_request, make_change(), base_cfg, trace)

    [gate] = trace.of("gate.decision")
    assert (gate["status"], gate["reason"], gate["findings"]) == ("PASS", "", 0)


def test_emitting_does_not_change_the_result(
    all_tools_present,
    stub_runners,
    monkeypatch,
    make_request,
    make_change,
    make_finding,
    base_cfg,
    trace,
) -> None:
    monkeypatch.setitem(
        DETERMINISTIC_RUNNERS, "ruff", lambda c, t, g: [make_finding(severity="medium", line=3)]
    )

    with_events = _run(make_request, make_change(), base_cfg, trace).model_dump(exclude={"timings"})
    without = _run(make_request, make_change(), base_cfg).model_dump(exclude={"timings"})

    assert with_events == without


def test_a_crashing_emitter_cannot_change_the_outcome(
    all_tools_present, stub_runners, make_request, make_change, base_cfg
) -> None:
    def broken(_e: Event) -> None:
        raise RuntimeError("boom")

    assert _run(make_request, make_change(), base_cfg, broken).status == "PASS"


def test_empty_diff_emits_nothing(
    all_tools_present, stub_runners, make_request, make_change, base_cfg, trace
) -> None:
    _run(make_request, make_change(diff="  \n"), base_cfg, trace)

    assert trace.events == []
