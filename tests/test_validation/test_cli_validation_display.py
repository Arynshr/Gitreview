"""Unit: `generate`/`create-pr` must show *what* validation found."""

from __future__ import annotations

import pytest

from gitscribe.cli import _apply_validate_flag, _show_validation


def _fail(**kw) -> dict:
    return {
        "validation_status": "FAIL",
        "validation_summary": "FAIL: 2 finding(s)",
        "validation_findings": [("critical", "a.py:1 - bad"), ("medium", "b.py:2 - meh")],
        "validation_findings_count": 2,
        **kw,
    }


@pytest.mark.parametrize("status", [None, "PASS", "SKIPPED"])
def test_nothing_is_printed_unless_validation_failed(capsys, status) -> None:
    _show_validation({"validation_status": status})

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_failure_lists_each_finding_with_severity_and_a_pointer_to_the_full_report(capsys) -> None:
    _show_validation(_fail())

    captured = capsys.readouterr()
    assert "validation FAIL: FAIL: 2 finding(s)" in captured.err
    assert "[CRITICAL] a.py:1 - bad" in captured.out and "[MEDIUM] b.py:2 - meh" in captured.out
    assert "gitscribe verify" in captured.out and "--no-validate" in captured.out
    assert "more" not in captured.out


def test_overflow_is_counted(capsys) -> None:
    _show_validation(_fail(validation_findings_count=13))

    assert "... 11 more" in capsys.readouterr().out


def test_error_status_is_shown_too(capsys) -> None:
    _show_validation(
        {
            "validation_status": "ERROR",
            "validation_summary": "boom",
            "validation_findings": [("error", "ai: down")],
        }
    )

    captured = capsys.readouterr()
    assert "validation ERROR: boom" in captured.err and "[ERROR] ai: down" in captured.out


@pytest.mark.parametrize("flag,expected", [(None, "keep"), (False, False), (True, True)])
def test_validate_flag_overrides_config_only_when_given(flag, expected) -> None:
    cfg = _apply_validate_flag({"validation": {"enabled": "keep"}}, flag)

    assert cfg["validation"]["enabled"] == expected


def test_validate_flag_creates_the_section_when_absent() -> None:
    assert _apply_validate_flag({}, False) == {"validation": {"enabled": False}}
