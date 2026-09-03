"""
Query -> embed -> vector search -> graph-expand -> assembled context.
Replaces/augments today's branch-prefix PR retrieval with code-aware
context.
"""

from __future__ import annotations

import json
import time
import types
import urllib.error
import urllib.parse
import urllib.request

from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field

from gitscribe.core.analysis.diff_symbols import changed_symbol_ids
from gitscribe.core.failure_router import classify_failure
from gitscribe.core.generator import _extract_json_block
from gitscribe.core.indexer.index_store import SearchResult, _get_connection, blast_radius, search
from gitscribe.core.telemetry import logger


class ContextSnippet(BaseModel):
    symbol_id: int
    name: str
    file: str
    lineno: int
    relation: str


class RAGContext(BaseModel):
    query: str
    snippets: list[ContextSnippet]

    def as_prompt_block(self) -> str:
        """Formats retrieved context for injection into generator.py's
        prompt template — plain text, no framework coupling.
        """
        lines = [f"# Relevant code context for: {self.query}"]
        for s in self.snippets:
            lines.append(f"- [{s.relation}] {s.name} ({s.file}:{s.lineno})")
        return "\n".join(lines)


def retrieve(query: str, cfg: dict, top_k: int = 5, expand_depth: int = 1) -> RAGContext:
    matches: list[SearchResult] = search(query, cfg, top_k=top_k)

    snippets: list[ContextSnippet] = []
    seen_ids: set[int] = set()

    for m in matches:
        if m.symbol_id in seen_ids:
            continue
        seen_ids.add(m.symbol_id)
        snippets.append(
            ContextSnippet(symbol_id=m.symbol_id, name=m.name, file=m.file, lineno=m.lineno, relation="match")
        )

        for related in blast_radius(m.symbol_id, max_depth=expand_depth):
            if related.symbol_id in seen_ids:
                continue
            seen_ids.add(related.symbol_id)
            snippets.append(
                ContextSnippet(
                    symbol_id=related.symbol_id,
                    name=related.name,
                    file=related.file,
                    lineno=related.lineno,
                    relation=related.direction,
                )
            )

    return RAGContext(query=query, snippets=snippets)


_SYNTHESIS_PROMPT = (
    "Answer the question using ONLY the code context below. Cite the specific "
    "symbol name(s) and file:line for anything you state. If the context doesn't "
    "contain enough information to answer, say so plainly instead of guessing.\n\n"
    "{context_block}\n\n"
    "Question: {query}"
)


def answer_query(query: str, context: RAGContext, cfg: dict):
    """Synthesizes a natural-language answer to `query`, grounded in the
    already-retrieved `context`, via `llm_client.build_chat_model()`
    (provider-agnostic, same BYOK pattern as the rest of the codebase).
    Takes a pre-built `RAGContext` so callers can skip the LLM call when
    there's nothing to ground an answer in. Raises whatever
    `build_chat_model`/`model.invoke` raise — callers decide how to
    surface or fall back.
    """
    from gitscribe.core.llm_client import build_chat_model

    model_name = cfg.get("llm", {}).get("model", "llama-3.3-70b-versatile")
    model = build_chat_model(cfg, model_name=model_name, temperature=0.1)

    prompt = _SYNTHESIS_PROMPT.format(context_block=context.as_prompt_block(), query=query)
    response = model.invoke(prompt)
    return response.content

_TOKENS_PER_CHAR = 0.25  # rough estimate; good enough for a hard budget cap


def _estimate_tokens(text: str) -> int:
    return int(len(text) * _TOKENS_PER_CHAR)


class _SandboxedReviewError(RuntimeError):
    pass


