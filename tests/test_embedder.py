import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

from gitscribe.core.indexer import embedder
from gitscribe.core.indexer.parser import Symbol


def make_symbol(name="foo", kind="function", parent=None, calls=None, bases=None):
    return Symbol(
        name=name,
        kind=kind,
        file="a.py",
        lineno=1,
        end_lineno=1,
        parent=parent,
        calls=calls or [],
        bases=bases or [],
    )


# --- _symbol_to_text ---


def test_symbol_to_text_includes_kind_and_name():
    text = embedder._symbol_to_text(make_symbol(name="helper", kind="function"))
    assert "function" in text
    assert "helper" in text


def test_symbol_to_text_includes_parent_scope():
    text = embedder._symbol_to_text(make_symbol(name="bar", kind="method", parent="Foo"))
    assert "Foo.bar" in text


def test_symbol_to_text_includes_calls_capped_at_ten():
    calls = [f"call_{i}" for i in range(15)]
    text = embedder._symbol_to_text(make_symbol(calls=calls))
    assert "call_0" in text
    assert "call_9" in text
    assert "call_14" not in text  # capped at 10


def test_symbol_to_text_includes_bases():
    text = embedder._symbol_to_text(make_symbol(kind="class", bases=["Base1", "Base2"]))
    assert "Base1" in text and "Base2" in text


def test_symbol_to_text_omits_empty_sections():
    text = embedder._symbol_to_text(make_symbol(calls=[], bases=[]))
    assert "calls:" not in text
    assert "inherits:" not in text


# --- vector <-> blob round trip ---


def test_vector_blob_round_trip():
    vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    blob = embedder.vector_to_blob(vec)
    restored = embedder.blob_to_vector(blob)
    np.testing.assert_allclose(vec, restored, rtol=1e-6)


def test_vector_to_blob_casts_to_float32():
    vec = np.array([1.0, 2.0], dtype=np.float64)
    blob = embedder.vector_to_blob(vec)
    restored = embedder.blob_to_vector(blob)
    assert restored.dtype == np.float32


# --- cosine_similarity ---


def test_cosine_similarity_identical_vectors():
    v = np.array([1.0, 2.0, 3.0])
    assert embedder.cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors():
    a = np.array([1.0, 0.0])
    b = np.array([0.0, 1.0])
    assert embedder.cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_opposite_vectors():
    a = np.array([1.0, 0.0])
    b = np.array([-1.0, 0.0])
    assert embedder.cosine_similarity(a, b) == pytest.approx(-1.0)


def test_cosine_similarity_zero_vector_does_not_raise():
    a = np.array([0.0, 0.0])
    b = np.array([1.0, 0.0])
    # denom guarded with a small epsilon fallback -- should not raise ZeroDivisionError
    result = embedder.cosine_similarity(a, b)
    assert result == 0.0


# --- provider dispatch ---


def test_embed_symbols_hosted_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        embedder.embed_symbols_hosted([make_symbol()], cfg={})


def test_embed_symbols_unknown_provider_raises_value_error():
    with pytest.raises(ValueError):
        embedder.embed_symbols([make_symbol()], cfg={"embedding": {"provider": "carrier_pigeon"}})


def test_embed_symbols_routes_to_local(monkeypatch):
    called = {}

    def fake_local(symbols, model_name):
        called["symbols"] = symbols
        called["model_name"] = model_name
        return [np.zeros(3)]

    monkeypatch.setattr(embedder, "embed_symbols_local", fake_local)
    result = embedder.embed_symbols(
        [make_symbol()], cfg={"embedding": {"provider": "local", "model": "test-model"}}
    )
    assert called["model_name"] == "test-model"
    assert len(result) == 1


def test_embed_symbols_defaults_to_local_when_no_config():
    """No `embedding:` block in cfg -> should default to local, not crash."""
    monkeypatch_target = "embedder.embed_symbols_local"
    # patch via sys.modules-level attribute to avoid needing monkeypatch fixture here
    original = embedder.embed_symbols_local
    embedder.embed_symbols_local = lambda symbols, model_name: [np.zeros(3)]
    try:
        result = embedder.embed_symbols([make_symbol()], cfg={})
        assert len(result) == 1
    finally:
        embedder.embed_symbols_local = original


# --- _get_local_model caching, with sentence_transformers mocked out ---


def test_get_local_model_caches_instance(monkeypatch):
    fake_st_module = MagicMock()
    fake_instance = MagicMock()
    fake_st_module.SentenceTransformer.return_value = fake_instance
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_module)
    embedder._LOCAL_MODEL_CACHE.clear()

    m1 = embedder._get_local_model("some-model")
    m2 = embedder._get_local_model("some-model")

    assert m1 is m2
    assert fake_st_module.SentenceTransformer.call_count == 1
    embedder._LOCAL_MODEL_CACHE.clear()


def test_get_local_model_separate_cache_per_model_name(monkeypatch):
    fake_st_module = MagicMock()
    fake_st_module.SentenceTransformer.side_effect = lambda name: MagicMock(name=name)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_module)
    embedder._LOCAL_MODEL_CACHE.clear()

    embedder._get_local_model("model-a")
    embedder._get_local_model("model-b")

    assert fake_st_module.SentenceTransformer.call_count == 2
    embedder._LOCAL_MODEL_CACHE.clear()
