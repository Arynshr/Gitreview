"""Validation DAG: planner, bounded-concurrent executor, and
end-to-end gate behaviour with fault isolation.

Scanner binaries are faked via `shutil.which`; job runners via
`DETERMINISTIC_RUNNERS`. No subprocess, git, or model is ever invoked.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from gitscribe.validation.analysis.scanners.betterleaks_scanner import BetterleaksScanner
from gitscribe.validation.deterministic import DETERMINISTIC_RUNNERS, DeterministicAnalysisError
from gitscribe.validation.graph import (
    execute_node,
    plan_node,
    run_validation_sync,
)

GRAPH_LOGGER = "gitscribe.validation.graph"


def _ids(out: dict) -> set[str]:
    return {job.job_id for job in out["plan"].jobs}


def _planned(make_state, change, cfg, **request_overrides):
    state = make_state(change, **request_overrides)
    return state.model_copy(update=plan_node(state, cfg))


# --- planner -----------------------------------------------------------------


def test_plan_emits_one_job_per_applicable_scanner(
    all_tools_present, make_state, make_change, base_cfg
) -> None:
    change = make_change(files=("a.py", "requirements.txt"))

    out = plan_node(make_state(change, requested_mode="static"), base_cfg)

    assert _ids(out) == {"betterleaks", "gitscribe-path-check", "ruff", "semgrep", "osv-scanner"}
    assert {j.job_id: j.required for j in out["plan"].jobs}["betterleaks"] is True
    assert out["errors"] == []
    assert all(problem is None for problem in out["diagnostics"]["scanner_health"].values())
    assert out["scope"].secret_scan_scope == change.files


def test_plan_skips_scanners_that_do_not_support_the_scope(
    all_tools_present, make_state, make_change, base_cfg
) -> None:
    out = plan_node(
        make_state(make_change(files=("README.md",)), requested_mode="static"), base_cfg
    )

    assert _ids(out) == {
        "betterleaks",
        "gitscribe-path-check",
        "semgrep",
    }  # no ruff (.py), no osv (deps)
    assert out["errors"] == []


def test_missing_binary_is_a_logged_error_not_a_crash(
    monkeypatch, make_state, make_change, base_cfg, caplog
) -> None:
    monkeypatch.setattr(
        "shutil.which", lambda name, *a, **k: None if name == "betterleaks" else f"/bin/{name}"
    )

    with caplog.at_level(logging.WARNING, logger=GRAPH_LOGGER):
        out = plan_node(make_state(make_change(), requested_mode="static"), base_cfg)

    assert "betterleaks" not in _ids(out)
    assert {"ruff", "semgrep", "gitscribe-path-check"} <= _ids(out)  # others still planned
    assert [e.source for e in out["errors"]] == ["betterleaks"]
    assert "not found" in out["errors"][0].message
    assert out["diagnostics"]["scanner_health"]["betterleaks"]
    assert any("scanner unavailable" in r.getMessage() for r in caplog.records)


def test_every_missing_tool_is_reported_and_path_check_still_runs(
    monkeypatch, make_state, make_change, base_cfg
) -> None:
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)

    out = plan_node(make_state(make_change(), requested_mode="static"), base_cfg)

    assert _ids(out) == {"gitscribe-path-check"}  # in-process check has no binary to miss
    assert {e.source for e in out["errors"]} == {"betterleaks", "ruff", "semgrep"}


def test_required_secret_scan_is_never_dropped_by_supports(
    all_tools_present, monkeypatch, make_state, make_change, base_cfg
) -> None:
    monkeypatch.setattr(BetterleaksScanner, "supports", lambda self, scope: False)

    out = plan_node(make_state(make_change(), requested_mode="static"), base_cfg)

    assert "betterleaks" in _ids(out)


def test_disabling_deterministic_still_plans_secret_scan(
    all_tools_present, make_state, make_change
) -> None:
    out = plan_node(
        make_state(make_change(), requested_mode="static"), {"deterministic": {"enabled": False}}
    )

    assert _ids(out) == {"betterleaks"}


@pytest.mark.parametrize("mode", ["agentic", "both", "static"])
def test_per_file_mode_no_longer_excludes_files_from_static_scanners(
    all_tools_present, make_state, make_change, base_cfg, mode
) -> None:
    out = plan_node(make_state(make_change(), requested_mode=mode), base_cfg)

    assert _ids(out) == {"betterleaks", "gitscribe-path-check", "ruff", "semgrep"}
    assert out["diagnostics"]["static_files"] == ["a.py"]


def test_agentic_file_rules_are_ignored(
    all_tools_present, make_state, make_change, base_cfg
) -> None:
    cfg = {**base_cfg, "file_rules": [{"path": "a.py", "mode": "agentic"}]}

    assert "ruff" in _ids(plan_node(make_state(make_change()), cfg))


# --- executor: bounded concurrency & fault isolation -------------------------


def test_executor_is_concurrent_but_bounded(
    all_tools_present, monkeypatch, make_state, make_change, base_cfg
) -> None:
    """4 jobs, max_workers=2: a 2-party barrier only releases if two jobs are
    in flight at once (proves concurrency); the peak counter proves the bound.
    """
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    active = peak = 0

    def runner(ctx, timeout, cfg):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait()
        finally:
            with lock:
                active -= 1
        return []

    for name in list(DETERMINISTIC_RUNNERS):
        monkeypatch.setitem(DETERMINISTIC_RUNNERS, name, runner)

    state = _planned(make_state, make_change(), base_cfg, requested_mode="static")
    assert len(state.plan.jobs) == 4

    out = asyncio.run(execute_node(state, base_cfg))

    assert out["errors"] == []  # a broken barrier would surface as errors
    assert peak == 2


def test_max_workers_one_serializes_jobs(
    all_tools_present, monkeypatch, make_state, make_change, base_cfg
) -> None:
    lock = threading.Lock()
    active = peak = 0

    def runner(ctx, timeout, cfg):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        with lock:
            active -= 1
        return []

    for name in list(DETERMINISTIC_RUNNERS):
        monkeypatch.setitem(DETERMINISTIC_RUNNERS, name, runner)

    cfg = {**base_cfg, "deterministic": {**base_cfg["deterministic"], "max_workers": 1}}
    state = _planned(make_state, make_change(), cfg, requested_mode="static")

    asyncio.run(execute_node(state, cfg))

    assert peak == 1


def test_one_failing_job_does_not_discard_the_others(
    all_tools_present, monkeypatch, make_state, make_change, make_finding, base_cfg, caplog
) -> None:
    kept = make_finding(id="kept", rule_id="PATH", severity="medium")

    def ruff(ctx, t, cfg):
        raise DeterministicAnalysisError("ruff exploded")

    def semgrep(ctx, t, cfg):
        raise RuntimeError("unexpected bug")  # not a DeterministicAnalysisError

    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "ruff", ruff)
    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "semgrep", semgrep)
    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "gitscribe-path-check", lambda c, t, g: [kept])
    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "betterleaks", lambda c, t, g: [])

    state = _planned(make_state, make_change(), base_cfg, requested_mode="static")

    with caplog.at_level(logging.WARNING, logger=GRAPH_LOGGER):
        out = asyncio.run(execute_node(state, base_cfg))

    messages = {e.source: e.message for e in out["errors"]}
    assert messages["ruff"] == "ruff exploded"
    assert messages["semgrep"] == "RuntimeError: unexpected bug"
    assert out["findings"] == [kept]
    assert out["scanner_results"]["gitscribe-path-check"] == [kept]
    assert sum("scanner job failed" in r.getMessage() for r in caplog.records) == 2


def test_executor_records_per_scanner_timings(
    all_tools_present, stub_runners, make_state, make_change, base_cfg
) -> None:
    state = _planned(make_state, make_change(), base_cfg, requested_mode="static")

    out = asyncio.run(execute_node(state, base_cfg))

    assert set(out["timings"]) == {f"{j.job_id}_s" for j in state.plan.jobs}


def test_jobs_receive_their_planned_file_scope(
    all_tools_present, stub_runners, make_state, make_change, base_cfg
) -> None:
    state = _planned(
        make_state,
        make_change(files=("a.py", "requirements.txt")),
        base_cfg,
        requested_mode="static",
    )

    asyncio.run(execute_node(state, base_cfg))

    seen = dict(stub_runners)
    assert seen["betterleaks"] == ["a.py", "requirements.txt"]
    assert seen["ruff"] == ["a.py", "requirements.txt"]  # static_files; ruff filters .py itself


# --- end to end ----------------------------------------------------------------


def _run(make_request, change, cfg, **request_overrides):
    return run_validation_sync(make_request(**request_overrides), cfg, change=change).result


def test_clean_change_passes_with_diagnostics(
    all_tools_present, stub_runners, make_request, make_change, base_cfg
) -> None:
    result = _run(make_request, make_change(), base_cfg, requested_mode="static")

    assert result.status == "PASS"
    assert result.errors == []
    assert "betterleaks" in result.diagnostics["jobs"]
    assert set(result.diagnostics["scanner_health"]) == {"betterleaks", "ruff", "semgrep"}


def test_high_severity_deterministic_finding_fails_the_gate(
    all_tools_present, stub_runners, monkeypatch, make_request, make_change, make_finding, base_cfg
) -> None:
    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "ruff", lambda c, t, g: [make_finding(line=None)])

    result = _run(make_request, make_change(), base_cfg, requested_mode="static")

    assert result.status == "FAIL"
    assert len(result.findings) == 1


def test_scanner_failure_fails_closed_but_keeps_other_findings(
    all_tools_present, stub_runners, monkeypatch, make_request, make_change, make_finding, base_cfg
) -> None:
    def boom(c, t, g):
        raise DeterministicAnalysisError("down")

    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "semgrep", boom)
    monkeypatch.setitem(
        DETERMINISTIC_RUNNERS, "ruff", lambda c, t, g: [make_finding(severity="low", line=2)]
    )

    result = _run(make_request, make_change(), base_cfg, requested_mode="static")

    assert result.status == "FAIL"
    assert [e.source for e in result.errors] == ["semgrep"]
    assert len(result.findings) == 1


def test_scanner_failure_passes_when_fail_closed_is_disabled(
    all_tools_present, stub_runners, monkeypatch, make_request, make_change, base_cfg
) -> None:
    def boom(c, t, g):
        raise DeterministicAnalysisError("down")

    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "semgrep", boom)

    result = _run(
        make_request, make_change(), {**base_cfg, "fail_closed": False}, requested_mode="static"
    )

    assert result.status == "PASS"
    assert [e.source for e in result.errors] == ["semgrep"]  # still recorded


def test_empty_diff_short_circuits(
    all_tools_present, stub_runners, make_request, make_change, base_cfg
) -> None:
    result = _run(make_request, make_change(diff="  \n"), base_cfg)

    assert result.status == "PASS"
    assert stub_runners == []  # nothing planned, nothing run
    assert result.diagnostics == {}
