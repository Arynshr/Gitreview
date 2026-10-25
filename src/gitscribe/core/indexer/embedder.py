"""
core/indexer/embedder.py
Stage 2, Step 4: per-symbol embeddings.

Default: local (sentence-transformers) — an indexer that silently costs
API calls on every run is a bad default for a BYOK tool. Hosted embeddings
are opt-in via config.yaml's `embedding:` block, mirroring llm_client.py's
provider pattern.

`_symbol_to_text()` embeds docstring + a short code snippet alongside the
existing kind/name/file/calls/bases metadata, so semantic search has real
natural-language/code content to match queries against, not just identifiers.
"""

from __future__ import annotations

import numpy as np

from gitscribe.core.indexer.parser import Symbol

_LOCAL_MODEL_CACHE: dict[str, object] = {}


def _get_local_model(model_name: str):
    if model_name not in _LOCAL_MODEL_CACHE:
        from sentence_transformers import SentenceTransformer

        _LOCAL_MODEL_CACHE[model_name] = SentenceTransformer(model_name)
    return _LOCAL_MODEL_CACHE[model_name]


def _symbol_to_text(sym: Symbol) -> str:
    scope = f"{sym.parent}." if sym.parent else ""
    parts = [f"{sym.kind} {scope}{sym.name}", f"file: {sym.file}"]
    if sym.docstring:
        parts.append(f"docstring: {sym.docstring}")
    if sym.snippet:
        parts.append(f"code: {sym.snippet}")
    if sym.calls:
        parts.append("calls: " + ", ".join(sym.calls[:10]))
    if sym.bases:
        parts.append("inherits: " + ", ".join(sym.bases))
    return " | ".join(parts)


def embed_symbols_local(
    symbols: list[Symbol], model_name: str = "all-MiniLM-L6-v2"
) -> list[np.ndarray]:
    model = _get_local_model(model_name)
    texts = [_symbol_to_text(s) for s in symbols]
    return list(model.encode(texts, convert_to_numpy=True))


def embed_symbols_hosted(symbols: list[Symbol], cfg: dict) -> list[np.ndarray]:
    """Opt-in path. Same provider-agnostic pattern as llm_client.py — routes
    through the configured hosted embedding API, at the user's own cost.
    Left as an explicit extension point; not wired to a specific provider
    here since that's a BYOK config choice, not a code default.
    """
    raise NotImplementedError(
        "Hosted embeddings are opt-in. Configure embedding.provider in "
        "config.yaml and implement the matching client, same as llm_client.py."
    )


def embed_symbols(symbols: list[Symbol], cfg: dict) -> list[np.ndarray]:
    provider = cfg.get("embedding", {}).get("provider", "local")
    model_name = cfg.get("embedding", {}).get("model", "all-MiniLM-L6-v2")

    if provider == "local":
        return embed_symbols_local(symbols, model_name)
    if provider == "hosted":
        return embed_symbols_hosted(symbols, cfg)
    raise ValueError(f"Unknown embedding.provider: {provider!r}")


def vector_to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1e-10
    return float(np.dot(a, b) / denom)
