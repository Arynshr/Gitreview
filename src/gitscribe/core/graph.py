"""
GitScribe's LangGraph StateGraph.

Deterministic nodes: diff_parser, summarizer, generator (happy path), gh_create
Agentic nodes: risk_classifier, retriever, failure_router

Flow:
  diff_parser -> summarizer -> validate
      -> [blocked: validation.block_pr_generation and FAIL] -> template_fallback -> END
      -> [continue] -> risk_classifier
      -> [skip_generation=True]  -> template_fallback -> END
      -> [skip_generation=False] -> retriever -> generator
              -> [success] -> END
              -> [failed]  -> failure_router
                      -> retry_same_model    -> generator (loop, bounded by attempt_count)
                      -> retry_fallback_model -> generator (fallback_used=True)
                      -> template_fallback -> END

`validate` runs `validation/graph.py`'s own DAG via
`core/validation_bridge.py` - a translation boundary, not a merge of the
two graphs' state (see validation_bridge.py's docstring). It is a no-op
pass-through (routes to "continue") unless `validation.block_pr_generation`
is explicitly enabled, so existing behavior is unchanged by default.
"""
import functools
import time

from langgraph.graph import END, StateGraph

from gitscribe.core.diff_parser import diff_parser_node
from gitscribe.core.failure_router import failure_router_node, route_after_failure
from gitscribe.core.generator import generator_node
from gitscribe.core.progress import phase
from gitscribe.core.retriever import retriever_node
from gitscribe.core.risk_classifier import risk_classifier_node_blended, route_after_risk
from gitscribe.core.state import GitScribeState
from gitscribe.core.summarizer import summarizer_node
from gitscribe.core.validation_bridge import route_after_validate, validate_node


def template_fallback_node(state: GitScribeState) -> dict:
    """No-LLM fallback: deterministic template from change_summary alone.

    Reached via three paths that must NOT be conflated:
      - risk_classifier decided the diff is trivial (skip_generation=True) -
        intentional, not a problem.
      - failure_router exhausted retries after real LLM failures - status
        was already "failed" on state; preserving it (not stomping to
        "success") lets callers surface state.last_error/failure_type.
      - `validate` found a blocking validation failure
        (`validation.block_pr_generation` enabled, validation_status ==
        "FAIL") - generation never ran; failure_type is set to
        "validation_blocked" to distinguish this from an LLM failure.
    """
    files = ", ".join(state.files_changed) or "no files detected"
    body = (
        f"## Summary\nAutomated fallback description.\n\n"
        f"## Changes\n{chr(10).join('- ' + s for s in state.change_summary)}\n\n"
        f"## Files\n{files}"
    )

    if state.failure_type == "validation_blocked":
        return {"pr_title": "PR: " + files[:60], "pr_body": body, "status": "failed"}

    status = "skipped" if state.skip_generation else "failed"
    return {"pr_title": "PR: " + files[:60], "pr_body": body, "status": status}


def retry_same_model_node(state: GitScribeState, cfg: dict) -> dict:
    time.sleep(cfg["failure_handling"]["retry_backoff_seconds"])
    return {"fallback_used": False}


def retry_fallback_model_node(state: GitScribeState) -> dict:
    return {"fallback_used": True}


def mark_validation_blocked_node(state: GitScribeState) -> dict:
    return {"failure_type": "validation_blocked", "last_error": state.validation_summary}


def _traced(name: str, fn, emitter):
    """Wrap a node so it reports phase.started/finished. No emitter -> the
    node is returned untouched (zero overhead, identical behaviour)."""
    if emitter is None:
        return fn

    def wrapper(state):
        with phase(emitter, name):
            return fn(state)

    return wrapper


def build_graph(cfg: dict, emitter=None):
    g = StateGraph(GitScribeState)

    def node(name: str, fn) -> None:
        g.add_node(name, _traced(name, fn, emitter))

    node("diff_parser", functools.partial(diff_parser_node, cfg=cfg))
    node("summarizer", summarizer_node)
    node("validate", functools.partial(validate_node, cfg=cfg, emitter=emitter))
    node("mark_validation_blocked", mark_validation_blocked_node)
    node("risk_classifier", functools.partial(risk_classifier_node_blended, cfg=cfg))
    node("retriever", functools.partial(retriever_node, cfg=cfg))
    node("generator", functools.partial(generator_node, cfg=cfg))
    node("failure_router", functools.partial(failure_router_node, cfg=cfg))
    node("template_fallback", template_fallback_node)
    node("retry_same_model", functools.partial(retry_same_model_node, cfg=cfg))
    node("retry_fallback_model", retry_fallback_model_node)

    g.set_entry_point("diff_parser")
    g.add_edge("diff_parser", "summarizer")
    g.add_edge("summarizer", "validate")

    g.add_conditional_edges(
        "validate",
        functools.partial(route_after_validate, cfg=cfg),
        {
            "blocked": "mark_validation_blocked",
            "continue": "risk_classifier",
        },
    )
    g.add_edge("mark_validation_blocked", "template_fallback")

    g.add_conditional_edges("risk_classifier", route_after_risk, {
        "skip": "template_fallback",
        "retrieve": "retriever",
    })

    g.add_edge("retriever", "generator")

    g.add_conditional_edges("generator", lambda s: s.status, {
        "success": END,
        "failed": "failure_router",
    })

    g.add_conditional_edges(
        "failure_router",
        functools.partial(route_after_failure, cfg=cfg),
        {
            "retry_same_model": "retry_same_model",
            "retry_fallback_model": "retry_fallback_model",
            "template_fallback": "template_fallback",
        },
    )

    g.add_edge("retry_same_model", "generator")
    g.add_edge("retry_fallback_model", "generator")
    g.add_edge("template_fallback", END)

    return g.compile()
