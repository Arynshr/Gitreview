import json

import pytest

from gitscribe.validation.ai import (
    AIReviewError,
    _extract_json,
    _loopback_url,
)


def test_malformed_ai_json_fails() -> None:
    with pytest.raises(AIReviewError):
        _extract_json("not json")


def test_non_loopback_ai_endpoint_is_rejected() -> None:
    with pytest.raises(AIReviewError):
        _loopback_url("http://example.com:11434")


def test_valid_loopback_endpoint_is_allowed() -> None:
    assert (
        _loopback_url(
            "http://127.0.0.1:11434/"
        )
        == "http://127.0.0.1:11434"
    )


def test_ai_json_contract() -> None:
    value = _extract_json(
        "```json\n"
        + json.dumps({"findings": []})
        + "\n```"
    )

    assert value == {"findings": []}
