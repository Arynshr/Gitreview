"""
Agentic node #2: adaptive retrieval depth.
Fixed 'last N PRs' fails on monorepos (irrelevant noise) and sparse repos
(over-fetch nothing). Agent inspects candidates and decides stop/widen.
"""

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate

from gitscribe.core import memory
from gitscribe.core.llm_client import build_chat_model
from gitscribe.core.state import GitScribeState

RELEVANCE_PROMPT = ChatPromptTemplate.from_template(
    """You are deciding whether retrieved past PRs are useful style/context
examples for a new PR on branch '{branch}' with changes: {change_summary}

Candidate PRs so far: {candidates}

Respond ONLY with JSON:
{{"action": "stop" | "widen", "reason": "<one sentence>"}}
"stop" = candidates are sufficient/relevant.
"widen" = candidates are irrelevant or too few; broaden the search."""
)


def _branch_prefix(branch: str) -> str:
    return branch.split("/")[0] + "/" if "/" in branch else branch


def retriever_node(state: GitScribeState, cfg: dict) -> dict:
    """LangGraph node with a small bounded loop (max_iterations=2)."""
    min_n = cfg["retrieval"]["min_prs"]
    max_n = cfg["retrieval"]["max_prs"]
    branch = state.branch_name

    candidates = memory.fetch_prs_by_branch_prefix(_branch_prefix(branch), min_n)
    depth_used = min_n
    stopped_reason = "min_prs_default"

    # Adaptive widening only makes sense if there's actually room to widen.
    # (Previously gated on cfg["risk_classifier"]["enabled"] - an unrelated
    # feature's flag; disabling risk classification silently also disabled
    # retrieval's own LLM-driven widening, which was never the intent.)
    if max_n > min_n:
        llm = build_chat_model(cfg, cfg["llm"]["model"], temperature=0)
        chain = RELEVANCE_PROMPT | llm | JsonOutputParser()

        max_iterations = 2
        for i in range(max_iterations):
            try:
                decision = chain.invoke(
                    {
                        "branch": branch,
                        "change_summary": state.change_summary,
                        "candidates": candidates,
                    }
                )
            except Exception:
                stopped_reason = "classifier_error_fail_open"
                break

            if decision.get("action") == "stop" or depth_used >= max_n:
                stopped_reason = decision.get("reason", "stopped")
                break

            depth_used = min(depth_used + min_n, max_n)
            candidates = memory.fetch_recent_prs(depth_used)
            stopped_reason = "widened_" + str(i)

    return {
        "retrieved_prs": candidates,
        "retrieval_depth_used": depth_used,
        "retrieval_stopped_reason": stopped_reason,
    }
