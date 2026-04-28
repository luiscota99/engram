"""
Embeddings module — generate text embeddings via a local Ollama instance.

Configure the model with ENGRAM_EMBED_MODEL (canonical). ENGRAM_EMBEDDING_MODEL
is accepted as a deprecated alias for the same purpose.

Only models whose output dimension matches VEC_EMBEDDING_DIMENSION (768) can be
stored in vec_memory; see database schema. Other advertised models remain
documented for reference but will not populate the vector index until the schema
supports their dimension.

Known 768-dim (default): nomic-embed-text — use for semantic + hybrid search.

See README.md for the embedding models tradeoff table.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

# Must match vec_memory embedding float[N] in database schema (sqlite-vec).
VEC_EMBEDDING_DIMENSION = 768

PRIMARY_EMBEDDING_MODEL_ENV = "ENGRAM_EMBED_MODEL"
LEGACY_EMBEDDING_MODEL_ENV = "ENGRAM_EMBEDDING_MODEL"

# Per-model context window limits (in characters, ~4 chars/token).
# The truncation guard keeps text well within each model's token limit.
_MODEL_CONTEXT: dict[str, int] = {
    "nomic-embed-text": 8000,       # 8192-token window → ~32 768 chars, cap at 8000
    "mxbai-embed-large": 2000,      # 512-token window  → ~2048  chars
    "bge-large": 2000,              # 512-token window
    "bge-large-en-v1.5": 2000,
    "snowflake-arctic-embed": 2000, # 512-token window
}

SUPPORTED_MODELS: dict[str, dict] = {
    "nomic-embed-text": {
        "dimensions": 768,
        "context_tokens": 8192,
        "size_mb": 274,
        "notes": "Default. Best context window. Recommended for most Engram use cases.",
    },
    "mxbai-embed-large": {
        "dimensions": 1024,
        "context_tokens": 512,
        "size_mb": 670,
        "notes": "Highest MTEB score among Ollama models. Short context window.",
    },
    "bge-large-en-v1.5": {
        "dimensions": 1024,
        "context_tokens": 512,
        "size_mb": 670,
        "notes": "Strong English retrieval. Short context window. Also available as bge-large.",
    },
    "snowflake-arctic-embed": {
        "dimensions": 1024,
        "context_tokens": 512,
        "size_mb": 669,
        "notes": "Fast inference. Competitive MTEB. Small context window.",
    },
}

_DEFAULT_MODEL = "nomic-embed-text"
_EMBED_TIMEOUT = 30  # seconds


def resolve_embedding_model_name() -> str:
    """Resolve embedding model from env: ENGRAM_EMBED_MODEL, then legacy ENGRAM_EMBEDDING_MODEL."""
    primary = os.environ.get(PRIMARY_EMBEDDING_MODEL_ENV, "").strip()
    if primary:
        legacy = os.environ.get(LEGACY_EMBEDDING_MODEL_ENV, "").strip()
        if legacy and legacy != primary:
            logger.warning(
                "%s=%r and %s=%r both set; using %s.",
                PRIMARY_EMBEDDING_MODEL_ENV,
                primary,
                LEGACY_EMBEDDING_MODEL_ENV,
                legacy,
                PRIMARY_EMBEDDING_MODEL_ENV,
            )
        return primary

    legacy = os.environ.get(LEGACY_EMBEDDING_MODEL_ENV, "").strip()
    if legacy:
        logger.warning(
            "%s is deprecated; set %s instead (same meaning).",
            LEGACY_EMBEDDING_MODEL_ENV,
            PRIMARY_EMBEDDING_MODEL_ENV,
        )
        return legacy

    return _DEFAULT_MODEL


def expected_dimensions_for_model(model: str) -> int | None:
    """Return known output dimension for model, or None if unknown."""
    meta = SUPPORTED_MODELS.get(model)
    return int(meta["dimensions"]) if meta else None


def embedding_matches_vec_schema(embedding: list[float], model: str) -> tuple[bool, str | None]:
    """Return whether *embedding* can be stored in vec_memory (fixed dimension).

    Unknown models must still produce vectors of length VEC_EMBEDDING_DIMENSION.
    Known models whose dimension differs from the schema cannot be stored.
    """
    exp = expected_dimensions_for_model(model)
    if exp is not None and exp != VEC_EMBEDDING_DIMENSION:
        return False, (
            f"model {model!r} produces {exp}-dim vectors; vec_memory requires "
            f"{VEC_EMBEDDING_DIMENSION}. Use e.g. nomic-embed-text, or migrate the schema."
        )
    if len(embedding) != VEC_EMBEDDING_DIMENSION:
        return False, (
            f"embedding length {len(embedding)} != vec_memory dimension {VEC_EMBEDDING_DIMENSION}"
            + (f" (model {model!r})" if model else "")
        )
    return True, None


def _get_model() -> str:
    """Return the active embedding model name (with legacy-env compatibility).

    Unknown names trigger a warnings.warn for backward compatibility with code
    that relied on implicit discovery.
    """
    model = resolve_embedding_model_name()
    if model not in _MODEL_CONTEXT:
        import warnings

        warnings.warn(
            f"{PRIMARY_EMBEDDING_MODEL_ENV}={model!r} is not in the known-models list. "
            f"Known: {list(_MODEL_CONTEXT)}. Proceeding anyway.",
            stacklevel=3,
        )
    return model


def embed_text(text: str, model: str | None = None) -> list[float] | None:
    """Generate an embedding using the local Ollama instance.

    Parameters
    ----------
    text:
        Text to embed.
    model:
        Override the model for this call.  If omitted, uses ``_get_model()``
        which respects ``ENGRAM_EMBED_MODEL``.

    Returns the embedding vector, or ``None`` if Ollama is unavailable.
    """
    if not text:
        return None

    active_model = model or _get_model()
    max_chars = _MODEL_CONTEXT.get(active_model, 2000)

    if len(text) > max_chars:
        text = text[:max_chars]

    base_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    url = f"{base_url}/api/embeddings"
    data = json.dumps({"model": active_model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as response:
            result = json.loads(response.read().decode())
            return result.get("embedding")
    except Exception:
        logger.exception(
            "Ollama embedding request failed (model=%s, url=%s)",
            active_model,
            url,
        )
        return None


def get_embedding_model() -> str:
    """Return the currently configured embedding model name (for display/logging)."""
    return _get_model()
