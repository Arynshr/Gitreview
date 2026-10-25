"""
core/merge_preview/hunk_parser.py
Pure, dependency-free parsing of git's conflict markers into structured
ConflictHunk objects.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from gitscribe.core.merge_preview.models import ConflictHunk
from gitscribe.core.merge_preview.worktree import merge_base, read_blob

logger = logging.getLogger("gitscribe.merge_preview")

_OURS_RE = re.compile(r"^<<<<<<< (.*)$")
_BASE_RE = re.compile(r"^\|\|\|\|\|\|\| ?(.*)$")
_SEP_RE = re.compile(r"^=======$")
_THEIRS_RE = re.compile(r"^>>>>>>> (.*)$")


class ConflictParseError(RuntimeError):
    """Raised when conflict markers are malformed (unbalanced/nested) —
    surfaced rather than silently producing a wrong hunk, since a wrong
    resolution proposal from bad parsing is worse than refusing.
    """


def parse_conflicts(content: str, file: str) -> list[ConflictHunk]:
    """Scans a file's raw content for `<<<<<<<`/`=======`/`>>>>>>>` blocks
    and returns one ConflictHunk per block, in file order. Pure function —
    no subprocess, no filesystem access — so it's exhaustively unit
    testable without a real git repo.
    """
    lines = content.splitlines()
    hunks: list[ConflictHunk] = []

    state = "outside"  # outside -> ours -> (base) -> theirs -> outside
    ours_label = base_label = theirs_label = ""
    start_line = 0
    ours_buf: list[str] = []
    base_buf: list[str] = []
    theirs_buf: list[str] = []
    saw_base_marker = False

    def _reset():
        nonlocal ours_buf, base_buf, theirs_buf, saw_base_marker
        ours_buf, base_buf, theirs_buf = [], [], []
        saw_base_marker = False

    for i, line in enumerate(lines, start=1):
        m_ours = _OURS_RE.match(line)
        m_base = _BASE_RE.match(line)
        m_sep = _SEP_RE.match(line)
        m_theirs = _THEIRS_RE.match(line)

        if m_ours:
            if state != "outside":
                raise ConflictParseError(
                    f"{file}:{i}: nested '<<<<<<<' before previous hunk closed"
                )
            state, ours_label, start_line = "ours", m_ours.group(1).strip(), i
            _reset()
            continue

        if m_base and state == "ours":
            state, base_label, saw_base_marker = "base", m_base.group(1).strip(), True
            continue

        if m_sep and state in ("ours", "base"):
            state = "theirs"
            continue

        if m_theirs:
            if state != "theirs":
                raise ConflictParseError(f"{file}:{i}: '>>>>>>>' without a matching '======='")
            hunks.append(
                ConflictHunk(
                    file=file,
                    hunk_index=len(hunks),
                    start_line=start_line,
                    end_line=i,
                    ours_label=ours_label,
                    theirs_label=m_theirs.group(1).strip(),
                    ours_text="\n".join(ours_buf),
                    theirs_text="\n".join(theirs_buf),
                    base_text="\n".join(base_buf) if saw_base_marker else None,
                )
            )
            state = "outside"
            continue

        if state == "ours":
            ours_buf.append(line)
        elif state == "base":
            base_buf.append(line)
        elif state == "theirs":
            theirs_buf.append(line)

    if state != "outside":
        raise ConflictParseError(f"{file}: unclosed conflict marker starting at line {start_line}")

    return hunks


def hunks_for_file(
    worktree_path: Path, file: str, base_ref: str, theirs_branch: str
) -> list[ConflictHunk]:
    """Reads the conflicted file out of the worktree, parses it, and fills
    in `base_text` from the real merge-base blob when the in-file markers
    didn't already carry one (i.e. `merge.conflictStyle` isn't `diff3`).
    Falls back gracefully (base_text stays None) if the file didn't exist
    at the merge-base — that's a legitimate "added in one branch" case,
    not an error.
    """
    full_path = worktree_path / file
    try:
        content = full_path.read_text()
    except OSError as e:
        logger.warning("could not read conflicted file %s: %s", full_path, e)
        return []

    hunks = parse_conflicts(content, file)
    if not hunks:
        return hunks

    if any(h.base_text is None for h in hunks):
        base_sha = merge_base(base_ref, theirs_branch)
        if base_sha:
            base_blob = read_blob(base_sha, file, cwd=worktree_path)
            if base_blob is not None:
                # Whole-file base text as a fallback context signal; a
                # per-hunk base without diff3 markers isn't reliably
                # recoverable, so we surface the closest useful thing
                # (the pre-conflict file) rather than nothing.
                hunks = [
                    h if h.base_text is not None else h.model_copy(update={"base_text": base_blob})
                    for h in hunks
                ]

    return hunks
