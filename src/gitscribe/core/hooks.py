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


def _clean_working_tree() -> bool:
    """True only when there are no uncommitted changes (staged or not).

    `gitscribe verify`'s deterministic/AI passes read file *content*
    directly off disk (Path.is_file()/read_text()), not from git blobs at
    `head` - unlike risk_classifier_node above, which only ever looks at
    diff text and commit messages. That means a validation result cached
    by (base, head) is only sound when the working tree provably matches
    what's committed at head; otherwise a stale PASS could survive a local
    edit that introduced a real problem. Gating both cache reads and
    writes on this keeps the cache correct at the cost of only ever
    caching runs against a clean tree (the common case for CI checkouts
    and hook-triggered runs; ad hoc `gitscribe verify` while mid-edit
    correctly always re-validates instead of trusting a stale result).
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def _validation_cache_key(cfg: dict, base: str, head: str, *extra: object) -> str:
    diff_key, _raw = _diff_cache_key(base, head)
    # Gate behavior depends on cfg (fail_on, file_rules, model, ...) and on
    # the sandboxed/force_ai flags a given `verify` invocation was run
    # with, not just the diff - all of that must invalidate the cache the
    # same way a new commit does, or a cached result from a differently
    # configured run could be served back incorrectly.
    payload = json.dumps({"cfg": cfg, "extra": extra}, sort_keys=True, default=str)
    cfg_digest = hashlib.sha1(payload.encode()).hexdigest()[:16]
    return f"validation-{diff_key}-{cfg_digest}"


def get_cached_validation(cfg: dict, base: str, head: str, *extra: object) -> dict | None:
    if not _clean_working_tree():
        return None

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"{_validation_cache_key(cfg, base, head, *extra)}.json"

    if cache_file.exists():
        return json.loads(cache_file.read_text())

    return None


def store_cached_validation(cfg: dict, base: str, head: str, result: dict, *extra: object) -> None:
    if not _clean_working_tree():
        return

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"{_validation_cache_key(cfg, base, head, *extra)}.json"
    cache_file.write_text(json.dumps(result))

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
