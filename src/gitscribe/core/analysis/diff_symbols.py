"""
Turns a raw unified diff (as produced by diff_parser.get_raw_diff()) into
the list of symbol_ids touched by it, via index_store.symbol_at().
"""

from __future__ import annotations

import re

from gitscribe.core.indexer.index_store import symbol_at

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def changed_lines_by_file(diff_text: str) -> dict[str, list[int]]:
    """New-file line numbers touched (added or modified) per file.

    Every file that has a `diff --git a/x b/y` header is present in the
    result - with an empty list if it introduces no new lines (a pure
    rename, or a deletion-only hunk). A file is *absent* only when its
    diff genuinely couldn't be attributed to a path at all. This
    distinction matters to callers doing "is this finding on a line the
    change actually introduced" filtering (e.g.
    `validation.policy._is_new`): "absent" must fail toward treating a
    finding as new/in-scope (a parsing gap shouldn't silently exempt a
    file from review), while "present with no lines" is a real, safe
    signal that this file was seen and added nothing new.

    Binary files and outright deletions (`+++ /dev/null`) stop line
    tracking for that file - no text hunk / no new-file line numbers
    apply to them - without discarding the empty entry already recorded
    for it at the `diff --git` header.

    This is the single canonical diff-line parser for the app (see
    validation_layer.md section 3.2/9.6); `validation/policy.py` used to
    duplicate this logic independently and must use this instead.
    """
    result: dict[str, list[int]] = {}
    current_file: str | None = None
    cursor: int | None = None

    for line in diff_text.splitlines():
        if line.startswith("Binary files "):
            current_file = None
            cursor = None
            continue

        m = _DIFF_GIT_RE.match(line)
        if m:
            current_file = m.group(2)
            cursor = None
            result.setdefault(current_file, [])
            continue

        if line.startswith("+++"):
            if line[4:].strip() == "/dev/null":
                # Deletion: the file has no new-file lines, but it was
                # already recorded (with an empty list) at the "diff
                # --git" header above - keep that entry, just stop
                # tracking a cursor for it.
                current_file = None
            continue

        if line.startswith("---"):
            continue

        m = _HUNK_HEADER_RE.match(line)
        if m:
            cursor = int(m.group(1))
            continue

        if cursor is None or current_file is None:
            continue

        if line.startswith("+"):
            result.setdefault(current_file, []).append(cursor)
            cursor += 1
        elif line.startswith("-"):
            pass  # removed line, no new-file line number to advance
        else:
            cursor += 1  # context line, present in both versions

    return result


def split_diff_by_file(diff_text: str) -> dict[str, str]:
    """Splits a multi-file `git diff` into one diff-text chunk per file.
    """
    chunks: dict[str, list[str]] = {}
    current_file: str | None = None

    for line in diff_text.splitlines(keepends=True):
        m = _DIFF_GIT_RE.match(line)
        if m:
            current_file = m.group(2)
            chunks[current_file] = [line]
            continue
        if current_file is not None:
            chunks[current_file].append(line)

    return {f: "".join(lines) for f, lines in chunks.items()}


def changed_symbol_ids(diff_text: str) -> list[int]:
    """Resolve each touched line to its enclosing symbol via symbol_at(),
    deduplicated, in first-seen order."""
    seen: dict[int, None] = {}
    for file, linenos in changed_lines_by_file(diff_text).items():
        for lineno in linenos:
            result = symbol_at(file, lineno)
            if result is not None:
                seen.setdefault(result.symbol_id, None)
    return list(seen)
