"""
Deterministic node: prompt construction + LLM call + structured parsing.
This is the README's core 'chain' — no agent behavior, single call.
"""

import json

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from gitscribe.core.llm_client import build_chat_model
from gitscribe.core.state import GitScribeState
from gitscribe.core.telemetry import timed_llm_call


class PRDescription(BaseModel):
    title: str = Field(description="Concise PR title")
    summary: str = Field(description="1-2 sentence summary")
    changes: str = Field(description="Bullet list of key changes")
    testing: str = Field(description="How this was/should be tested")
    impact: str = Field(description="Risk/impact notes")


PARSER = PydanticOutputParser(pydantic_object=PRDescription)

STYLE_INSTRUCTIONS = {
    "default": "Write in a clear, professional tone with complete sentences.",
    "concise": "Be terse. Use short bullet fragments, not full sentences. Omit any section with nothing notable to say.",
    "detailed": "Be thorough. Explain reasoning behind changes and call out edge cases considered.",
}

GEN_PROMPT = ChatPromptTemplate.from_template(
    """You are a senior software engineer writing a Pull Request description.

Style: {style_instruction}

Diff Summary:
{change_summary}

Commit Messages:
{commit_messages}

Past Style Examples (for tone/format only, do not copy content):
{past_prs}

{format_instructions}

Respond with ONLY the JSON object. No preamble, no explanation, no markdown \
fence around it — just the raw JSON, starting with {{ and ending with }}."""
)


def _find_balanced_end(text: str, start: int) -> int | None:
    """Index of the closing '}' matching the '{' at `start`, scanning
    brace depth string-aware (a brace inside a quoted JSON string value
    doesn't affect depth). None if never balanced."""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _extract_json_block(text: str) -> str:
    first_candidate: str | None = None
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        end = _find_balanced_end(text, start)
        if end is None:
            break  # unbalanced from here on -- nothing further to try
        candidate = text[start : end + 1]
        if first_candidate is None:
            first_candidate = candidate
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            pos = end + 1  # not valid JSON on its own -- try the next candidate
    # nothing parsed cleanly; return the first balanced group (if any) so
    # the caller's parser still raises a specific, readable error instead
    # of choking on unrelated raw text
    return first_candidate if first_candidate is not None else text


def generator_node(state: GitScribeState, cfg: dict) -> dict:
    """LangGraph node: returns partial update with pr_title, pr_body, status, etc."""
    model_name = cfg["llm"]["model"] if not state.fallback_used else cfg["llm"]["fallback_model"]
    style_instruction = STYLE_INSTRUCTIONS.get(state.style, STYLE_INSTRUCTIONS["default"])

    llm = build_chat_model(cfg, model_name, temperature=cfg["llm"]["temperature"])
    prompt_value = GEN_PROMPT.invoke(
        {
            "style_instruction": style_instruction,
            "change_summary": state.change_summary,
            "commit_messages": state.commit_messages,
            "past_prs": [p.title for p in state.retrieved_prs],
            "format_instructions": PARSER.get_format_instructions(),
        }
    )

    # Parsing is inside the timed/logged block now, not after it — a call that
    # returns text but fails to parse into PRDescription is a failed generation
    try:
        with timed_llm_call("generator", model_name) as ctx:
            ai_msg = llm.invoke(prompt_value)
            ctx["usage"] = getattr(ai_msg, "usage_metadata", None)
            cleaned = _extract_json_block(ai_msg.content)
            result: PRDescription = PARSER.invoke(cleaned)
    except Exception as e:
        return {
            "last_error": str(e),
            "attempt_count": state.attempt_count + 1,
            "status": "failed",
        }

    body = (
        f"## Summary\n{result.summary}\n\n"
        f"## Changes\n{result.changes}\n\n"
        f"## Testing\n{result.testing}\n\n"
        f"## Impact\n{result.impact}"
    )
    return {
        "pr_title": result.title,
        "pr_body": body,
        "status": "success",
        "last_error": None,
    }
