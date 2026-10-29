"""Integration: change collection, message generation and path-scoped
commits, against a real throwaway git repository."""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from gitscribe.core import commit_message as cm


def git(*args: str, cwd=None) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, cwd=cwd).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git("init", "-q", "-b", "main", cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=tmp_path)
    git("config", "user.name", "t", cwd=tmp_path)
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "api" / "a.py").write_text("a = 1\n")
    (tmp_path / "web" / "w.py").write_text("w = 1\n")
    git("add", ".", cwd=tmp_path)
    git("commit", "-qm", "chore: init", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _modify_both(repo) -> None:
    (repo / "api" / "a.py").write_text("a = 2\n")
    (repo / "web" / "w.py").write_text("w = 2\n")


def test_path_scope_sees_only_that_folder(repo) -> None:
    _modify_both(repo)

    changes = cm.collect_changes(["api"])

    assert changes.files == ["api/a.py"] and "w = 2" not in changes.diff


def test_default_scope_is_staged_changes_only(repo) -> None:
    _modify_both(repo)
    git("add", "web/w.py")

    changes = cm.collect_changes(None)

    assert changes.files == ["web/w.py"]


def test_nothing_staged_is_empty(repo) -> None:
    assert cm.collect_changes(None).empty


def test_untracked_files_are_listed_only_when_requested(repo) -> None:
    (repo / "api" / "new.py").write_text("n = 1\n")

    assert cm.collect_changes(["api"]).untracked == []
    assert cm.collect_changes(["api"], include_untracked=True).untracked == ["api/new.py"]


def test_oversized_diffs_are_truncated_but_flagged(repo, monkeypatch) -> None:
    monkeypatch.setattr(cm, "MAX_DIFF_CHARS", 50)
    (repo / "api" / "a.py").write_text("x = 1\n" * 100)

    changes = cm.collect_changes(["api"])

    assert changes.truncated and len(changes.diff) == 50 and "a.py" in changes.stat


def test_commit_with_paths_commits_only_that_folder_and_leaves_other_staged_work(repo) -> None:
    _modify_both(repo)
    git("add", "web/w.py")  # unrelated staged work must survive

    sha = cm.stage_and_commit("feat(api): bump a", ["api"])

    assert git("show", "--name-only", "--format=", sha).split() == ["api/a.py"]
    assert git("diff", "--cached", "--name-only").split() == ["web/w.py"]


def test_commit_can_include_new_files(repo) -> None:
    (repo / "api" / "new.py").write_text("n = 1\n")

    sha = cm.stage_and_commit("feat(api): add new", ["api"], include_untracked=True)

    assert "api/new.py" in git("show", "--name-only", "--format=", sha)


@pytest.mark.parametrize("msg,ok", [
    ("feat(api): add endpoint", True), ("fix: handle empty input", True),
    ("Add endpoint", False), ("feat: Capitalised", False), ("", False),
])
def test_message_validation_matches_the_commit_msg_hook(msg, ok) -> None:
    assert (cm.validate_message(msg) is None) is ok


def _fake_llm(monkeypatch, content, seen=None, boom=False):
    class LLM:
        def invoke(self, messages):
            if boom:
                raise RuntimeError("429 rate limit")
            if seen is not None:
                seen.append(messages)
            return SimpleNamespace(content=content, usage_metadata=None)

    monkeypatch.setattr(cm, "build_chat_model", lambda cfg, model, temperature=0.0: LLM())

    @contextmanager
    def no_telemetry(*a, **k):
        yield {}

    monkeypatch.setattr(cm, "timed_llm_call", no_telemetry)


CFG = {"llm": {"model": "m"}}


def test_generated_message_is_cleaned_of_code_fences(repo, monkeypatch) -> None:
    _fake_llm(monkeypatch, "```\nfeat(api): bump a\n\nwhy\n```")
    (repo / "api" / "a.py").write_text("a = 5\n")

    assert cm.generate_message(cm.collect_changes(["api"]), CFG) == "feat(api): bump a\n\nwhy"


def test_prompt_contains_the_scoped_patch_and_the_format_rules(repo, monkeypatch) -> None:
    seen: list = []
    _fake_llm(monkeypatch, "fix: x", seen)
    _modify_both(repo)

    cm.generate_message(cm.collect_changes(["api"]), CFG)

    system, human = seen[0][0][1], seen[0][1][1]
    assert "Conventional Commits" in system and "a = 2" in human and "w = 2" not in human


def test_llm_failure_becomes_a_commit_message_error(repo, monkeypatch) -> None:
    _fake_llm(monkeypatch, "", boom=True)
    (repo / "api" / "a.py").write_text("a = 5\n")

    with pytest.raises(cm.CommitMessageError, match="rate limit"):
        cm.generate_message(cm.collect_changes(["api"]), CFG)
