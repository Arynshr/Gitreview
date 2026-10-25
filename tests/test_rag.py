from unittest.mock import patch

from gitscribe.core.analysis.rag import retrieve
from gitscribe.core.indexer.index_store import BlastRadiusResult, SearchResult


def make_search_result(sid, name, file="a.py", lineno=1, score=0.9):
    return SearchResult(
        symbol_id=sid, name=name, kind="function", file=file, lineno=lineno, score=score
    )


def make_blast_result(sid, name, file="a.py", depth=1, direction="callee"):
    return BlastRadiusResult(symbol_id=sid, name=name, file=file, depth=depth, direction=direction)


@patch("gitscribe.core.analysis.rag.blast_radius")
@patch("gitscribe.core.analysis.rag.search")
def test_retrieve_includes_direct_match(mock_search, mock_blast):
    mock_search.return_value = [make_search_result(1, "target_fn")]
    mock_blast.return_value = []

    ctx = retrieve("find the target function", cfg={})

    assert len(ctx.snippets) == 1
    assert ctx.snippets[0].name == "target_fn"
    assert ctx.snippets[0].relation == "match"


@patch("gitscribe.core.analysis.rag.blast_radius")
@patch("gitscribe.core.analysis.rag.search")
def test_retrieve_uses_real_direction_not_guessed(mock_search, mock_blast):
    """Regression test for the earlier bug where relation was guessed from
    depth ('caller' if depth > 0 else 'callee') instead of using
    blast_radius's actual direction field. Now that blast_radius returns
    real direction, rag.py should pass it through unchanged.
    """
    mock_search.return_value = [make_search_result(1, "root_fn")]
    mock_blast.return_value = [
        make_blast_result(2, "downstream_fn", depth=1, direction="callee"),
        make_blast_result(3, "upstream_fn", depth=1, direction="caller"),
    ]

    ctx = retrieve("query", cfg={})

    relations = {s.name: s.relation for s in ctx.snippets}
    assert relations["downstream_fn"] == "callee"
    assert relations["upstream_fn"] == "caller"


@patch("gitscribe.core.analysis.rag.blast_radius")
@patch("gitscribe.core.analysis.rag.search")
def test_retrieve_dedupes_symbol_appearing_as_match_and_related(mock_search, mock_blast):
    """If a symbol is both a direct match and reachable via blast_radius
    from another match, it should only appear once (first occurrence wins).
    """
    mock_search.return_value = [make_search_result(1, "fn_a"), make_search_result(2, "fn_b")]
    mock_blast.return_value = [make_blast_result(2, "fn_b", direction="callee")]  # already a match

    ctx = retrieve("query", cfg={}, top_k=2)

    ids = [s.symbol_id for s in ctx.snippets]
    assert ids.count(2) == 1


@patch("gitscribe.core.analysis.rag.blast_radius")
@patch("gitscribe.core.analysis.rag.search")
def test_retrieve_dedupes_symbol_reachable_from_multiple_matches(mock_search, mock_blast):
    mock_search.return_value = [make_search_result(1, "fn_a"), make_search_result(2, "fn_b")]
    mock_blast.return_value = [make_blast_result(99, "shared_dep", direction="callee")]

    ctx = retrieve("query", cfg={}, top_k=2)

    shared_occurrences = [s for s in ctx.snippets if s.symbol_id == 99]
    assert len(shared_occurrences) == 1


@patch("gitscribe.core.analysis.rag.blast_radius")
@patch("gitscribe.core.analysis.rag.search")
def test_retrieve_empty_search_returns_empty_context(mock_search, mock_blast):
    mock_search.return_value = []
    ctx = retrieve("nothing matches", cfg={})
    assert ctx.snippets == []
    mock_blast.assert_not_called()


@patch("gitscribe.core.analysis.rag.blast_radius")
@patch("gitscribe.core.analysis.rag.search")
def test_retrieve_passes_top_k_and_expand_depth_through(mock_search, mock_blast):
    mock_search.return_value = [make_search_result(1, "fn")]
    mock_blast.return_value = []

    retrieve("query", cfg={}, top_k=7, expand_depth=3)

    mock_search.assert_called_once()
    assert mock_search.call_args.kwargs.get("top_k") == 7 or mock_search.call_args.args[-1] == 7
    mock_blast.assert_called_once_with(1, max_depth=3)


def test_as_prompt_block_formats_query_and_snippets():
    from gitscribe.core.analysis.rag import ContextSnippet, RAGContext

    ctx = RAGContext(
        query="how does auth work",
        snippets=[
            ContextSnippet(symbol_id=1, name="login", file="auth.py", lineno=10, relation="match"),
            ContextSnippet(
                symbol_id=2, name="hash_pw", file="auth.py", lineno=20, relation="callee"
            ),
        ],
    )
    block = ctx.as_prompt_block()
    assert "how does auth work" in block
    assert "login" in block and "auth.py:10" in block
    assert "[callee]" in block
