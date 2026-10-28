"""Commit-message service: collect a change set, write a Conventional Commit
message from it, and (optionally) commit exactly that change set.

Two scopes:
  * paths given  -> working-tree changes under those paths vs HEAD
  * no paths     -> whatever is already staged
The same scope is used for the message and for the commit, so what is
described is what is committed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gitscribe.core.diff_parser import GitCommandError, _run_git
from gitscribe.core.hooks import COMMIT_RE
from gitscribe.core.llm_client import build_chat_model
from gitscribe.core.progress import phase
from gitscribe.core.telemetry import timed_llm_call
from gitscribe.events import Emitter

# The provider enforces a tokens-per-minute cap; the prompt is kept small on
# purpose. Stat summary is always included, the patch is cut to this many chars.
MAX_DIFF_CHARS = 12_000

SYSTEM = (
    "You write git commit messages in the Conventional Commits format.\n"
    "Rules:\n"
    "- First line: <type>(<optional-scope>): <description>\n"
    "- type is one of: feat fix build chore ci docs style refactor perf test\n"
    "- scope is lowercase [a-z0-9_-] only; description starts with a lowercase letter, "
    "imperative mood, at most 72 characters, no trailing period\n"
    "- Optional body after a blank line explaining what and why (not how), wrapped at 72 columns\n"
    "- Add a 'BREAKING CHANGE: ...' footer only for genuinely breaking changes\n"
    "- Output ONLY the commit message. No code fences, no commentary."
)


class CommitMessageError(RuntimeError):
    pass


@dataclass
class ChangeSet:
    paths: list[str]
    files: list[str]
    stat: str
    diff: str
    untracked: list[str]
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.files and not self.untracked


def collect_changes(paths: list[str] | None, include_untracked: bool = False) -> ChangeSet:
    paths = list(paths or [])
    spec = ["--", *paths] if paths else []
    base = ["diff", "HEAD"] if paths else ["diff", "--cached"]

    stat = _run_git([*base, "--stat", *spec]).strip()
    names = _run_git([*base, "--name-only", *spec]).split()
    untracked: list[str] = []
    if paths and include_untracked:
        untracked = _run_git(["ls-files", "--others", "--exclude-standard", *spec]).split()

    diff = _run_git([*base, *spec])
    truncated = len(diff) > MAX_DIFF_CHARS
    return ChangeSet(paths, names, stat, diff[:MAX_DIFF_CHARS], untracked, truncated)


def _clean(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    return text.strip("`").strip()


def validate_message(message: str) -> str | None:
    """None when the subject satisfies the repo's commit-msg hook, else why not."""
    subject = message.splitlines()[0] if message.strip() else ""
    if not subject:
        return "empty commit message"
    if COMMIT_RE.match(subject):
        return None
    return f"subject is not a valid Conventional Commit for this repo's commit-msg hook: {subject!r}"


def generate_message(changes: ChangeSet, cfg: dict, emitter: Emitter | None = None) -> str:
    with phase(emitter, "message") as info:
        model_name = cfg["llm"]["model"]
        llm = build_chat_model(cfg, model_name, temperature=0.2)
        listing = changes.stat or "\n".join(changes.files)
        human = f"Files changed:\n{listing}\n"
        if changes.untracked:
            human += "\nNew untracked files: " + ", ".join(changes.untracked) + "\n"
        human += f"\nPatch{' (truncated)' if changes.truncated else ''}:\n{changes.diff}"
        try:
            with timed_llm_call("commit_message", model_name) as ctx:
                ai_msg = llm.invoke([("system", SYSTEM), ("human", human)])
                ctx["usage"] = getattr(ai_msg, "usage_metadata", None)
        except Exception as e:
            raise CommitMessageError(f"LLM call failed: {str(e)[:200]}") from e

        content = ai_msg.content
        if isinstance(content, list):
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        message = _clean(content)
        info["chars"] = len(message)
        return message


def stage_and_commit(
    message: str, paths: list[str] | None, include_untracked: bool = False, no_verify: bool = False
) -> str:
    """Returns the new short commit hash."""
    paths = list(paths or [])
    spec = ["--", *paths] if paths else []
    if paths:
        _run_git(["add", "-u", *spec])
        if include_untracked:
            _run_git(["add", *spec])
    args = ["commit", "-m", message]
    if no_verify:
        args.append("--no-verify")
    _run_git([*args, *spec])  # `-- paths` = commit only these, leave other staged work alone
    return _run_git(["rev-parse", "--short", "HEAD"]).strip()


__all__ = [
    "ChangeSet", "CommitMessageError", "GitCommandError", "collect_changes",
    "generate_message", "stage_and_commit", "validate_message",
]
