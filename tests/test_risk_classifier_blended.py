import json
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from gitscribe.core.risk_classifier import risk_classifier_node_blended
from gitscribe.core.state import GitScribeState

CFG_BLENDED = {
    "llm": {"model": "llama-3.3-70b-versatile"},
    "risk_classifier": {"enabled": True, "trivial_threshold": 0.15, "structural_weight": 0.3},
}

STATE_WITH_DIFF = GitScribeState(
    raw_diff="diff --git a/api/auth.py b/api/auth.py\n"
    "--- a/api/auth.py\n+++ b/api/auth.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def login():\n"
    "+    return eval(user_input)\n"
    "     pass\n",
    files_changed=["api/auth.py"],
    change_summary=["api/auth.py: +1 line"],
)


def test_blend_falls_back_to_llm_score_when_no_index_exists():
    """Regression test: with no code index built (or the diff not mapping
    to any known symbol), the structural signal is *unavailable* and must
    not be blended in as zero risk — that would silently understate real
    risk on every unindexed repo.
    """
    fake = FakeListChatModel(
        responses=[json.dumps({"risk_score": 0.9, "reasoning": "eval() is dangerous"})]
    )
    with patch("gitscribe.core.risk_classifier.build_chat_model", return_value=fake):
        with patch("gitscribe.core.risk_classifier.changed_symbol_ids", return_value=[]):
            result = risk_classifier_node_blended(STATE_WITH_DIFF, CFG_BLENDED)

    assert result["risk_score"] == 0.9, (
        "score must be unblended when structural signal is unavailable"
    )
    assert "unavailable" in result["risk_reasoning"]


def test_blend_applies_when_symbols_resolve():
    fake = FakeListChatModel(
        responses=[json.dumps({"risk_score": 0.9, "reasoning": "eval() is dangerous"})]
    )
    with patch("gitscribe.core.risk_classifier.build_chat_model", return_value=fake):
        with patch("gitscribe.core.risk_classifier.changed_symbol_ids", return_value=[1]):
            with patch(
                "gitscribe.core.risk_classifier._structural_signal",
                return_value=(0.0, "no errors, radius 0"),
            ):
                result = risk_classifier_node_blended(STATE_WITH_DIFF, CFG_BLENDED)

    # weight=0.3, llm=0.9, structural=0.0 -> 0.7*0.9 + 0.3*0.0 = 0.63
    assert result["risk_score"] == 0.63
    assert "structural, weight=0.3" in result["risk_reasoning"]


def test_blend_with_zero_weight_matches_unblended_node():
    cfg = {
        **CFG_BLENDED,
        "risk_classifier": {**CFG_BLENDED["risk_classifier"], "structural_weight": 0.0},
    }
    fake = FakeListChatModel(responses=[json.dumps({"risk_score": 0.5, "reasoning": "moderate"})])
    with patch("gitscribe.core.risk_classifier.build_chat_model", return_value=fake):
        result = risk_classifier_node_blended(STATE_WITH_DIFF, cfg)
    assert result["risk_score"] == 0.5
