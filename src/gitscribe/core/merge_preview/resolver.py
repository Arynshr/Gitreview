"""
core/merge_preview/resolver.py
Agentic node: proposes a resolution per conflict hunk, grounded in
HunkContext (blast radius + recovered branch intent from PR memory).
"""

from __future__ import annotations

import logging

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from gitscribe.core.generator import _extract_json_block
from gitscribe.core.llm_client import build_chat_model
from gitscribe.core.merge_preview.models import Confidence, FileReport, HunkContext, HunkResolution
from gitscribe.core.telemetry import timed_llm_call

logger = logging.getLogger("gitscribe.merge_preview")

_CONFIDENCE_ORDER: dict[Confidence, int] = {"low": 0, "medium": 1, "high": 2}


class _SingleResolutionLLM(BaseModel):
    resolved_text: str = Field(description="Proposed merged content, no conflict markers")
    rationale: str = Field(
        description="Why this preserves both branches' intent, or why it doesn't fully"
    )
    confidence: Confidence = Field(
        description="'high' only if you are confident this is correct and safe"
    )


class _BatchResolutionLLM(_SingleResolutionLLM):
    hunk_index: int


class _BatchResponse(BaseModel):
    resolutions: list[_BatchResolutionLLM]


_SINGLE_PARSER = PydanticOutputParser(pydantic_object=_SingleResolutionLLM)
_BATCH_PARSER = PydanticOutputParser(pydantic_object=_BatchResponse)

_SHARED_INSTRUCTIONS = (
    "You are resolving a git merge conflict. For each hunk, propose the merged "
    "content that best preserves the INTENT of both branches, not just their text. "
    "Use the blast-radius and branch-intent context to judge whether the two sides "
    "are doing unrelated things (usually safely combinable) or the same thing two "
    "different ways (usually NOT safely combinable — pick one and say why in the "
    "rationale, or mark confidence low if you can't tell). "
    "Only mark confidence 'high' when you are genuinely confident the resolution "
    "is correct and safe to apply without a human reading it first. "
    "Never invent behavior that isn't present in ours_text, theirs_text, or base_text."
)

_BATCH_PROMPT = ChatPromptTemplate.from_template(
    _SHARED_INSTRUCTIONS
    + """

File: {file}

{hunks_block}

{format_instructions}

Respond with ONLY the JSON object — one entry per hunk_index listed above, \
no preamble, no markdown fence, starting with {{ and ending with }}."""
)

_SINGLE_PROMPT = ChatPromptTemplate.from_template(
    _SHARED_INSTRUCTIONS
    + """

This conflict was flagged as uncertain in an earlier pass — give it your full, \
isolated attention.

{hunk_block}

{format_instructions}

Respond with ONLY the JSON object. No preamble, no markdown fence — just the \
raw JSON, starting with {{ and ending with }}."""
)


def _hunk_block(ctx: HunkContext) -> str:
    hunk = ctx.hunk
    lines = [
        f"hunk_index: {hunk.hunk_index}",
        ctx.as_prompt_block(),
        f"--- ours ({hunk.ours_label}) ---\n{hunk.ours_text}",
        f"--- theirs ({hunk.theirs_label}) ---\n{hunk.theirs_text}",
    ]
    if hunk.base_text is not None:
        lines.append(f"--- base (common ancestor) ---\n{hunk.base_text}")
    return "\n".join(lines)


def _fallback_resolution(hunk_index: int, error: Exception) -> HunkResolution:
    """Fail-open, same philosophy as risk_classifier/failure_router: a
    broken LLM call must never crash the whole preview or silently claim
    high confidence.
    """
    return HunkResolution(
        hunk_index=hunk_index,
        resolved_text="",
        rationale=f"Automatic resolution failed ({type(error).__name__}: {error}). Resolve manually.",
        confidence="low",
        escalated=False,
    )


