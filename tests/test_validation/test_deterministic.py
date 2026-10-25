"""Scanner-adapter translation layer (`validation/deterministic.py`).

The subprocess boundary is `run_scanner_command`; it is the only thing
patched, so adapter build/parse/error-translation logic runs for real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitscribe.validation.analysis.models import ProcessResult
from gitscribe.validation.analysis.runner import ScannerExecutionError
from gitscribe.validation.deterministic import (
    BUNDLE,
    DETERMINISTIC_RUNNERS,
    DeterministicAnalysisError,
    run_deterministic,
    run_ruff,
)
from gitscribe.validation.models import ChangeContext


def _ctx(files: list[str]) -> ChangeContext:
    return ChangeContext(base="base", head="head", diff="diff", files=files)


def _proc(returncode: int | None = 0, stdout: str = "", stderr: str = "") -> ProcessResult:
    return ProcessResult(args=["ruff"], returncode=returncode, stdout=stdout, stderr=stderr)


def _patch_runner(monkeypatch: pytest.MonkeyPatch, outcome) -> list:
    """`outcome` is a ProcessResult to return or an Exception to raise."""
    calls: list = []

    def fake(command, telemetry=None):
        calls.append(command)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("gitscribe.validation.deterministic.run_scanner_command", fake)
    return calls


@pytest.fixture
def py_file(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.py"
    path.write_text("import os\n", encoding="utf-8")
    return path


def test_ruff_finding_is_normalized(monkeypatch, py_file) -> None:
    stdout = json.dumps(
        [
            {
                "filename": str(py_file),
                "location": {"row": 3, "column": 1},
                "code": "F401",
                "message": "unused import",
            }
        ]
    )
    _patch_runner(monkeypatch, _proc(returncode=1, stdout=stdout))

    findings = run_ruff(_ctx([str(py_file)]))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.source == "deterministic"
    assert finding.rule_id == "F401"
    assert finding.severity == "medium"
    assert finding.file == str(py_file)
    assert finding.line == 3
    assert finding.sources == ["ruff"]


def test_ruff_clean_run_has_no_findings(monkeypatch, py_file) -> None:
    _patch_runner(monkeypatch, _proc(returncode=0, stdout=""))

    assert run_ruff(_ctx([str(py_file)])) == []


def test_ruff_scanner_failure_is_not_clean(monkeypatch, py_file) -> None:
    _patch_runner(monkeypatch, _proc(returncode=2, stderr="scanner failed"))

    with pytest.raises(DeterministicAnalysisError, match="ruff failed"):
        run_ruff(_ctx([str(py_file)]))


def test_ruff_malformed_json_is_an_error_not_clean(monkeypatch, py_file) -> None:
    _patch_runner(monkeypatch, _proc(returncode=0, stdout="{not json"))

    with pytest.raises(DeterministicAnalysisError, match="malformed"):
        run_ruff(_ctx([str(py_file)]))


def test_missing_binary_is_translated_and_message_preserved(monkeypatch, py_file) -> None:
    _patch_runner(monkeypatch, ScannerExecutionError("required scanner not found: ruff"))

    with pytest.raises(DeterministicAnalysisError, match="not found: ruff"):
        run_ruff(_ctx([str(py_file)]))


@pytest.mark.parametrize("files", [[], ["notes.md"], ["does_not_exist.py"]])
def test_ruff_skips_without_existing_python_files(monkeypatch, files) -> None:
    calls = _patch_runner(monkeypatch, _proc())

    assert run_ruff(_ctx(files)) == []
    assert calls == []  # never spawned a subprocess


def test_runner_table_covers_every_plannable_job() -> None:
    assert set(BUNDLE) <= set(DETERMINISTIC_RUNNERS)
    assert "betterleaks" in DETERMINISTIC_RUNNERS
    assert "betterleaks" not in BUNDLE  # §10.3: planned unconditionally, outside the bundle


def test_run_deterministic_runs_bundle_in_order(monkeypatch) -> None:
    order: list[str] = []

    for name in list(DETERMINISTIC_RUNNERS):
        monkeypatch.setitem(
            DETERMINISTIC_RUNNERS,
            name,
            lambda ctx, t, cfg, _n=name: (order.append(_n), [_n])[1],
        )

    findings = run_deterministic(_ctx(["a.py"]))

    assert order == list(BUNDLE)
    assert findings == list(BUNDLE)


def test_run_deterministic_propagates_first_failure(monkeypatch) -> None:
    def boom(ctx, t, cfg):
        raise DeterministicAnalysisError("ruff down")

    monkeypatch.setitem(DETERMINISTIC_RUNNERS, "ruff", boom)
    for name in set(BUNDLE) - {"ruff"}:
        monkeypatch.setitem(DETERMINISTIC_RUNNERS, name, lambda ctx, t, cfg: [])

    with pytest.raises(DeterministicAnalysisError, match="ruff down"):
        run_deterministic(_ctx(["a.py"]))
