from unittest.mock import patch

import pytest

from gitscribe.core.merge_preview.hunk_parser import (
    ConflictParseError,
    hunks_for_file,
    parse_conflicts,
)

SIMPLE_CONFLICT = """\
def greet():
<<<<<<< HEAD
    return "hello"
=======
    return "hi there"
>>>>>>> feature/greeting
"""

TWO_HUNK_CONFLICT = """\
import os
<<<<<<< HEAD
import sys
=======
import json
>>>>>>> feature/imports

def run():
<<<<<<< HEAD
    return os.getcwd()
=======
    return json.dumps({})
>>>>>>> feature/imports
"""

DIFF3_CONFLICT = """\
<<<<<<< HEAD
value = 1
||||||| base_commit
value = 0
=======
value = 2
>>>>>>> feature/value
"""

NO_CONFLICT = """\
def clean():
    return 42
"""


def test_parses_single_hunk_with_correct_boundaries():
    hunks = parse_conflicts(SIMPLE_CONFLICT, "greet.py")
    assert len(hunks) == 1

    h = hunks[0]
    assert h.file == "greet.py"
    assert h.hunk_index == 0
    assert h.ours_label == "HEAD"
    assert h.theirs_label == "feature/greeting"
    assert h.ours_text == '    return "hello"'
    assert h.theirs_text == '    return "hi there"'
    assert h.base_text is None
    # start_line/end_line point at the marker lines themselves (1-indexed)
    assert h.start_line == 2
    assert h.end_line == 6


def test_parses_multiple_hunks_in_order():
    hunks = parse_conflicts(TWO_HUNK_CONFLICT, "run.py")
    assert len(hunks) == 2
    assert [h.hunk_index for h in hunks] == [0, 1]
    assert hunks[0].ours_text == "import sys"
    assert hunks[1].theirs_text == "    return json.dumps({})"
    # second hunk starts after the first one closes
    assert hunks[1].start_line > hunks[0].end_line


def test_diff3_style_captures_base_text():
    hunks = parse_conflicts(DIFF3_CONFLICT, "value.py")
    assert len(hunks) == 1
    assert hunks[0].base_text == "value = 0"
    assert hunks[0].ours_text == "value = 1"
    assert hunks[0].theirs_text == "value = 2"


def test_no_conflict_markers_returns_empty_list():
    assert parse_conflicts(NO_CONFLICT, "clean.py") == []


def test_empty_content_returns_empty_list():
    assert parse_conflicts("", "empty.py") == []


def test_multiline_hunk_bodies_are_preserved():
    content = """\
<<<<<<< HEAD
line one
line two
line three
=======
alt line one
alt line two
>>>>>>> other
"""
    hunks = parse_conflicts(content, "multi.py")
    assert hunks[0].ours_text == "line one\nline two\nline three"
    assert hunks[0].theirs_text == "alt line one\nalt line two"


def test_unclosed_marker_raises_conflict_parse_error():
    broken = "<<<<<<< HEAD\nsome text\n======= \nmore text\n"
    with pytest.raises(ConflictParseError, match="unclosed"):
        parse_conflicts(broken, "broken.py")


def test_theirs_marker_without_separator_raises():
    broken = "<<<<<<< HEAD\nsome text\n>>>>>>> branch\n"
    with pytest.raises(ConflictParseError, match="without a matching"):
        parse_conflicts(broken, "broken.py")


def test_nested_ours_marker_raises():
    broken = "<<<<<<< HEAD\n<<<<<<< nested\n=======\ntext\n>>>>>>> branch\n"
    with pytest.raises(ConflictParseError, match="nested"):
        parse_conflicts(broken, "broken.py")


def test_empty_ours_or_theirs_side_is_valid():
    """One side of a conflict can legitimately be empty (e.g. one branch
    deleted the block the other modified) — not a parse error.
    """
    content = "<<<<<<< HEAD\n=======\nadded content\n>>>>>>> branch\n"
    hunks = parse_conflicts(content, "f.py")
    assert hunks[0].ours_text == ""
    assert hunks[0].theirs_text == "added content"


# --- hunks_for_file: the disk/git-touching wrapper around parse_conflicts ---


def test_hunks_for_file_reads_and_parses_working_tree_file(tmp_path):
    (tmp_path / "greet.py").write_text(SIMPLE_CONFLICT)

    with patch("gitscribe.core.merge_preview.hunk_parser.merge_base", return_value=None):
        hunks = hunks_for_file(
            tmp_path, "greet.py", base_ref="HEAD", theirs_branch="feature/greeting"
        )

    assert len(hunks) == 1
    assert hunks[0].ours_text == '    return "hello"'


def test_hunks_for_file_backfills_base_text_from_merge_base_when_no_diff3(tmp_path):
    (tmp_path / "greet.py").write_text(SIMPLE_CONFLICT)

    with (
        patch("gitscribe.core.merge_preview.hunk_parser.merge_base", return_value="abc123"),
        patch(
            "gitscribe.core.merge_preview.hunk_parser.read_blob",
            return_value="def greet():\n    return None\n",
        ),
    ):
        hunks = hunks_for_file(
            tmp_path, "greet.py", base_ref="HEAD", theirs_branch="feature/greeting"
        )

    assert hunks[0].base_text == "def greet():\n    return None\n"


def test_hunks_for_file_leaves_base_text_none_when_merge_base_unavailable(tmp_path):
    (tmp_path / "greet.py").write_text(SIMPLE_CONFLICT)

    with patch("gitscribe.core.merge_preview.hunk_parser.merge_base", return_value=None):
        hunks = hunks_for_file(
            tmp_path, "greet.py", base_ref="HEAD", theirs_branch="feature/greeting"
        )

    assert hunks[0].base_text is None


def test_hunks_for_file_missing_file_returns_empty_list(tmp_path):
    """Simulates a binary conflict or a file the merge deleted entirely on
    one side — should degrade gracefully, not raise.
    """
    hunks = hunks_for_file(
        tmp_path, "does_not_exist.py", base_ref="HEAD", theirs_branch="feature/x"
    )
    assert hunks == []


def test_hunks_for_file_diff3_style_skips_merge_base_lookup(tmp_path):
    """When the file already has ||||||| markers, hunks_for_file shouldn't
    need to call merge_base()/read_blob() at all.
    """
    (tmp_path / "value.py").write_text(DIFF3_CONFLICT)

    with patch("gitscribe.core.merge_preview.hunk_parser.merge_base") as mock_merge_base:
        hunks = hunks_for_file(tmp_path, "value.py", base_ref="HEAD", theirs_branch="feature/value")

    mock_merge_base.assert_not_called()
    assert hunks[0].base_text == "value = 0"
