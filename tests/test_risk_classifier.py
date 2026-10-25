import json
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from gitscribe.core.risk_classifier import risk_classifier_node, route_after_risk
from gitscribe.core.state import GitScribeState

CFG = {
    "llm": {"model": "llama-3.3-70b-versatile"},
    "risk_classifier": {"enabled": True, "trivial_threshold": 0.15},
}

STATE = GitScribeState(
    files_changed=["api/auth.py"],
    change_summary=["api/auth.py: +40/-2 lines changed"],
    commit_messages=["feat: add oauth login"],
)


def test_risk_classifier_high_risk_does_not_skip():
    fake = FakeListChatModel(
        responses=[json.dumps({"risk_score": 0.9, "reasoning": "touches auth"})]
    )
    with patch("gitscribe.core.risk_classifier.build_chat_model", return_value=fake):
        result = risk_classifier_node(STATE, CFG)

    assert result["risk_score"] == 0.9
    assert result["skip_generation"] is False


def test_risk_classifier_trivial_diff_skips():
    fake = FakeListChatModel(
        responses=[json.dumps({"risk_score": 0.05, "reasoning": "formatting only"})]
    )
    with patch("gitscribe.core.risk_classifier.build_chat_model", return_value=fake):
        result = risk_classifier_node(STATE, CFG)

    assert result["skip_generation"] is True


def test_risk_classifier_disabled_always_proceeds():
    cfg = {**CFG, "risk_classifier": {"enabled": False, "trivial_threshold": 0.15}}
    result = risk_classifier_node(STATE, cfg)
    assert result == {"risk_score": 1.0, "skip_generation": False}


def test_risk_classifier_fails_open_on_llm_error():
    fake = FakeListChatModel(responses=["not json"])
    with patch("gitscribe.core.risk_classifier.build_chat_model", return_value=fake):
        result = risk_classifier_node(STATE, CFG)

    assert result["skip_generation"] is False
    assert "classifier_error" in result["risk_reasoning"]


def test_route_after_risk_skip():
    assert route_after_risk(GitScribeState(skip_generation=True)) == "skip"


def test_route_after_risk_retrieve():
    assert route_after_risk(GitScribeState(skip_generation=False)) == "retrieve"
