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
import math
import os
import time
from collections import OrderedDict

from . import config

logger = logging.getLogger(__name__)

# Must match vec_memory embedding float[N] in database schema (sqlite-vec).
VEC_EMBEDDING_DIMENSION = 768

PRIMARY_EMBEDDING_MODEL_ENV = "ENGRAM_EMBED_MODEL"
LEGACY_EMBEDDING_MODEL_ENV = "ENGRAM_EMBEDDING_MODEL"

# Embedding backend selection:
#   ENGRAM_EMBED_URL unset      → Ollama at OLLAMA_HOST (default)
#   ENGRAM_EMBED_URL=disabled   → embeddings off (lexical-only search)
#   ENGRAM_EMBED_URL=<url>      → OpenAI-compatible /v1/embeddings endpoint
#                                 (auth via ENGRAM_EMBED_API_KEY if set)
EMBED_URL_ENV = "ENGRAM_EMBED_URL"
EMBED_API_KEY_ENV = "ENGRAM_EMBED_API_KEY"

# Per-model context window limits (in characters, ~4 chars/token).
# The truncation guard keeps text well within each model's token limit.
_MODEL_CONTEXT: dict[str, int] = {
    "nomic-embed-text": 8000,       # 8192-token window → ~32 768 chars, cap at 8000
    "mxbai-embed-large": 2000,      # 512-token window  → ~2048  chars
    "bge-large": 2000,              # 512-token window
    "bge-large-en-v1.5": 2000,
    "snowflake-arctic-embed": 2000, # 512-token window
    "embeddinggemma": 6000,         # 2048-token window on Ollama → cap at 6000 chars
    "snowflake-arctic-embed2": 8000,  # 8192-token window
    "qwen3-embedding": 8000,        # 32K window; cap for latency
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
    "embeddinggemma": {
        "dimensions": 768,
        "context_tokens": 2048,
        "size_mb": 622,
        "notes": "Google, 300M params (2025). Same 768-dim as nomic (no rebuild to "
                 "switch) BUT measured 5x SLOWER than nomic via Ollama on Apple "
                 "Silicon (2026-07) — benchmark on your hardware before switching.",
    },
    "snowflake-arctic-embed2": {
        "dimensions": 1024,
        "context_tokens": 8192,
        "size_mb": 1200,
        "notes": "Multilingual without English regression; quantization-friendly MRL.",
    },
    "qwen3-embedding": {
        "dimensions": 1024,
        "context_tokens": 32768,
        "size_mb": 639,
        "notes": "0.6B variant. Top multilingual MTEB family (2025); flexible output dims.",
    },
}

_DEFAULT_MODEL = "nomic-embed-text"
_EMBED_TIMEOUT = 30  # seconds

# In-process LRU cache of (model, text) -> embedding. The HTTP round-trip to
# Ollama dominates search latency (~85% in profiling), so repeated queries —
# common in a long-lived MCP server session — must not re-embed.
EMBED_CACHE_MAX = 256
_embed_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

# L2: persistent cross-process cache. The CLI is a fresh process per
# invocation, so the LRU above is always cold there — every repeated query
# paid the full ~100ms Ollama round-trip. This small standalone sqlite file
# (NOT the main memory.db — no schema/migration coupling) makes repeats a
# sub-ms indexed SELECT. Set ENGRAM_EMBED_CACHE=off to disable.
PERSISTENT_CACHE_MAX_ROWS = 5000


def _persistent_cache_path() -> str | None:
    mode = os.environ.get("ENGRAM_EMBED_CACHE", "").strip().lower()
    if mode in ("off", "0", "disabled"):
        return None
    db_dir = os.path.dirname(os.environ.get("ENGRAM_DB_PATH", "") or os.path.join(os.path.expanduser("~"), ".engram", "memory.db"))
    return os.path.join(db_dir, "embed_cache.db")


