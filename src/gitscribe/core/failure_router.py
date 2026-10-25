"""
Agentic node #4: failure-recovery routing.
"""

from gitscribe.core.state import GitScribeState


def classify_failure(error_msg: str) -> str:
    msg = error_msg.lower()
    if "rate limit" in msg or "429" in msg:
        return "rate_limit"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    return "bad_output"


def failure_router_node(state: GitScribeState, cfg: dict) -> dict:
    """LangGraph node: returns partial update with failure_type."""
    error = state.last_error or ""
    return {"failure_type": classify_failure(error)}


def route_after_failure(state: GitScribeState, cfg: dict) -> str:
    """Conditional edge: decide next node based on failure type + attempt count."""
    max_retries = cfg["failure_handling"]["max_retries"]
    attempts = state.attempt_count

    if attempts >= max_retries:
        return "template_fallback"

    failure_type = state.failure_type
    if failure_type == "rate_limit":
        return "retry_same_model"  # backoff + retry
    if failure_type == "timeout":
        return "retry_same_model"
    if failure_type == "bad_output":
        return "retry_fallback_model"  # switch to smaller/more reliable model
    return "template_fallback"
