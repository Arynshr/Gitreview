import json
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from gitscribe.core.merge_preview.context import gather_context
from gitscribe.core.merge_preview.models import BranchIntent, ConflictHunk, HunkContext
from gitscribe.core.merge_preview.resolver import resolve_file

CFG = {
    "llm": {
        "model": "openai/gpt-oss-20b",
        "fallback_model": "openai/gpt-oss-20b",
        "temperature": 0.2,
    },
    "merge_preview": {"escalate_below_confidence": "high"},
}


def _hunk(index: int, file: str = "app.py") -> ConflictHunk:
    return ConflictHunk(
        file=file,
        hunk_index=index,
        start_line=10 + index,
        end_line=15 + index,
        ours_label="HEAD",
        theirs_label="feature/x",
        ours_text=f"ours_{index}",
        theirs_text=f"theirs_{index}",
    )


def _ctx(index: int, file: str = "app.py") -> HunkContext:
    return HunkContext(
        hunk=_hunk(index, file),
        symbol_name=None,
        blast_radius_summary=[],
        ours_intent=BranchIntent(branch="main"),
        theirs_intent=BranchIntent(branch="feature/x"),
    )


def _batch_json(entries: list[dict]) -> str:
    return json.dumps({"resolutions": entries})


def _single_json(**kwargs) -> str:
    return json.dumps(kwargs)


def test_all_high_confidence_batch_needs_no_escalation():
    contexts = [_ctx(0), _ctx(1)]
    batch_response = _batch_json(
        [
            {
                "hunk_index": 0,
                "resolved_text": "merged_0",
                "rationale": "trivial",
                "confidence": "high",
            },
            {
                "hunk_index": 1,
                "resolved_text": "merged_1",
                "rationale": "trivial",
                "confidence": "high",
            },
        ]
    )
    fake_llm = FakeListChatModel(responses=[batch_response])

    with patch("gitscribe.core.merge_preview.resolver.build_chat_model", return_value=fake_llm):
        report = resolve_file(contexts, CFG)

    assert report.file == "app.py"
    assert report.batch_call_count == 1
    assert report.escalation_call_count == 0
    assert all(r.confidence == "high" for r in report.resolutions)
    assert all(not r.escalated for r in report.resolutions)
    assert {r.resolved_text for r in report.resolutions} == {"merged_0", "merged_1"}


def test_low_confidence_hunk_is_escalated_and_replaced():
    contexts = [_ctx(0), _ctx(1)]
    batch_response = _batch_json(
        [
            {
                "hunk_index": 0,
                "resolved_text": "merged_0",
                "rationale": "trivial",
                "confidence": "high",
            },
            {
                "hunk_index": 1,
                "resolved_text": "unsure_1",
                "rationale": "ambiguous",
                "confidence": "low",
            },
        ]
    )
    escalated_response = _single_json(
        resolved_text="better_merged_1", rationale="resolved after closer look", confidence="medium"
    )
    fake_llm = FakeListChatModel(responses=[batch_response, escalated_response])

    with patch("gitscribe.core.merge_preview.resolver.build_chat_model", return_value=fake_llm):
        report = resolve_file(contexts, CFG)

    assert report.batch_call_count == 1
    assert report.escalation_call_count == 1

    by_index = {r.hunk_index: r for r in report.resolutions}
    assert by_index[0].escalated is False
    assert by_index[0].confidence == "high"
    assert by_index[1].escalated is True
    assert by_index[1].resolved_text == "better_merged_1"
    assert by_index[1].confidence == "medium"


def test_medium_confidence_threshold_accepts_medium_without_escalation():
    """escalate_below_confidence='medium' should only escalate 'low',
    accepting 'medium' batch results outright.
    """
    cfg = {**CFG, "merge_preview": {"escalate_below_confidence": "medium"}}
    contexts = [_ctx(0)]
    batch_response = _batch_json(
        [
            {
                "hunk_index": 0,
                "resolved_text": "merged_0",
                "rationale": "ok",
                "confidence": "medium",
            },
        ]
    )
    fake_llm = FakeListChatModel(responses=[batch_response])

    with patch("gitscribe.core.merge_preview.resolver.build_chat_model", return_value=fake_llm):
        report = resolve_file(contexts, cfg)

    assert report.escalation_call_count == 0
    assert report.resolutions[0].confidence == "medium"
    assert report.resolutions[0].escalated is False


