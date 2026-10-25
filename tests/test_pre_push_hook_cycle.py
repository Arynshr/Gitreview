"""Regression test for the pre-push hook recursion bug."""

import ast
from pathlib import Path

CLI_PATH = Path(__file__).parent.parent / "src" / "gitscribe" / "cli.py"


def _create_pr_source() -> str:
    tree = ast.parse(CLI_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "create_pr":
            return ast.get_source_segment(CLI_PATH.read_text(), node)
    raise AssertionError("create_pr function not found in cli.py")


def test_create_pr_internal_push_bypasses_hooks():
    src = _create_pr_source()
    assert '"git", "push", "-u", "origin", branch' in src, (
        "expected internal push call not found -- did the push logic move?"
    )
    # The push call must include --no-verify, or it re-fires the pre-push
    # hook and reproduces the infinite loop.
    push_line = next(
        line for line in src.splitlines() if '"git", "push", "-u", "origin", branch' in line
    )
    assert "--no-verify" in push_line, (
        f"create_pr's internal push is missing --no-verify, which causes "
        f"pre-push hook recursion: {push_line!r}"
    )


def test_pre_push_variable_naming_matches_logic():
    """Guards against the has_upstream/no_upstream naming flip regressing:
    the variable gating the recursive create-pr call must be true only
    when there is NO upstream (that's the actual condition being checked
    via `git rev-parse --verify @{u}` returning nonzero)."""
    tree = ast.parse(CLI_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "pre_push_cmd":
            src = ast.get_source_segment(CLI_PATH.read_text(), node)
            assert "no_upstream" in src
            assert "if no_upstream:" in src
            return
    raise AssertionError("pre_push_cmd not found in cli.py")
