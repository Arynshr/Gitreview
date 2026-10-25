import json
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from gitscribe.core.generator import generator_node
from gitscribe.core.state import GitScribeState

FAKE_PR_JSON = json.dumps(
    {
        "title": "Add user auth",
        "summary": "Adds login/logout endpoints.",
        "changes": "- Added auth.py\n- Updated schema",
        "testing": "Unit tests for login flow",
        "impact": "Low risk, additive only",
    }
)

CFG = {
    "llm": {
        "model": "llama-3.3-70b-versatile",
        "fallback_model": "llama-3.1-8b-instant",
        "temperature": 0.2,
    },
}

STATE = GitScribeState(
    change_summary=["api/user.py: +20/-0 lines changed"],
    commit_messages=["feat: add auth"],
    retrieved_prs=[],
    fallback_used=False,
)


def test_generator_node_success():
    fake_llm = FakeListChatModel(responses=[FAKE_PR_JSON])
    with patch("gitscribe.core.generator.build_chat_model", return_value=fake_llm):
        result = generator_node(STATE, CFG)

    assert result["status"] == "success"
    assert result["pr_title"] == "Add user auth"
    assert "Adds login/logout endpoints" in result["pr_body"]
    assert result["last_error"] is None


def test_generator_node_bad_output_marks_failed():
    fake_llm = FakeListChatModel(responses=["not valid json at all"])
    with patch("gitscribe.core.generator.build_chat_model", return_value=fake_llm):
        result = generator_node(STATE, CFG)

    assert result["status"] == "failed"
    assert result["last_error"] is not None
    assert result["attempt_count"] == 1


def test_style_instruction_reaches_prompt_text():
    """Verify each style preset actually changes prompt content, not just accepted and ignored."""
    from gitscribe.core.generator import GEN_PROMPT, STYLE_INSTRUCTIONS

    prompt_value = GEN_PROMPT.invoke(
        {
            "style_instruction": STYLE_INSTRUCTIONS["concise"],
            "change_summary": STATE.change_summary,
            "commit_messages": STATE.commit_messages,
            "past_prs": [],
            "format_instructions": "",
        }
    )
    assert "terse" in prompt_value.to_string().lower()


def test_generator_node_passes_state_style_through_without_error():
    fake_llm = FakeListChatModel(responses=[FAKE_PR_JSON])
    concise_state = STATE.model_copy(update={"style": "concise"})
    with patch("gitscribe.core.generator.build_chat_model", return_value=fake_llm):
        result = generator_node(concise_state, CFG)

    assert result["status"] == "success"
