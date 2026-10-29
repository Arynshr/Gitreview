"""Integration: `gitscribe commit` / `commit-message` command behaviour."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from gitscribe import cli
from gitscribe.core import commit_message as cm

runner = CliRunner()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture(autouse=True)
def _reset_console_routing():
    yield
    from gitscribe import console

    console.route_to_stderr(False)  # `--stream json` moves human output to stderr; don't leak it


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True)
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "api" / "a.py").write_text("a = 1\n")
    (tmp_path / "web" / "w.py").write_text("w = 1\n")
    monkeypatch.chdir(tmp_path)
    git("add", ".")
    git("commit", "-qm", "chore: init")
    (tmp_path / "api" / "a.py").write_text("a = 2\n")
    (tmp_path / "web" / "w.py").write_text("w = 2\n")
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: {"llm": {"model": "m"}})
    monkeypatch.setattr(cli, "require_api_key", lambda: None)
    return tmp_path


@pytest.fixture
def llm(monkeypatch):
    calls: list = []

    def fake(changes, cfg, emitter=None):
        calls.append(changes)
        return "feat(api): bump a"

    monkeypatch.setattr(cm, "generate_message", fake)
    return calls


def _count() -> int:
    return int(git("rev-list", "--count", "HEAD").strip())


def test_commit_path_yes_commits_only_that_folder_with_the_generated_message(repo, llm) -> None:
    result = runner.invoke(cli.app, ["commit", "--path", "api", "--yes"])

    assert result.exit_code == 0, result.output
    assert git("log", "-1", "--format=%s").strip() == "feat(api): bump a"
    assert git("show", "--name-only", "--format=", "HEAD").split() == ["api/a.py"]
    assert git("status", "--porcelain").split() == ["M", "web/w.py"]


def test_manual_message_skips_the_llm(repo, llm) -> None:
    result = runner.invoke(cli.app, ["commit", "-p", "api", "-m", "fix(api): manual", "-y"])

    assert result.exit_code == 0, result.output
    assert llm == [] and git("log", "-1", "--format=%s").strip() == "fix(api): manual"


def test_dry_run_commits_nothing(repo, llm) -> None:
    before = _count()

    result = runner.invoke(cli.app, ["commit", "-p", "api", "--dry-run"])

    assert result.exit_code == 0 and "feat(api): bump a" in result.output and _count() == before


def test_invalid_message_is_refused_non_interactively(repo, llm) -> None:
    before = _count()

    result = runner.invoke(cli.app, ["commit", "-p", "api", "-m", "Bad subject", "-y"])

    assert result.exit_code == 1 and "refusing" in result.output and _count() == before


def test_without_yes_a_non_tty_session_is_refused_before_committing(repo, llm) -> None:
    result = runner.invoke(cli.app, ["commit", "-p", "api"])

    assert result.exit_code == 1 and "--yes" in result.output and _count() == 1


def test_nothing_to_commit_under_the_path_is_a_clear_error(repo, llm) -> None:
    git("checkout", "--", "api")

    result = runner.invoke(cli.app, ["commit", "-p", "api", "-y"])

    assert result.exit_code == 1 and "no changes" in result.output


def test_unknown_path_fails_fast(repo, llm) -> None:
    result = runner.invoke(cli.app, ["commit", "-p", "nope", "-y"])

    assert result.exit_code == 2 and "path not found" in result.output


def test_commit_message_prints_only_the_message_on_stdout(repo, llm) -> None:
    result = runner.invoke(cli.app, ["commit-message", "-p", "api"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "feat(api): bump a"


def test_commit_message_json_stream_ends_with_a_result_event(repo, llm) -> None:
    result = runner.invoke(cli.app, ["commit-message", "-p", "api", "--stream", "json"])

    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert events[-1] == {"type": "result", "message": "feat(api): bump a", "valid": True}
    assert [e["name"] for e in events if e["type"] == "phase.started"] == ["collect"]
