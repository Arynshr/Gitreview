import hashlib
import json
import re
import subprocess
from pathlib import Path

from gitscribe.core.diff_parser import extract_files_changed, get_commit_messages, get_raw_diff
from gitscribe.core.risk_classifier import risk_classifier_node
from gitscribe.core.state import GitScribeState
from gitscribe.core.summarizer import summarize_diff

CACHE_DIR = Path(".git") / "gitscribe-cache"
BUMP_FILE = Path(".git") / "gitscribe-last-bump"

COMMIT_RE = re.compile(
    r"^(?P<type>feat|fix|build|chore|ci|docs|style|refactor|perf|test)"
    r"(?:\((?P<scope>[a-z0-9_-]+)\))?"
    r"(?P<breaking>!)?: "
    r"(?P<desc>[a-z].{0,98})$"
)
BREAKING_FOOTER_RE = re.compile(r"^BREAKING CHANGE: .+", re.MULTILINE)


def _diff_cache_key(base: str, head: str) -> tuple[str, str]:
    raw = get_raw_diff(base=base, head=head)
    return hashlib.sha1(raw.encode()).hexdigest(), raw


def get_cached_risk(cfg: dict, base: str, head: str) -> dict:
    key, raw = _diff_cache_key(base, head)
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    files_changed = extract_files_changed(raw)
    state = GitScribeState(
        raw_diff=raw,
        files_changed=files_changed,
        change_summary=summarize_diff(files_changed, raw),
        commit_messages=get_commit_messages(base=base, head=head),
    )
    result = risk_classifier_node(state, cfg)
    cache_file.write_text(json.dumps(result))
    return result

def bump_for_commit(subject: str, body: str) -> str:
    m = COMMIT_RE.match(subject)
    if not m:
        return "patch"
    is_breaking = bool(m.group("breaking")) or bool(BREAKING_FOOTER_RE.search(body))
    if is_breaking:
        return "major"
    return "minor" if m.group("type") == "feat" else "patch"


def max_bump(bumps: list[str]) -> str:
    order = {"patch": 0, "minor": 1, "major": 2}
    return max(bumps, key=lambda b: order.get(b, 0)) if bumps else "patch"


def next_tag(bump: str) -> str:
    last = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"], capture_output=True, text=True
    ).stdout.strip() or "v0.0.0"
    major, minor, patch = (int(x) for x in last.lstrip("v").split("."))
    if bump == "major":
        return f"v{major + 1}.0.0"
    if bump == "minor":
        return f"v{major}.{minor + 1}.0"
    return f"v{major}.{minor}.{patch + 1}"


def conflicted_files(cwd: str | Path | None = None) -> list[str]:
    """`cwd` lets merge_preview.worktree reuse this against a disposable
    worktree instead of re-implementing conflict detection there.
    """
    result = subprocess.run(
        ["git", "diff", "--diff-filter=U", "--name-only"],
        capture_output=True, text=True, cwd=cwd,
    )
    return [f for f in result.stdout.splitlines() if f]
