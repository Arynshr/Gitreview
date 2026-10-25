from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from gitscribe.core.graph import build_graph, template_fallback_node
from gitscribe.core.state import GitScribeState

CFG = {
    "llm": {"model": "m1", "fallback_model": "m2", "temperature": 0.2},
    "retrieval": {"min_prs": 3, "max_prs": 3},
    "risk_classifier": {"enabled": True, "trivial_threshold": 0.15},
    "failure_handling": {"max_retries": 1, "retry_backoff_seconds": 0},
    "ignore_patterns": [],
}


def test_template_fallback_status_is_skipped_for_trivial_diff():
    """Risk-classifier skip path is not a failure - status must say so."""
    state = GitScribeState(
        skip_generation=True, files_changed=["a.py"], change_summary=["tiny tweak"]
    )
    result = template_fallback_node(state)
    assert result["status"] == "skipped"


def test_template_fallback_status_is_failed_after_exhausted_retries():
    """Regression test for the bug where template_fallback_node stamped
    status="success" unconditionally, making a genuinely failed LLM
    generation indistinguishable from a real success. This is the exact
    bug that let `create-pr` silently open a real GitHub PR with a generic
    "Automated fallback description" body with zero indication the LLM
    had failed."""
    state = GitScribeState(
        skip_generation=False,
        files_changed=["a.py"],
        change_summary=["some change"],
        last_error="could not parse pydantic model",
        failure_type="bad_output",
        attempt_count=2,
    )
    result = template_fallback_node(state)
    assert result["status"] == "failed"


def test_full_graph_reaches_failed_status_when_llm_always_fails():
    """End-to-end through the real compiled graph (not just the node in
    isolation): a generator that never produces valid JSON must surface as
    status="failed" on the final state, with last_error/failure_type
    populated, after failure_handling.max_retries is exhausted."""
    always_bad = FakeListChatModel(responses=["not json at all"] * 5)

    with (
        patch("gitscribe.core.risk_classifier.build_chat_model", return_value=always_bad),
        patch("gitscribe.core.retriever.build_chat_model", return_value=always_bad),
        patch("gitscribe.core.generator.build_chat_model", return_value=always_bad),
        patch(
            "gitscribe.core.diff_parser.get_raw_diff", return_value="diff --git a/x.py b/x.py\n+1"
        ),
        patch("gitscribe.core.diff_parser.get_commit_messages", return_value=["feat: x"]),
        patch("gitscribe.core.diff_parser.extract_files_changed", return_value=["x.py"]),
        patch("gitscribe.core.memory.fetch_prs_by_branch_prefix", return_value=[]),
    ):
        graph = build_graph(CFG)
        result = graph.invoke(
            GitScribeState(branch_name="feature/x", attempt_count=0, status="pending")
        )

    assert result["status"] == "failed"
    assert result["last_error"]
    assert result["failure_type"] in ("rate_limit", "timeout", "bad_output")
    assert "Automated fallback description" in result["pr_body"]
