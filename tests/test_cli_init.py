import os

from typer.testing import CliRunner

from gitscribe.cli import app

runner = CliRunner()


def test_init_installs_pre_push_hook(tmp_path, monkeypatch):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    installed = tmp_path / ".git" / "hooks" / "pre-push"
    assert result.exit_code == 0
    assert installed.exists()
    assert os.access(installed, os.X_OK)


def test_init_does_not_overwrite_existing_hook(tmp_path, monkeypatch):
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    existing = hooks_dir / "pre-push"
    existing.write_text("# custom hook, do not touch")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "custom hook" in existing.read_text()


def test_init_fails_outside_git_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0


def test_init_configures_merge_preview_git_alias(tmp_path, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    alias = subprocess.run(
        ["git", "config", "--get", "alias.merge-preview"], capture_output=True, text=True
    )
    assert alias.returncode == 0
    assert "gitscribe merge-preview" in alias.stdout


def test_init_does_not_overwrite_a_preexisting_different_merge_preview_alias(tmp_path, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    subprocess.run(
        ["git", "config", "alias.merge-preview", "!echo custom"], check=True, cwd=tmp_path
    )

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    alias = subprocess.run(
        ["git", "config", "--get", "alias.merge-preview"], capture_output=True, text=True
    )
    assert alias.stdout.strip() == "!echo custom"