class _SandboxedChatModel:
    """Minimal `.invoke(prompt) -> obj.content` shim around the local,
    loopback-only llama.cpp server used by the validation layer
    (gitscribe/validation/ai.py) — same request shape and same
    localhost-only enforcement, reimplemented narrowly here rather than
    imported so that `core/analysis` doesn't take a hard dependency on
    `validation`'s review internals (only its config loader is reused,
    see `_sandboxed_ai_config` below).

    Exists purely so `run_agentic_review`/`_run_batch_call` can swap this
    in for `build_chat_model(...)` without changing their own call sites
    or retry logic at all.
    """

    def __init__(self, ai_cfg: dict) -> None:
        self._ai_cfg = ai_cfg

    def invoke(self, prompt: str) -> types.SimpleNamespace:
        import os

        parsed = urllib.parse.urlparse(
            str(self._ai_cfg.get("base_url", "http://127.0.0.1:8080"))
        )
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise _SandboxedReviewError(
                "sandboxed agentic review endpoint must be local HTTP on "
                "localhost/127.0.0.1"
            )
        base_url = str(self._ai_cfg.get("base_url", "http://127.0.0.1:8080")).rstrip("/")

        payload = {
            "model": str(self._ai_cfg.get("model", "qwen2.5-coder-3b-instruct-q4_k_m")),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": 0.1,
            "max_tokens": int(self._ai_cfg.get("max_output_tokens", 1200)),
        }

        headers = {"Content-Type": "application/json"}
        api_key_env = str(self._ai_cfg.get("api_key_env", "VALIDATION_API_KEY"))
        api_key = os.environ.get(api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        request = urllib.request.Request(
            f"{base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request, timeout=float(self._ai_cfg.get("timeout_seconds", 120))
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise _SandboxedReviewError(f"sandboxed agentic review failed: {exc}") from exc

        choices = body.get("choices") or [{}]
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise _SandboxedReviewError("sandboxed agentic review returned no content")

        return types.SimpleNamespace(content=content)


def _sandboxed_ai_config(cfg: dict) -> dict:
    """Reuses validation/config.py's already-battle-tested loader for the
    local llama.cpp settings (base_url/model/api_key_env) instead of
    duplicating that config surface here. Imported lazily, and only on
    the opt-in sandboxed path, so legacy agentic review never depends on
    `validation` being present or correctly configured.

    `timeout_seconds` is overridable separately via
    `review.agentic.sandboxed_timeout_seconds`: validation.ai's timeout
    is tuned for a single-diff security review, but this path batches
    several files into one prompt and can legitimately take much longer
    on CPU inference — reusing the same budget for both is wrong.
    """
    from gitscribe.validation.config import load_validation_config

    ai_cfg = dict(load_validation_config("config.yaml")["ai"])

    override = cfg.get("review", {}).get("agentic", {}).get("sandboxed_timeout_seconds")
    if override:
        ai_cfg["timeout_seconds"] = override

    return ai_cfg


def _build_llm(cfg: dict, model_name: str, temperature: float, sandboxed: bool):
    if sandboxed:
        # A single local model backs the sandboxed path — there is no
        # separate "fallback" local model to escalate to on bad_output,
        # unlike the cloud model_name/fallback_model pair below.
        return _SandboxedChatModel(_sandboxed_ai_config(cfg))

    from gitscribe.core.llm_client import build_chat_model

    return build_chat_model(cfg, model_name, temperature=temperature)


class ReviewFindingLLM(BaseModel):
    severity: str = Field(description="one of: info, warning, error")
    rule_or_reason: str = Field(description="short label for what triggered this finding")
    message: str = Field(description="the finding itself, specific and actionable")
    line_start: int | None = None
    line_end: int | None = None


class ReviewFindingsLLM(BaseModel):
    findings: list[ReviewFindingLLM] = Field(default_factory=list)


_REVIEW_PARSER = PydanticOutputParser(pydantic_object=ReviewFindingsLLM)

_REVIEW_PROMPT = """You are reviewing a code change for correctness, risk, and \
maintainability issues a linter would miss.

Diff hunk (never omit or summarize — review this exactly as given):
{diff_hunk}

Direct callers of the changed code:
{callers_block}

Direct callees of the changed code:
{callees_block}

Same-file siblings (may be omitted below due to context budget):
{siblings_block}

Respond with findings only where you have a concrete, specific concern.
{format_instructions}
"""


def _assemble_review_context(
    diff_hunk: str, changed_symbol_ids: list[int], hops: int, max_context_tokens: int
) -> dict[str, str]:
    """Fixed assembly order (spec §3.3): diff hunk (never truncated) ->
    callers -> callees -> siblings, dropped in that reverse priority when
    over budget."""
    callers: list[str] = []
    callees: list[str] = []
    siblings: list[str] = []

    for sid in changed_symbol_ids:
        for r in blast_radius(sid, max_depth=hops):
            line = f"{r.name} ({r.file}:{r.lineno}, depth={r.depth})"
            (callers if r.direction == "caller" else callees).append(line)

    conn = _get_connection()
    for sid in changed_symbol_ids:
        row = conn.execute("SELECT file FROM symbols WHERE id = ?", (sid,)).fetchone()
        if row is None:
            continue
        sib_rows = conn.execute(
            "SELECT name, lineno FROM symbols WHERE file = ? AND id != ?", (row["file"], sid)
        ).fetchall()
        siblings.extend(f"{r['name']} (line {r['lineno']})" for r in sib_rows)

    budget = max_context_tokens - _estimate_tokens(diff_hunk)
    blocks = {"callers_block": "", "callees_block": "", "siblings_block": ""}

    # Priority: callers, then callees, then siblings — truncate siblings first.
    for key, items in (("callers_block", callers), ("callees_block", callees), ("siblings_block", siblings)):
        text = "\n".join(items) or "(none)"
        cost = _estimate_tokens(text)
        if cost > budget:
            kept = items[:]
            while kept and _estimate_tokens("\n".join(kept)) > budget:
                kept.pop()
            text = "\n".join(kept) if kept else "(omitted — over context budget)"
            budget = max(budget - _estimate_tokens(text), 0)
        else:
            budget -= cost
        blocks[key] = text

    return blocks


def run_agentic_review(
    diff_hunk: str,
    changed_symbol_ids: list[int],
    cfg: dict,
    sandboxed: bool = False,
) -> list[ReviewFindingLLM]:
    """Runs the agentic review pass. Retries are routed through
    failure_router.classify_failure instead of a second failure path, per
    the spec's explicit constraint.

    `sandboxed=True` routes this call through the local, loopback-only
    llama.cpp server instead of the cloud BYOK provider. Default is
    False: unchanged behavior, unchanged call path.
    """
    review_cfg = cfg.get("review", {}).get("agentic", {})
    hops = review_cfg.get("hops", 2)
    max_context_tokens = review_cfg.get("max_context_tokens", 8000)
    max_retries = cfg.get("failure_handling", {}).get("max_retries", 2)

    diff_budget = int(max_context_tokens * 0.6)  # leave room for context + prompt scaffolding
    if _estimate_tokens(diff_hunk) > diff_budget:
        max_chars = int(diff_budget / _TOKENS_PER_CHAR)
        diff_hunk = diff_hunk[:max_chars] + "\n[... diff truncated, exceeded per-call token budget ...]"

    context = _assemble_review_context(diff_hunk, changed_symbol_ids, hops, max_context_tokens)
    prompt = _REVIEW_PROMPT.format(
        diff_hunk=diff_hunk,
        format_instructions=_REVIEW_PARSER.get_format_instructions(),
        **context,
    )

    model_name = cfg["llm"]["model"]
    fallback_model = cfg["llm"].get("fallback_model", model_name)
    backoff = cfg.get("failure_handling", {}).get("retry_backoff_seconds", 0)
    last_error: Exception | None = None
    failure_type: str | None = None
    for attempt in range(max_retries + 1):
        # bad_output on the previous attempt switches to fallback_model
        # (mirrors graph.py's retry_fallback_model_node) instead of
        # retrying the same model against the same malformed-output
        # failure, which rarely helps. Not meaningful when sandboxed —
        # there's only one local model — so it's a no-op there.
        current_model = fallback_model if failure_type == "bad_output" else model_name
        try:
            llm = _build_llm(cfg, current_model, temperature=0.1, sandboxed=sandboxed)
            ai_msg = llm.invoke(prompt)
            cleaned = _extract_json_block(ai_msg.content)
            parsed: ReviewFindingsLLM = _REVIEW_PARSER.invoke(cleaned)
            return parsed.findings
        except Exception as exc:
            last_error = exc
            failure_type = classify_failure(str(exc))
            if attempt == max_retries:
                break
            if failure_type in ("rate_limit", "timeout") and backoff:
                time.sleep(backoff)
    if last_error is not None:
        raise last_error
    return []


def write_agentic_findings(findings: list[ReviewFindingLLM], symbol_id: int | None) -> int:
    conn = _get_connection()
    for f in findings:
        conn.execute(
            """INSERT INTO review_findings
               (symbol_id, source, severity, rule_or_reason, message, line_start, line_end)
               VALUES (?, 'agentic', ?, ?, ?, ?, ?)""",
            (symbol_id, f.severity, f.rule_or_reason, f.message, f.line_start, f.line_end),
        )
    conn.commit()
    return len(findings)


class _BatchReviewFindingLLM(ReviewFindingLLM):
    file: str = Field(description="which file this finding applies to")


class _BatchReviewResponse(BaseModel):
    findings: list[_BatchReviewFindingLLM] = Field(default_factory=list)


_BATCH_REVIEW_PARSER = PydanticOutputParser(pydantic_object=_BatchReviewResponse)

_BATCH_REVIEW_PROMPT = """You are reviewing several files changed in one commit for correctness, \
risk, and maintainability issues a linter would miss.

{files_block}

Respond with findings only where you have a concrete, specific concern. Every \
finding MUST include which file it applies to.
{format_instructions}
"""


def _should_escalate_to_agentic(symbol_ids: list[int], min_blast_radius: int | None = None) -> bool:
    """The gate: True means "worth an LLM call", False means "lint signal
    is clean, skip it". Unresolvable diffs (empty symbol_ids — e.g. a
    brand-new file not yet indexed) can't be gated safely, so they default
    to True rather than silently never being reviewed.
    """
    if not symbol_ids:
        return True

    conn = _get_connection()
    placeholders = ",".join("?" * len(symbol_ids))
    error_count = conn.execute(
        f"""SELECT COUNT(*) AS n FROM review_findings
            WHERE severity = 'error' AND symbol_id IN ({placeholders})""",
        symbol_ids,
    ).fetchone()["n"]
    if error_count > 0:
        return True

    if min_blast_radius is None:
        return False

    max_radius = max((len(blast_radius(sid, max_depth=3)) for sid in symbol_ids), default=0)
    return max_radius >= min_blast_radius


def _light_context_block(changed_symbol_ids: list[int], hops: int) -> str:
    """Cheaper than _assemble_review_context: names only, no siblings, no
    per-block truncation bookkeeping — batched calls already pack several
    files per prompt, so each file's own context share needs to stay small.
    """
    names: list[str] = []
    for sid in changed_symbol_ids:
        for r in blast_radius(sid, max_depth=hops):
            names.append(f"{r.direction}:{r.name}")
    return ", ".join(names[:15]) if names else "(none)"


def _reserved_prompt_overhead(cfg: dict, sandboxed: bool) -> int:
    """Static prompt template + format_instructions cost, plus the
    response token budget for whichever path is active. Both were
    previously unaccounted for in `_pack_into_batches`, which only
    budgeted per-file diff size — so a batch could be "packed" within
    max_context_tokens and still exceed the server's real context
    window once the shared template and the model's own response were
    added on top.
    """
    static_overhead = _estimate_tokens(_BATCH_REVIEW_PROMPT) + _estimate_tokens(
        _BATCH_REVIEW_PARSER.get_format_instructions()
    )

    if sandboxed:
        response_budget = int(_sandboxed_ai_config(cfg).get("max_output_tokens", 1200))
    else:
        response_budget = int(cfg.get("llm", {}).get("max_tokens", 1000))

    return static_overhead + response_budget


def _pack_into_batches(
    items: list[tuple[str, str, list[int]]], max_tokens: int
) -> list[list[tuple[str, str, list[int]]]]:
    """Greedy bin-packing by estimated token size. `items` is
    (file, diff_text, symbol_ids). Oversized single items go in their own
    batch and get truncated later by the per-file fallback, not dropped.

    `max_tokens` here is expected to already be the *effective* budget
    (max_context_tokens minus `_reserved_prompt_overhead`), not the raw
    context window — callers reserve headroom before calling this.
    """
    batches: list[list[tuple[str, str, list[int]]]] = []
    current: list[tuple[str, str, list[int]]] = []
    current_tokens = 0

    for file, diff_text, symbol_ids in items:
        cost = _estimate_tokens(diff_text) + 100  # rough per-file scaffolding overhead
        if current and current_tokens + cost > max_tokens:
            batches.append(current)
            current, current_tokens = [], 0
        current.append((file, diff_text, symbol_ids))
        current_tokens += cost

    if current:
        batches.append(current)
    return batches


def _run_batch_call(
    batch: list[tuple[str, str, list[int]]], cfg: dict, sandboxed: bool = False
) -> list[_BatchReviewFindingLLM]:
    review_cfg = cfg.get("review", {}).get("agentic", {})
    hops = review_cfg.get("hops", 2)
    max_retries = cfg.get("failure_handling", {}).get("max_retries", 2)

    sections = []
    for file, diff_text, symbol_ids in batch:
        context = _light_context_block(symbol_ids, hops)
        sections.append(f"### FILE: {file}\n{diff_text}\nRelated symbols: {context}")
    files_block = "\n\n".join(sections)

    prompt = _BATCH_REVIEW_PROMPT.format(
        files_block=files_block,
        format_instructions=_BATCH_REVIEW_PARSER.get_format_instructions(),
    )

    model_name = cfg["llm"]["model"]
    fallback_model = cfg["llm"].get("fallback_model", model_name)
    backoff = cfg.get("failure_handling", {}).get("retry_backoff_seconds", 0)
    last_error: Exception | None = None
    failure_type: str | None = None
    for attempt in range(max_retries + 1):
        current_model = fallback_model if failure_type == "bad_output" else model_name
        try:
            llm = _build_llm(cfg, current_model, temperature=0.1, sandboxed=sandboxed)
            ai_msg = llm.invoke(prompt)
            cleaned = _extract_json_block(ai_msg.content)
            parsed: _BatchReviewResponse = _BATCH_REVIEW_PARSER.invoke(cleaned)
            return parsed.findings
        except Exception as exc:
            last_error = exc
            failure_type = classify_failure(str(exc))
            if attempt == max_retries:
                break
            if failure_type in ("rate_limit", "timeout") and backoff:
                time.sleep(backoff)
    if last_error is not None:
        raise last_error
    return []


def run_batched_agentic_review(
    per_file_diffs: dict[str, str], cfg: dict, sandboxed: bool = False
) -> tuple[dict[str, list[ReviewFindingLLM]], dict[str, int | None], set[str]]:
    """Top-level entry point: gate, then batch, then call. Returns
    (findings_by_file, anchor_symbol_by_file, reviewed_files) — the third
    value is which files actually passed the gate and went to the LLM,
    distinct from "went to the LLM and came back clean" (both look like
    an empty findings list otherwise, and callers reporting a skip count
    need to tell those apart).

    `sandboxed=True` routes every call below through the local llama.cpp
    server (gitscribe.validation.config's settings) instead of the cloud
    BYOK provider. Default False: identical behavior to before this flag
    existed.
    """
    review_cfg = cfg.get("review", {}).get("agentic", {})
    max_context_tokens = review_cfg.get("max_context_tokens", 6000)
    min_blast_radius = review_cfg.get("min_blast_radius_for_review", None)

    reserved = _reserved_prompt_overhead(cfg, sandboxed)
    # Floor so a small max_context_tokens doesn't produce a zero/negative
    # budget and silently pack nothing.
    effective_budget = max(max_context_tokens - reserved, 500)

    candidates: list[tuple[str, str, list[int]]] = []
    anchors: dict[str, int | None] = {}
    for file, diff_text in per_file_diffs.items():
        symbol_ids = changed_symbol_ids(diff_text)
        anchors[file] = symbol_ids[0] if symbol_ids else None
        if _should_escalate_to_agentic(symbol_ids, min_blast_radius):
            candidates.append((file, diff_text, symbol_ids))

    reviewed_files = {c[0] for c in candidates}
    results: dict[str, list[ReviewFindingLLM]] = {f: [] for f in per_file_diffs}
    if not candidates:
        return results, anchors, reviewed_files

    batches = _pack_into_batches(candidates, effective_budget)
    for batch in batches:
        if len(batch) == 1 and _estimate_tokens(batch[0][1]) > effective_budget:
            # too big to batch with anything else and too big on its own —
            # fall back to the single-file path, which truncates as a
            # last resort instead of failing the whole batch.
            file, diff_text, symbol_ids = batch[0]
            try:
                results[file] = run_agentic_review(diff_text, symbol_ids, cfg, sandboxed=sandboxed)
            except Exception as exc:
                logger.warning("agentic review failed for %s, skipping: %s", file, exc)
            continue

        # One batch exhausting its retries (e.g. persistent malformed-JSON
        # output) must not discard every other batch's already-accumulated
        # results — this used to propagate straight out of the function,
        # so cli.py's broad `except Exception` around the whole call would
        # report 0 findings even when most batches had already succeeded.
        try:
            findings = _run_batch_call(batch, cfg, sandboxed=sandboxed)
        except Exception as exc:
            failed_files = [c[0] for c in batch]
            logger.warning(
                "agentic review batch failed (%d file(s): %s), skipping: %s",
                len(failed_files), ", ".join(failed_files), exc,
            )
            continue

        for f in findings:
            results.setdefault(f.file, []).append(
                ReviewFindingLLM(
                    severity=f.severity,
                    rule_or_reason=f.rule_or_reason,
                    message=f.message,
                    line_start=f.line_start,
                    line_end=f.line_end,
                )
            )

    return results, anchors, reviewed_files


def write_agentic_findings_by_file(
    results: dict[str, list[ReviewFindingLLM]], anchors: dict[str, int | None]
) -> int:
    """Writes every file's findings, anchored to that file's own changed
    symbol — not one global anchor across the whole (formerly whole-branch)
    diff, which was the pre-batching behavior."""
    total = 0
    for file, findings in results.items():
        if findings:
            total += write_agentic_findings(findings, anchors.get(file))
    return total
