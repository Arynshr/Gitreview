"""Integration: lint output/repair, hook fail-open behaviour, init --force,
and global CLI hygiene."""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from gitscribe import cli
from gitscribe.core.analysis import linter
from gitscribe.core.analysis.linter import LintFinding
from gitscribe.core.diff_parser import GitCommandError

runner = CliRunner()


def _f(line=3, code="F401", sev="error", fixable=True, file="a.py") -> LintFinding:
    return LintFinding(file=file, lineno=line, col=5, code=code, message="unused import", severity=sev, fixable=fixable)


# --- lint ------------------------------------------------------------------


def test_lint_lists_each_finding_grouped_by_file_with_fix_hint(monkeypatch) -> None:
    monkeypatch.setattr(linter, "run_ruff", lambda root, paths=None: [_f(), _f(line=9, code="E501", sev="warning", fixable=False, file="b.py")])

    result = runner.invoke(cli.app, ["lint"])

    out = result.output
    assert "a.py" in out and "b.py" in out
    assert "3:5" in out and "F401" in out and "unused import" in out and "[fixable]" in out
    assert "2 finding(s): 1 error(s), 1 warning(s)" in out
    assert "1 auto-fixable -> gitscribe lint --fix" in out
    assert result.exit_code == 1  # an error-severity finding


def test_lint_clean_repo_says_so_and_exits_zero(monkeypatch) -> None:
    monkeypatch.setattr(linter, "run_ruff", lambda root, paths=None: [])

    result = runner.invoke(cli.app, ["lint"])

    assert result.exit_code == 0 and "no lint findings" in result.output


def test_lint_fix_applies_repairs_then_reports_what_remains(monkeypatch) -> None:
    runs = iter([[_f(), _f(line=4)], [_f(line=4, fixable=False)]])
    applied: list = []
    monkeypatch.setattr(linter, "run_ruff", lambda root, paths=None: next(runs))
    monkeypatch.setattr(linter, "apply_fixes", lambda root, paths=None, unsafe=False: applied.append(unsafe))

    result = runner.invoke(cli.app, ["lint", "--fix", "--no-fails-on-error"])

    assert applied == [False] and "fixed 1 of 2 finding(s); 1 remain" in result.output


def test_lint_format_runs_ruff_format(monkeypatch) -> None:
    formatted: list = []
    monkeypatch.setattr(linter, "run_ruff", lambda root, paths=None: [])
    monkeypatch.setattr(linter, "format_code", lambda root, paths=None, diff=False: formatted.append(diff) or "")

    result = runner.invoke(cli.app, ["lint", "--format"])

    assert result.exit_code == 0 and formatted == [False]


def test_lint_diff_previews_and_changes_nothing(monkeypatch) -> None:
    def forbidden(*a, **k):
        raise AssertionError("must not run/modify in --diff mode")

    monkeypatch.setattr(linter, "run_ruff", forbidden)
    monkeypatch.setattr(linter, "apply_fixes", forbidden)
    monkeypatch.setattr(linter, "preview_fixes", lambda root, paths=None, unsafe=False: "--- a.py\n+++ a.py\n")

    result = runner.invoke(cli.app, ["lint", "--diff", "--fix"])

    assert result.exit_code == 0 and "--- a.py" in result.output


def test_lint_scopes_to_path(monkeypatch, tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    monkeypatch.chdir(tmp_path)
    seen: list = []
    monkeypatch.setattr(linter, "run_ruff", lambda root, paths=None: seen.append(paths) or [])

    runner.invoke(cli.app, ["lint", "--path", "pkg"])

    assert seen == [["pkg"]]


def test_ruff_crash_is_an_error_not_no_findings(monkeypatch) -> None:
    def crash(root, paths=None):
        raise linter.RuffError("invalid config: bad key")

    monkeypatch.setattr(linter, "run_ruff", crash)

    result = runner.invoke(cli.app, ["lint"])

    assert result.exit_code == 2 and "ruff failed: invalid config: bad key" in result.output


def test_missing_ruff_is_reported(monkeypatch) -> None:
    def missing(root, paths=None):
        raise linter.RuffNotFoundError("`ruff` not found on PATH")

    monkeypatch.setattr(linter, "run_ruff", missing)

    result = runner.invoke(cli.app, ["lint"])

    assert result.exit_code == 1 and "not found" in result.output


# --- hooks must fail open --------------------------------------------------

CFG = {
    "hooks": {"pre_push": {"block_on_risk": True}, "merge_check": {"block_on_risk": True}},
    "risk_classifier": {"trivial_threshold": 1.0},
}


@pytest.fixture
def hook_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: CFG)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)  # never spawn a real `gitscribe create-pr`


def test_pre_push_does_not_block_when_its_own_risk_check_crashes(hook_env, monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("origin/main not found")

    monkeypatch.setattr(cli.hook_utils, "get_cached_risk", boom)

    result = runner.invoke(cli.app, ["pre-push"])

    assert result.exit_code == 0 and "risk check skipped" in result.output


def test_pre_push_still_blocks_on_genuine_high_risk(hook_env, monkeypatch) -> None:
    monkeypatch.setattr(cli.hook_utils, "get_cached_risk", lambda *a, **k: {"risk_score": 10.0, "risk_reasoning": "big"})

    result = runner.invoke(cli.app, ["pre-push"])

    assert result.exit_code == 1 and "blocked" in result.output


def test_merge_check_does_not_block_when_its_own_check_crashes(hook_env, monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("MERGE_HEAD missing")

    monkeypatch.setattr(cli.hook_utils, "get_cached_risk", boom)

    result = runner.invoke(cli.app, ["merge-check"])

    assert result.exit_code == 0 and "skipped" in result.output


def test_post_merge_survives_a_missing_reflog(hook_env, monkeypatch) -> None:
    monkeypatch.setattr(cli, "current_branch", lambda: "main")

    def no_reflog(*a, **k):
        raise GitCommandError("HEAD@{1} unknown revision")

    monkeypatch.setattr(cli, "get_raw_diff", no_reflog)

    result = runner.invoke(cli.app, ["post-merge"])

    assert result.exit_code == 0 and "skipping memory update" in result.output


# --- init --------------------------------------------------------------------


@pytest.fixture
def init_env(monkeypatch, tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    monkeypatch.setattr(cli, "repo_hooks_dir", hooks)
    monkeypatch.setattr(cli, "_ensure_merge_preview_alias", lambda: None)
    monkeypatch.setattr(cli, "_ensure_api_key", lambda: None)
    return hooks


def test_init_does_not_overwrite_existing_hooks_by_default(init_env) -> None:
    (init_env / "pre-push").write_text("#!/bin/sh\n# mine\n")

    result = runner.invoke(cli.app, ["init"])

    assert "--force" in result.output and (init_env / "pre-push").read_text().endswith("# mine\n")


def test_init_force_replaces_and_keeps_a_backup(init_env) -> None:
    (init_env / "pre-push").write_text("#!/bin/sh\n# mine\n")

    result = runner.invoke(cli.app, ["init", "--force"])

    assert result.exit_code == 0
    assert (init_env / "pre-push.bak").read_text().endswith("# mine\n")
    assert "# mine" not in (init_env / "pre-push").read_text()


# --- global hygiene ----------------------------------------------------------


def test_version_flag() -> None:
    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0 and result.output.startswith("gitscribe")


def test_no_args_shows_help() -> None:
    result = runner.invoke(cli.app, [])

    assert "Usage" in result.output and "commit" in result.output


def test_crash_tracebacks_never_print_local_variables() -> None:
    assert cli.app.pretty_exceptions_show_locals is False  # locals would include the API key
