"""Local AI review pass (`validation/ai.py`): endpoint safety, redaction,
read-only tool confinement, output validation, and failure translation.
No model or network is used - `_build_agent` is replaced with a fake.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from gitscribe.validation import ai
from gitscribe.validation.ai import (
    AIFindingOut,
    AIReviewError,
    ReadOnlyReviewTools,
    _loopback_url,
    _redact,
    _to_finding,
    review_locally_async,
)
from gitscribe.validation.models import ChangeContext


def _finding_out(**overrides) -> AIFindingOut:
    data = {
        "category": "security",
        "severity": "high",
        "confidence": "high",
        "file": "a.py",
        "line": 3,
        "description": "d",
        "evidence": "e",
        "recommendation": "r",
    }
    data.update(overrides)
    return AIFindingOut(**data)


# --- endpoint safety ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://example.com:11434", "https://127.0.0.1:8080", "http://10.0.0.5:8080", "ftp://localhost"],
)
def test_non_loopback_or_non_http_endpoint_is_rejected(url) -> None:
    with pytest.raises(AIReviewError):
        _loopback_url(url)


def test_valid_loopback_endpoint_is_allowed_and_normalized() -> None:
    assert _loopback_url("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
    assert _loopback_url("http://localhost:8080") == "http://localhost:8080"


# --- redaction ---------------------------------------------------------------


def test_redact_masks_labeled_secret_values() -> None:
    assert _redact("password = hunter2hunter2") == "password = [REDACTED]"


def test_redact_leaves_short_values_and_plain_text() -> None:
    assert _redact("token=abc") == "token=abc"
    assert _redact("nothing sensitive here") == "nothing sensitive here"


# --- structured output validation -------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"description": "   "},
        {"evidence": ""},
        {"recommendation": " "},
        {"line": 0},
        {"severity": "extreme"},
        {"confidence": "certain"},
    ],
)
def test_ai_finding_schema_rejects_bad_output(overrides) -> None:
    with pytest.raises(ValidationError):
        _finding_out(**overrides)


def test_to_finding_maps_and_redacts() -> None:
    item = _finding_out(description="password = hunter2hunter2 is hardcoded")

    finding = _to_finding(item, 0, {"a.py"}, "model-x")

    assert finding.id == "ai:0:a.py:3"
    assert finding.source == "ai"
    assert finding.sources == ["model-x"]
    assert "hunter2hunter2" not in finding.message


def test_to_finding_rejects_unreviewed_file() -> None:
    with pytest.raises(AIReviewError, match="unreviewed file"):
        _to_finding(_finding_out(file="other.py"), 0, {"a.py"}, "m")


def test_to_finding_allows_file_less_finding() -> None:
    assert _to_finding(_finding_out(file=None, line=None), 0, {"a.py"}, "m").file is None


# --- read-only tool confinement ---------------------------------------------


@pytest.fixture
def tools(tmp_path):
    def _make(files: dict[str, str | bytes], max_file_chars: int = 50):
        paths = []
        for name, content in files.items():
            path = tmp_path / name
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
            paths.append(str(path))
        ctx = ChangeContext(base="b", head="h", diff="the diff", files=paths)
        return ReadOnlyReviewTools(context=ctx, max_file_chars=max_file_chars), paths

    return _make


def test_read_file_is_confined_to_changed_files(tools, tmp_path) -> None:
    t, _ = tools({"a.py": "x = 1\n"})
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")

    with pytest.raises(AIReviewError, match="non-changed"):
        t.read_file(str(outside))


def test_read_file_truncates_and_handles_binary(tools) -> None:
    t, (text_path, bin_path) = tools({"a.py": "x" * 500, "b.bin": b"\xff\xfe\x00"}, max_file_chars=10)

    assert t.read_file(text_path) == "x" * 10
    assert "unavailable or binary" in t.read_file(bin_path)


def test_search_code_caps_results_and_ignores_blank_query(tools) -> None:
    t, (path,) = tools({"a.py": "needle\n" * 80})

    assert len(t.search_code("needle")) == 50
    assert t.search_code("needle")[0] == f"{path}:1: needle"
    assert t.search_code("   ") == []


def test_inspect_symbol_finds_function_and_class(tools) -> None:
    t, (path,) = tools({"a.py": "def foo():\n    return 1\n\nclass Bar:\n    pass\n"})

    assert "def foo" in t.inspect_symbol(path, "foo")
    assert "class Bar" in t.inspect_symbol(path, "Bar")
    assert t.inspect_symbol(path, "missing") == ""


def test_inspect_symbol_tolerates_syntax_errors_and_denies_other_files(tools) -> None:
    t, (path,) = tools({"a.py": "def (:\n"})

    assert t.inspect_symbol(path, "foo") == ""
    with pytest.raises(AIReviewError, match="non-changed"):
        t.inspect_symbol("/etc/hostname", "foo")


# --- review_locally_async: failure translation & fault isolation ------------


class _FakeAgent:
    def __init__(self, behavior):
        self._behavior = behavior

    async def run(self, prompt, deps=None, usage_limits=None):
        return await self._behavior(prompt)


@pytest.fixture
def fake_agent(monkeypatch):
    built: list[int] = []

    def install(behavior):
        monkeypatch.setattr(ai, "_build_agent", lambda *a, **k: (built.append(1), _FakeAgent(behavior))[1])
        monkeypatch.setattr(ai, "_register_tools", lambda agent: None)
        return built

    return install


def _ctx(diff: str = "+x = 1\n") -> ChangeContext:
    return ChangeContext(base="b", head="h", diff=diff, files=["a.py"])


def _review(ctx=None, cfg=None):
    return asyncio.run(review_locally_async(ctx or _ctx(), cfg or {}, []))


def test_review_returns_normalized_findings(fake_agent) -> None:
    async def behavior(prompt):
        return SimpleNamespace(output=[_finding_out()])

    fake_agent(behavior)

    findings = _review()

    assert [f.source for f in findings] == ["ai"]
    assert findings[0].file == "a.py"


def test_non_loopback_endpoint_fails_before_agent_is_built(fake_agent) -> None:
    async def behavior(prompt):  # pragma: no cover - must never run
        raise AssertionError("agent ran")

    built = fake_agent(behavior)

    with pytest.raises(AIReviewError):
        _review(cfg={"base_url": "http://example.com:8080"})

    assert built == []


def test_secrets_are_redacted_from_the_prompt(fake_agent) -> None:
    seen: list[str] = []

    async def behavior(prompt):
        seen.append(prompt)
        return SimpleNamespace(output=[])

    fake_agent(behavior)

    _review(ctx=_ctx(diff="+password = hunter2hunter2\n"))

    assert "hunter2hunter2" not in seen[0]
    assert "[REDACTED]" in seen[0]


def test_provider_failure_becomes_ai_review_error(fake_agent) -> None:
    async def behavior(prompt):
        raise ConnectionError("llama-server down")

    fake_agent(behavior)

    with pytest.raises(AIReviewError, match="review failed"):
        _review()


def test_total_budget_timeout_becomes_ai_review_error(fake_agent) -> None:
    async def behavior(prompt):
        await asyncio.sleep(1)

    fake_agent(behavior)

    with pytest.raises(AIReviewError, match="total budget"):
        _review(cfg={"total_timeout_seconds": 0.05})


def test_finding_for_unreviewed_file_fails_the_pass(fake_agent) -> None:
    async def behavior(prompt):
        return SimpleNamespace(output=[_finding_out(file="invented.py")])

    fake_agent(behavior)

    with pytest.raises(AIReviewError, match="unreviewed file"):
        _review()
