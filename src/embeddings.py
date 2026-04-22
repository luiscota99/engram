"""
Embeddings module — generate text embeddings via a local Ollama instance.

Supported models (via ENGRAM_EMBED_MODEL env var):
  nomic-embed-text     768-dim, 8192-token context  (default — best balance)
  mxbai-embed-large    1024-dim, 512-token context  (higher MTEB, smaller window)
  bge-large            1024-dim, 512-token context  (strong EN retrieval)
  snowflake-arctic-embed  1024-dim, 512-token context  (fast, good quality)

See README.md §Embedding Models for full tradeoff table.
"""
from __future__ import annotations

import json
import os
import urllib.request

# Per-model context window limits (in characters, ~4 chars/token).
# The truncation guard keeps text well within each model's token limit.
_MODEL_CONTEXT: dict[str, int] = {
    "nomic-embed-text": 8000,       # 8192-token window → ~32 768 chars, cap at 8000
    "mxbai-embed-large": 2000,      # 512-token window  → ~2048  chars
    "bge-large": 2000,              # 512-token window
    "bge-large-en-v1.5": 2000,
    "snowflake-arctic-embed": 2000, # 512-token window
}

_DEFAULT_MODEL = "nomic-embed-text"
_EMBED_TIMEOUT = 30  # seconds


def _get_model() -> str:
    """Return the active embedding model name.

    Priority:
    1. ``ENGRAM_EMBED_MODEL`` environment variable
    2. Hard-coded default (``nomic-embed-text``)

    The value is validated against the known-models list.  An unrecognised
    model name is accepted with a warning (allows future Ollama models).
    """
    model = os.environ.get("ENGRAM_EMBED_MODEL", _DEFAULT_MODEL).strip()
    if model not in _MODEL_CONTEXT:
        # Unknown model — log once and continue; Ollama may support it.
        import warnings
        warnings.warn(
            f"ENGRAM_EMBED_MODEL='{model}' is not in the known-models list. "
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
        return None


def get_embedding_model() -> str:
    """Return the currently configured embedding model name (for display/logging)."""
    return _get_model()


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
        "notes": "Fast inference. Competitive MTEB. Short context window.",
    },
}
