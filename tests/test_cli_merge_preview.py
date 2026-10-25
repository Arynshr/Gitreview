import json
from unittest.mock import patch

from typer.testing import CliRunner

from gitscribe.cli import app
from gitscribe.core.merge_preview.models import FileReport, HunkResolution, MergePreviewReport
from gitscribe.core.merge_preview.worktree import WorktreeError

runner = CliRunner()

CONFIG_YAML = """
llm:
  model: openai/gpt-oss-20b
  fallback_model: openai/gpt-oss-20b
"""


def _write_config(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)


def test_merge_preview_reports_clean_merge(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "fake-key")

    clean_report = MergePreviewReport(ours_branch="main", theirs_branch="feature/x", clean=True)
    with (
        patch("gitscribe.cli.current_branch", return_value="main"),
        patch("gitscribe.cli.run_merge_preview", return_value=clean_report),
    ):
        result = runner.invoke(app, ["merge-preview", "feature/x"])

    assert result.exit_code == 0
    assert "no conflicts" in result.stdout.lower()


def test_merge_preview_reports_conflicts_with_confidence(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "fake-key")

    conflict_report = MergePreviewReport(
        ours_branch="main",
        theirs_branch="feature/x",
        clean=False,
        files=[
            FileReport(
                file="app.py",
                resolutions=[
                    HunkResolution(
                        hunk_index=0,
                        resolved_text="merged",
                        rationale="safe combine",
                        confidence="high",
                    ),
                    HunkResolution(
                        hunk_index=1, resolved_text="", rationale="ambiguous", confidence="low"
                    ),
                ],
            )
        ],
    )
    with (
        patch("gitscribe.cli.current_branch", return_value="main"),
        patch("gitscribe.cli.run_merge_preview", return_value=conflict_report),
    ):
        result = runner.invoke(app, ["merge-preview", "feature/x"])

    assert result.exit_code == 0
    assert "app.py" in result.stdout
    assert "1/2" in result.stdout  # total_safe/total_hunks
    assert "manual review" in result.stdout.lower()


def test_merge_preview_json_output_is_valid_json(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "fake-key")

    report = MergePreviewReport(ours_branch="main", theirs_branch="feature/x", clean=True)
    with (
        patch("gitscribe.cli.current_branch", return_value="main"),
        patch("gitscribe.cli.run_merge_preview", return_value=report),
    ):
        result = runner.invoke(app, ["merge-preview", "feature/x", "--json"])

    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["clean"] is True
    assert parsed["theirs_branch"] == "feature/x"


def test_merge_preview_worktree_error_exits_nonzero(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "fake-key")

    with (
        patch("gitscribe.cli.current_branch", return_value="main"),
        patch(
            "gitscribe.cli.run_merge_preview",
            side_effect=WorktreeError("could not create preview worktree"),
        ),
    ):
        result = runner.invoke(app, ["merge-preview", "feature/x"])

    assert result.exit_code == 1
    assert "could not create preview worktree" in result.output


def test_merge_preview_requires_api_key(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("API_KEY", raising=False)

    result = runner.invoke(app, ["merge-preview", "feature/x"])

    assert result.exit_code == 1
    assert "API_KEY" in result.output


def test_merge_preview_passes_custom_base_ref_through(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "fake-key")

    report = MergePreviewReport(ours_branch="main", theirs_branch="feature/x", clean=True)
    with (
        patch("gitscribe.cli.current_branch", return_value="main"),
        patch("gitscribe.cli.run_merge_preview", return_value=report) as mock_run,
    ):
        result = runner.invoke(app, ["merge-preview", "feature/x", "--base", "release/2.0"])

    assert result.exit_code == 0
    _, kwargs = mock_run.call_args
    assert kwargs["base_ref"] == "release/2.0"
