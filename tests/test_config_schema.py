import pytest
from pydantic import ValidationError

from gitscribe.core.config_schema import GitScribeConfig

VALID_RAW = {
    "llm": {"model": "llama-3.3-70b-versatile", "fallback_model": "llama-3.1-8b-instant"},
}


def test_valid_config_loads_with_defaults():
    cfg = GitScribeConfig(**VALID_RAW)
    assert cfg.llm.model == "llama-3.3-70b-versatile"
    assert cfg.retrieval.min_prs == 3
    assert cfg.risk_classifier.trivial_threshold == 0.15


def test_missing_required_llm_model_raises():
    with pytest.raises(ValidationError):
        GitScribeConfig(llm={"fallback_model": "llama-3.1-8b-instant"})


def test_temperature_out_of_range_raises():
    with pytest.raises(ValidationError):
        GitScribeConfig(llm={**VALID_RAW["llm"], "temperature": 5.0})


def test_trivial_threshold_out_of_range_raises():
    with pytest.raises(ValidationError):
        GitScribeConfig(**VALID_RAW, risk_classifier={"trivial_threshold": 1.5})


def test_merge_preview_defaults():
    cfg = GitScribeConfig(**VALID_RAW)
    assert cfg.merge_preview.escalate_below_confidence == "high"
    assert cfg.merge_preview.blast_radius_depth == 2
    assert cfg.merge_preview.worktree_cleanup is True


def test_merge_preview_invalid_confidence_level_raises():
    with pytest.raises(ValidationError):
        GitScribeConfig(**VALID_RAW, merge_preview={"escalate_below_confidence": "extreme"})


def test_merge_preview_blast_radius_depth_must_be_positive():
    with pytest.raises(ValidationError):
        GitScribeConfig(**VALID_RAW, merge_preview={"blast_radius_depth": 0})


def test_merge_preview_as_dict_round_trips_through_as_dict():
    """as_dict() (model_dump()) must include merge_preview, since cli.py's
    load_config() callers read cfg["merge_preview"][...] as a plain dict —
    this is exactly the bug that made the pre-existing `hooks:` config
    section silently inert (never declared on GitScribeConfig, so
    model_dump() drops it).
    """
    cfg = GitScribeConfig(**VALID_RAW)
    as_dict = cfg.as_dict()
    assert "merge_preview" in as_dict
    assert as_dict["merge_preview"]["escalate_below_confidence"] == "high"


def test_as_dict_matches_node_access_pattern():
    cfg = GitScribeConfig(**VALID_RAW)
    d = cfg.as_dict()
    assert d["llm"]["model"] == "llama-3.3-70b-versatile"
    assert d["retrieval"]["max_prs"] == 10

def test_sandboxed_timeout_seconds_must_be_positive():
    with pytest.raises(ValidationError):
        from gitscribe.core.config_schema import AgenticReviewConfig

        AgenticReviewConfig(sandboxed_timeout_seconds=0)

    with pytest.raises(ValidationError):
        AgenticReviewConfig(sandboxed_timeout_seconds=-10)

    cfg = AgenticReviewConfig(sandboxed_timeout_seconds=300)
    assert cfg.sandboxed_timeout_seconds == 300