def test_hunk_missing_from_batch_response_is_escalated():
    """If the model drops a hunk from its JSON response entirely, it must
    still be escalated, not silently disappear from the report.
    """
    contexts = [_ctx(0), _ctx(1)]
    batch_response = _batch_json(
        [
            {"hunk_index": 0, "resolved_text": "merged_0", "rationale": "ok", "confidence": "high"},
            # hunk_index 1 omitted entirely
        ]
    )
    escalated_response = _single_json(
        resolved_text="merged_1", rationale="isolated pass", confidence="high"
    )
    fake_llm = FakeListChatModel(responses=[batch_response, escalated_response])

    with patch("gitscribe.core.merge_preview.resolver.build_chat_model", return_value=fake_llm):
        report = resolve_file(contexts, CFG)

    assert len(report.resolutions) == 2
    by_index = {r.hunk_index: r for r in report.resolutions}
    assert by_index[1].escalated is True
    assert by_index[1].resolved_text == "merged_1"


def test_batch_call_failure_escalates_every_hunk():
    """A completely broken/unparseable batch response must fail open:
    every hunk gets escalated individually rather than crashing the
    preview or silently reporting nothing.
    """
    contexts = [_ctx(0), _ctx(1)]
    fake_llm = FakeListChatModel(
        responses=[
            "not valid json at all",  # batch call fails to parse
            _single_json(resolved_text="merged_0", rationale="ok", confidence="high"),
            _single_json(resolved_text="merged_1", rationale="ok", confidence="high"),
        ]
    )

    with patch("gitscribe.core.merge_preview.resolver.build_chat_model", return_value=fake_llm):
        report = resolve_file(contexts, CFG)

    assert report.escalation_call_count == 2
    assert all(r.escalated for r in report.resolutions)
    assert {r.resolved_text for r in report.resolutions} == {"merged_0", "merged_1"}


def test_escalation_call_also_failing_produces_manual_review_fallback():
    """Both the batch AND the per-hunk escalation fail — must still return
    a report (empty resolved_text, confidence='low') rather than raising,
    so the CLI always has something to show the user.
    """
    contexts = [_ctx(0)]
    fake_llm = FakeListChatModel(responses=["garbage", "still garbage"])

    with patch("gitscribe.core.merge_preview.resolver.build_chat_model", return_value=fake_llm):
        report = resolve_file(contexts, CFG)

    assert len(report.resolutions) == 1
    assert report.resolutions[0].confidence == "low"
    assert report.resolutions[0].resolved_text == ""
    assert "failed" in report.resolutions[0].rationale.lower()


def test_resolve_file_empty_contexts_returns_empty_report():
    report = resolve_file([], CFG)
    assert report.resolutions == []
    assert report.batch_call_count == 0


def test_file_report_helper_properties():
    contexts = [_ctx(0), _ctx(1), _ctx(2)]
    batch_response = _batch_json(
        [
            {"hunk_index": 0, "resolved_text": "m0", "rationale": "ok", "confidence": "high"},
            {"hunk_index": 1, "resolved_text": "m1", "rationale": "ok", "confidence": "medium"},
            {"hunk_index": 2, "resolved_text": "m2", "rationale": "ok", "confidence": "low"},
        ]
    )
    escalated_1 = _single_json(resolved_text="m1b", rationale="ok", confidence="medium")
    escalated_2 = _single_json(resolved_text="m2b", rationale="ok", confidence="high")
    fake_llm = FakeListChatModel(responses=[batch_response, escalated_1, escalated_2])

    with patch("gitscribe.core.merge_preview.resolver.build_chat_model", return_value=fake_llm):
        report = resolve_file(contexts, CFG)

    assert report.safe_count == 2  # hunk 0 (batch, high) + hunk 2 (escalated to high)
    assert len(report.needs_manual_review) == 1  # hunk 1, escalated but still only medium


def test_uses_real_gather_context_end_to_end_smoke():
    """Light integration check: a HunkContext built via the real
    context.gather_context() (with index_store/memory mocked) flows
    through resolve_file() without shape mismatches.
    """
    hunk = _hunk(0)
    with (
        patch("gitscribe.core.merge_preview.context.index_store.symbol_at", return_value=None),
        patch(
            "gitscribe.core.merge_preview.context.memory.fetch_prs_by_branch_prefix",
            return_value=[],
        ),
    ):
        ctx = gather_context(hunk, CFG, ours_branch="main", theirs_branch="feature/x")

    batch_response = _batch_json(
        [
            {"hunk_index": 0, "resolved_text": "merged", "rationale": "ok", "confidence": "high"},
        ]
    )
    fake_llm = FakeListChatModel(responses=[batch_response])

    with patch("gitscribe.core.merge_preview.resolver.build_chat_model", return_value=fake_llm):
        report = resolve_file([ctx], CFG)

    assert report.resolutions[0].resolved_text == "merged"
