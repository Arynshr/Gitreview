from unittest.mock import patch

from gitscribe.core.indexer.index_store import BlastRadiusResult, SearchResult
from gitscribe.core.merge_preview.context import (
    _blast_radius_summary,
    _branch_intent,
    gather_context,
)
from gitscribe.core.merge_preview.models import ConflictHunk

CFG = {"merge_preview": {"blast_radius_depth": 2}}

HUNK = ConflictHunk(
    file="app.py",
    hunk_index=0,
    start_line=10,
    end_line=15,
    ours_label="HEAD",
    theirs_label="feature/x",
    ours_text="return 1",
    theirs_text="return 2",
)


def test_branch_intent_returns_titles_and_summaries_from_memory():
    fake_prs = [
        {"title": "Add auth", "body": "Adds login endpoint"},
        {"title": "Fix auth bug", "body": "Fixes token expiry"},
    ]
    with patch(
        "gitscribe.core.merge_preview.context.memory.fetch_prs_by_branch_prefix",
        return_value=fake_prs,
    ):
        intent = _branch_intent("feature/auth")

    assert intent.branch == "feature/auth"
    assert intent.pr_titles == ["Add auth", "Fix auth bug"]
    assert intent.has_history is True


def test_branch_intent_empty_history_is_not_an_error():
    with patch(
        "gitscribe.core.merge_preview.context.memory.fetch_prs_by_branch_prefix", return_value=[]
    ):
        intent = _branch_intent("brand-new-branch")

    assert intent.has_history is False
    assert intent.pr_titles == []


def test_branch_intent_swallows_memory_lookup_failure():
    """A broken/missing local DB must degrade to "no history", not crash
    the whole merge preview.
    """
    with patch(
        "gitscribe.core.merge_preview.context.memory.fetch_prs_by_branch_prefix",
        side_effect=RuntimeError("db locked"),
    ):
        intent = _branch_intent("feature/x")

    assert intent.has_history is False


def test_blast_radius_summary_returns_symbol_and_related():
    symbol = SearchResult(
        symbol_id=1, name="process", kind="function", file="app.py", lineno=10, score=0.0
    )
    related = [
        BlastRadiusResult(
            symbol_id=2, name="helper", file="app.py", depth=1, direction="callee", lineno=20
        ),
    ]
    with (
        patch("gitscribe.core.merge_preview.context.index_store.symbol_at", return_value=symbol),
        patch(
            "gitscribe.core.merge_preview.context.index_store.blast_radius", return_value=related
        ),
    ):
        name, summary = _blast_radius_summary("app.py", 10, max_depth=2)

    assert name == "process"
    assert "helper" in summary[0]


def test_blast_radius_summary_returns_none_when_symbol_not_found():
    with patch("gitscribe.core.merge_preview.context.index_store.symbol_at", return_value=None):
        name, summary = _blast_radius_summary("unknown.py", 1, max_depth=2)

    assert name is None
    assert summary == []


def test_blast_radius_summary_swallows_index_lookup_failure():
    """No index built yet (`gitscribe index` never run) shouldn't crash
    context gathering — it should just mean less context.
    """
    with patch(
        "gitscribe.core.merge_preview.context.index_store.symbol_at",
        side_effect=Exception("no such table: symbols"),
    ):
        name, summary = _blast_radius_summary("app.py", 10, max_depth=2)

    assert name is None
    assert summary == []


def test_gather_context_assembles_full_hunk_context():
    symbol = SearchResult(
        symbol_id=1, name="process", kind="function", file="app.py", lineno=10, score=0.0
    )
    with (
        patch("gitscribe.core.merge_preview.context.index_store.symbol_at", return_value=symbol),
        patch("gitscribe.core.merge_preview.context.index_store.blast_radius", return_value=[]),
        patch(
            "gitscribe.core.merge_preview.context.memory.fetch_prs_by_branch_prefix",
            return_value=[],
        ),
    ):
        ctx = gather_context(HUNK, CFG, ours_branch="main", theirs_branch="feature/x")

    assert ctx.hunk == HUNK
    assert ctx.symbol_name == "process"
    assert ctx.ours_intent.branch == "main"
    assert ctx.theirs_intent.branch == "feature/x"


def test_gather_context_uses_default_blast_radius_depth_when_unconfigured():
    """cfg without a merge_preview section shouldn't raise a KeyError."""
    with (
        patch("gitscribe.core.merge_preview.context.index_store.symbol_at", return_value=None),
        patch(
            "gitscribe.core.merge_preview.context.memory.fetch_prs_by_branch_prefix",
            return_value=[],
        ),
    ):
        ctx = gather_context(HUNK, {}, ours_branch="main", theirs_branch="feature/x")

    assert ctx.symbol_name is None


def test_hunk_context_as_prompt_block_includes_all_populated_fields():
    symbol = SearchResult(
        symbol_id=1, name="process", kind="function", file="app.py", lineno=10, score=0.0
    )
    related = [
        BlastRadiusResult(
            symbol_id=2, name="helper", file="app.py", depth=1, direction="callee", lineno=20
        )
    ]
    with (
        patch("gitscribe.core.merge_preview.context.index_store.symbol_at", return_value=symbol),
        patch(
            "gitscribe.core.merge_preview.context.index_store.blast_radius", return_value=related
        ),
        patch(
            "gitscribe.core.merge_preview.context.memory.fetch_prs_by_branch_prefix",
            return_value=[{"title": "Refactor process()", "body": "..."}],
        ),
    ):
        ctx = gather_context(HUNK, CFG, ours_branch="main", theirs_branch="feature/x")

    block = ctx.as_prompt_block()
    assert "process" in block
    assert "helper" in block
    assert "Refactor process()" in block