def _cache_conn():
    import sqlite3

    path = _persistent_cache_path()
    if not path:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=2)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embed_cache ("
        "key TEXT PRIMARY KEY, vec TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    return conn


def _cache_key_hash(model: str, text: str) -> str:
    import hashlib

    return hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()


def _persistent_cache_get(model: str, text: str) -> list[float] | None:
    try:
        conn = _cache_conn()
        if conn is None:
            return None
        row = conn.execute(
            "SELECT vec FROM embed_cache WHERE key = ?", (_cache_key_hash(model, text),)
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else None
    except Exception:
        logger.debug("persistent embed cache read failed", exc_info=True)
        return None


def _persistent_cache_put(model: str, text: str, vec: list[float]) -> None:
    try:
        conn = _cache_conn()
        if conn is None:
            return
        conn.execute(
            "INSERT OR REPLACE INTO embed_cache (key, vec, created_at) VALUES (?, ?, ?)",
            (_cache_key_hash(model, text), json.dumps(vec), time.time()),
        )
        n = conn.execute("SELECT COUNT(*) FROM embed_cache").fetchone()[0]
        if n > PERSISTENT_CACHE_MAX_ROWS:
            conn.execute(
                "DELETE FROM embed_cache WHERE key IN ("
                "SELECT key FROM embed_cache ORDER BY created_at ASC LIMIT ?)",
                (n - PERSISTENT_CACHE_MAX_ROWS,),
            )
        conn.commit()
        conn.close()
    except Exception:
        logger.debug("persistent embed cache write failed", exc_info=True)

# Dead-host cooldown: avoid hammering a down Ollama instance after repeated failures.
DEAD_HOST_COOLDOWN = 20.0
_HOST_FAIL_THRESHOLD = 2
_dead_hosts: dict[str, float] = {}
_host_fails: dict[str, int] = {}
_last_embed_failure_reason: str | None = None


def l2_normalize(vec: list[float]) -> list[float]:
    """Scale *vec* to unit length.

    Ollama's legacy ``/api/embeddings`` returns unnormalized vectors while the
    newer ``/api/embed`` returns unit vectors. Mixing them under euclidean KNN
    silently partitions the index (unnormalized queries only ever match
    unnormalized docs). Normalizing everything in code makes L2 ordering
    equivalent to cosine regardless of which endpoint produced the vector.
    """
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0:
        return vec
    return [x / norm for x in vec]


def _host_key(base_url: str) -> str:
    return base_url.rstrip("/")


def _is_host_in_cooldown(base_url: str) -> bool:
    key = _host_key(base_url)
    until = _dead_hosts.get(key)
    if until is None:
        return False
    if time.time() >= until:
        _dead_hosts.pop(key, None)
        return False
    return True


def _mark_host_failure(base_url: str) -> None:
    global _last_embed_failure_reason
    key = _host_key(base_url)
    n = _host_fails.get(key, 0) + 1
    _host_fails[key] = n
    if n >= _HOST_FAIL_THRESHOLD:
        _dead_hosts[key] = time.time() + DEAD_HOST_COOLDOWN
        _last_embed_failure_reason = f"ollama_host_cooldown ({DEAD_HOST_COOLDOWN}s)"
        logger.warning("Embedding host %s marked dead for %.0fs after %d failures", key, DEAD_HOST_COOLDOWN, n)


def _mark_host_success(base_url: str) -> None:
    key = _host_key(base_url)
    _host_fails.pop(key, None)
    _dead_hosts.pop(key, None)


def resolve_embed_backend() -> tuple[str, str]:
    """Return ``(kind, base_url)`` where kind is ``ollama``, ``openai``, or ``disabled``.

    Any non-empty ``ENGRAM_EMBED_URL`` other than the literal ``disabled`` selects
    the OpenAI-compatible backend at that URL.
    """
    url = os.environ.get(EMBED_URL_ENV, "").strip()
    if url.lower() == "disabled":
        return "disabled", ""
    if url:
        return "openai", url.rstrip("/")
    return "ollama", config.ollama_host()


def _openai_embeddings_endpoint(base_url: str) -> str:
    """Accept a bare host, a ``.../v1`` base, or a full ``.../embeddings`` URL."""
    if base_url.endswith("/embeddings"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/embeddings"
    return f"{base_url}/v1/embeddings"


def is_embedding_host_available() -> bool:
    """Return True if embeddings are enabled and the host is not in dead-host cooldown."""
    kind, base_url = resolve_embed_backend()
    if kind == "disabled":
        return False
    return not _is_host_in_cooldown(base_url)


def get_embedding_degradation_reason() -> str | None:
    """Human-readable reason when semantic embeddings are unavailable."""
    kind, base_url = resolve_embed_backend()
    if kind == "disabled":
        return "embeddings_disabled (ENGRAM_EMBED_URL=disabled)"
    if _is_host_in_cooldown(base_url):
        return _last_embed_failure_reason or "ollama_host_cooldown"
    return _last_embed_failure_reason


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


def embedding_matches_vec_schema(
    embedding: list[float], model: str, expected_dim: int | None = None
) -> tuple[bool, str | None]:
    """Return whether *embedding* can be stored in vec_memory.

    ``expected_dim`` is the live vec_memory dimension (``schema_meta.vec_dimension``);
    it defaults to VEC_EMBEDDING_DIMENSION for databases that predate flexible dims.
    Known models whose advertised dimension differs from the schema cannot be stored.
    """
    dim = expected_dim if expected_dim is not None else VEC_EMBEDDING_DIMENSION
    exp = expected_dimensions_for_model(model)
    if exp is not None and exp != dim:
        return False, (
            f"model {model!r} produces {exp}-dim vectors; vec_memory requires "
            f"{dim}. Run: engram migrate-embeddings --target-model {model}"
        )
    if len(embedding) != dim:
        return False, (
            f"embedding length {len(embedding)} != vec_memory dimension {dim}"
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
    global _last_embed_failure_reason

    if not text:
        return None

    kind, base_url = resolve_embed_backend()
    if kind == "disabled":
        _last_embed_failure_reason = "embeddings_disabled"
        return None

    active_model = model or _get_model()
    max_chars = config.embed_max_chars() or _MODEL_CONTEXT.get(active_model, 2000)

    if len(text) > max_chars:
        text = text[:max_chars]

    cache_key = (active_model, text)
    cached = _embed_cache.get(cache_key)
    if cached is not None:
        _embed_cache.move_to_end(cache_key)
        return list(cached)

    l2 = _persistent_cache_get(active_model, text)
    if l2 is not None:
        _embed_cache[cache_key] = list(l2)
        _embed_cache.move_to_end(cache_key)
        while len(_embed_cache) > EMBED_CACHE_MAX:
            _embed_cache.popitem(last=False)
        return l2

    if _is_host_in_cooldown(base_url):
        _last_embed_failure_reason = "ollama_host_cooldown"
        return None

    headers = {"Content-Type": "application/json"}
    if kind == "openai":
        url = _openai_embeddings_endpoint(base_url)
        data = json.dumps({"model": active_model, "input": text}).encode("utf-8")
        api_key = os.environ.get(EMBED_API_KEY_ENV, "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    else:
        url = f"{base_url.rstrip('/')}/api/embeddings"
        data = json.dumps({"model": active_model, "prompt": text}).encode("utf-8")

    import urllib.request

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as response:
            result = json.loads(response.read().decode())
            if kind == "openai":
                items = result.get("data") or []
                embedding = items[0].get("embedding") if items else None
            else:
                embedding = result.get("embedding")
            if embedding:
                _mark_host_success(base_url)
                _last_embed_failure_reason = None
                embedding = l2_normalize(embedding)
                _embed_cache[cache_key] = list(embedding)
                _embed_cache.move_to_end(cache_key)
                while len(_embed_cache) > EMBED_CACHE_MAX:
                    _embed_cache.popitem(last=False)
                _persistent_cache_put(active_model, text, embedding)
            return embedding
    except Exception:
        _mark_host_failure(base_url)
        _last_embed_failure_reason = "ollama_request_failed" if kind == "ollama" else "embed_request_failed"
        logger.exception(
            "Embedding request failed (backend=%s, model=%s, url=%s)",
            kind,
            active_model,
            url,
        )
        return None


def embed_batch(texts: list[str], model: str | None = None) -> list[list[float] | None]:
    """Embed many texts in one request; returns one vector (or None) per input.

    Uses Ollama's ``/api/embed`` (array input) or the OpenAI-compatible
    ``/v1/embeddings`` array form. One HTTP round-trip per batch instead of one
    per document — the difference between ~5s/doc and ~0.1s/doc on bulk ingest.
    Falls back to per-item ``embed_text`` if the batch endpoint fails (e.g. an
    older Ollama build). Results are cached like single embeds.
    """
    global _last_embed_failure_reason

    if not texts:
        return []

    kind, base_url = resolve_embed_backend()
    if kind == "disabled":
        _last_embed_failure_reason = "embeddings_disabled"
        return [None] * len(texts)

    active_model = model or _get_model()
    max_chars = config.embed_max_chars() or _MODEL_CONTEXT.get(active_model, 2000)
    prepared = [(t or "")[:max_chars] for t in texts]

    # Serve what we can from cache; batch only the misses.
    out: list[list[float] | None] = [None] * len(texts)
    miss_idx: list[int] = []
    for i, t in enumerate(prepared):
        if not t:
            continue
        cached = _embed_cache.get((active_model, t))
        if cached is not None:
            _embed_cache.move_to_end((active_model, t))
            out[i] = list(cached)
        else:
            miss_idx.append(i)
    if not miss_idx:
        return out

    if _is_host_in_cooldown(base_url):
        _last_embed_failure_reason = "ollama_host_cooldown"
        return out

    headers = {"Content-Type": "application/json"}
    inputs = [prepared[i] for i in miss_idx]
    if kind == "openai":
        url = _openai_embeddings_endpoint(base_url)
        api_key = os.environ.get(EMBED_API_KEY_ENV, "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    else:
        url = f"{base_url.rstrip('/')}/api/embed"
    data = json.dumps({"model": active_model, "input": inputs}).encode("utf-8")

    import urllib.request

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT * 4) as response:
            result = json.loads(response.read().decode())
            if kind == "openai":
                vectors = [item.get("embedding") for item in (result.get("data") or [])]
            else:
                vectors = result.get("embeddings") or []
    except Exception:
        logger.warning(
            "Batch embedding failed (backend=%s, n=%d); falling back to per-item requests",
            kind,
            len(inputs),
            exc_info=True,
        )
        for i in miss_idx:
            out[i] = embed_text(prepared[i], model=active_model)
        return out

    if len(vectors) != len(inputs):
        logger.warning(
            "Batch embed returned %d vectors for %d inputs; falling back per-item",
            len(vectors),
            len(inputs),
        )
        for i in miss_idx:
            out[i] = embed_text(prepared[i], model=active_model)
        return out

    _mark_host_success(base_url)
    _last_embed_failure_reason = None
    for i, vec in zip(miss_idx, vectors):
        if vec:
            vec = l2_normalize(vec)
            out[i] = vec
            _embed_cache[(active_model, prepared[i])] = list(vec)
            _embed_cache.move_to_end((active_model, prepared[i]))
    while len(_embed_cache) > EMBED_CACHE_MAX:
        _embed_cache.popitem(last=False)
    return out


def clear_embedding_cache() -> None:
    """Drop all cached embeddings (tests; or after switching Ollama model weights)."""
    _embed_cache.clear()


def get_embedding_model() -> str:
    """Return the currently configured embedding model name (for display/logging)."""
    return _get_model()