def _resolve_single(ctx: HunkContext, cfg: dict, model_name: str) -> HunkResolution:
    llm = build_chat_model(cfg, model_name, temperature=0.1)
    prompt_value = _SINGLE_PROMPT.invoke(
        {
            "hunk_block": _hunk_block(ctx),
            "format_instructions": _SINGLE_PARSER.get_format_instructions(),
        }
    )
    try:
        with timed_llm_call("merge_preview.hunk_resolve", model_name) as call_ctx:
            ai_msg = llm.invoke(prompt_value)
            call_ctx["usage"] = getattr(ai_msg, "usage_metadata", None)
            parsed: _SingleResolutionLLM = _SINGLE_PARSER.invoke(
                _extract_json_block(ai_msg.content)
            )
    except Exception as e:
        logger.warning("per-hunk resolution failed for hunk %d: %s", ctx.hunk.hunk_index, e)
        return _fallback_resolution(ctx.hunk.hunk_index, e)

    return HunkResolution(
        hunk_index=ctx.hunk.hunk_index,
        resolved_text=parsed.resolved_text,
        rationale=parsed.rationale,
        confidence=parsed.confidence,
        escalated=True,
    )


def _batch_resolve(
    contexts: list[HunkContext], cfg: dict, model_name: str
) -> dict[int, HunkResolution]:
    if not contexts:
        return {}

    file = contexts[0].hunk.file
    hunks_block = "\n\n".join(_hunk_block(c) for c in contexts)

    llm = build_chat_model(cfg, model_name, temperature=0.1)
    prompt_value = _BATCH_PROMPT.invoke(
        {
            "file": file,
            "hunks_block": hunks_block,
            "format_instructions": _BATCH_PARSER.get_format_instructions(),
        }
    )

    try:
        with timed_llm_call("merge_preview.batch_resolve", model_name) as call_ctx:
            ai_msg = llm.invoke(prompt_value)
            call_ctx["usage"] = getattr(ai_msg, "usage_metadata", None)
            parsed: _BatchResponse = _BATCH_PARSER.invoke(_extract_json_block(ai_msg.content))
    except Exception as e:
        logger.warning(
            "batch resolution failed for file %s: %s — every hunk will be escalated", file, e
        )
        return {c.hunk.hunk_index: _fallback_resolution(c.hunk.hunk_index, e) for c in contexts}

    by_index = {
        r.hunk_index: HunkResolution(
            hunk_index=r.hunk_index,
            resolved_text=r.resolved_text,
            rationale=r.rationale,
            confidence=r.confidence,
            escalated=False,
        )
        for r in parsed.resolutions
    }

    for c in contexts:
        if c.hunk.hunk_index not in by_index:
            logger.info(
                "hunk %d missing from batch response for %s, will escalate", c.hunk.hunk_index, file
            )
            by_index[c.hunk.hunk_index] = _fallback_resolution(
                c.hunk.hunk_index, ValueError("hunk omitted from batch LLM response")
            )
    return by_index


def resolve_file(contexts: list[HunkContext], cfg: dict) -> FileReport:
    """Adaptive entry point: batch pass, then escalate anything below the
    configured confidence floor. `contexts` must all belong to the same
    file (caller groups by file before calling this).
    """
    if not contexts:
        return FileReport(file="", resolutions=[])

    file = contexts[0].hunk.file
    model_name = cfg["llm"]["model"]
    escalate_below: Confidence = cfg.get("merge_preview", {}).get(
        "escalate_below_confidence", "high"
    )
    threshold = _CONFIDENCE_ORDER[escalate_below]

    batch_results = _batch_resolve(contexts, cfg, model_name)
    resolutions: list[HunkResolution] = []
    escalation_calls = 0

    contexts_by_index = {c.hunk.hunk_index: c for c in contexts}
    for hunk_index, result in sorted(batch_results.items()):
        if _CONFIDENCE_ORDER[result.confidence] < threshold:
            logger.info(
                "hunk %d in %s below confidence floor (%s < %s), escalating",
                hunk_index,
                file,
                result.confidence,
                escalate_below,
            )
            result = _resolve_single(contexts_by_index[hunk_index], cfg, model_name)
            escalation_calls += 1
        resolutions.append(result)

    return FileReport(
        file=file,
        resolutions=resolutions,
        batch_call_count=1,
        escalation_call_count=escalation_calls,
    )
