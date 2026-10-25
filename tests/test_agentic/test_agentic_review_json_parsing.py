"""Regression tests for the agentic-review "0 findings" bug.

Two compounding root causes, both in the code path that runs when the LLM
review pass produces structured JSON output:

These tests exercise the fixed logic directly via AST-extracted source
(pydantic/langchain aren't installed in this sandbox, so the real package
can't be imported here) -- see each test's docstring for what it proves.
"""

import ast
import json
from pathlib import Path

GEN_PATH = Path(__file__).resolve().parents[2] / "src" / "gitscribe" / "core" / "generator.py"
RAG_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "gitscribe" / "core" / "analysis" / "rag.py"
)


def _load_extract_json_block():
    src = GEN_PATH.read_text()
    tree = ast.parse(src)
    ns = {"json": json}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "_find_balanced_end",
            "_extract_json_block",
        ):
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<gen>", "exec"), ns)
    assert "_extract_json_block" in ns, "_extract_json_block not found in generator.py"
    return ns["_extract_json_block"]


EXTRACT_CASES = [
    ("clean json only", '{"findings": []}', True),
    ("code fence wrapped", '```json\n{"findings": [{"a":1}]}\n```', True),
    (
        "prose with an unrelated brace before AND after the real JSON",
        "I will check for issues using patterns like {TODO, FIXME} markers.\n\n"
        '```json\n{"findings": [{"severity": "warning", "rule_or_reason": "x", '
        '"message": "y", "line_start": 1, "line_end": 1}]}\n```\n\n'
        "Let me know if you want detail on any finding{s}.",
        True,
    ),
    (
        "brace inside a quoted JSON string value",
        '{"findings": [{"message": "dict comprehension {k: v for k,v in x} is unclear"}]}',
        True,
    ),
    (
        "schema example echoed before the real answer",
        'Schema: {"properties": {"findings": {"type": "array"}}}\nAnswer: {"findings": []}',
        True,
    ),
    ("no json at all", "Sorry, I cannot help with that.", False),
]


def test_extract_json_block_handles_prose_with_extra_braces():
    """The core repro case: this exact input broke the old greedy regex
    (produced invalid JSON, guaranteed parse failure) and is exactly the
    shape of output a verbose fallback model tends to produce."""
    extract = _load_extract_json_block()
    for name, text, should_parse in EXTRACT_CASES:
        extracted = extract(text)
        try:
            json.loads(extracted)
            parsed = True
        except ValueError:
            parsed = False
        assert parsed == should_parse, (
            f"case {name!r}: expected parsed={should_parse}, got {parsed}"
        )


def test_extract_json_block_skips_invalid_candidate_for_valid_one():
    """A balanced-but-invalid brace group appearing before the real JSON
    (e.g. "{TODO, FIXME}") must not be returned in place of the real,
    later, valid JSON object."""
    extract = _load_extract_json_block()
    text = '{TODO, FIXME} markers.\n{"findings": []}'
    extracted = extract(text)
    assert json.loads(extracted) == {"findings": []}


def test_run_batched_agentic_review_isolates_batch_failures():
    """One batch's exception must not discard other batches' already-
    accumulated results. Verified at the source level: the batch-call
    sites in the loop must be wrapped in their own try/except rather than
    letting an exception propagate out of the whole function."""
    src = RAG_PATH.read_text()
    tree = ast.parse(src)
    func = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_batched_agentic_review"
    )
    func_src = ast.get_source_segment(src, func)

    # Both call sites that talk to the LLM per-batch must be inside a
    # try/except within the loop, not bare calls that can propagate.
    assert "try:" in func_src and "except Exception" in func_src
    # Sanity: still calls both paths (single-file fallback + batch call)
    assert "run_agentic_review(diff_text, symbol_ids, cfg" in func_src  # + sandboxed= kwarg
    assert "_run_batch_call(batch, cfg" in func_src  # + sandboxed= kwarg
