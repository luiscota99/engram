"""Central configuration — every environment variable Engram reads, in one place.

All values are resolved at call time (never at import time) so tests and tooling
can set variables after import. Modules with domain-specific resolution logic
(embedding model aliasing in ``src/embeddings.py``, per-task LLM overrides in
``src/llm.py``) keep that logic but read raw values through these accessors.

Environment variables:

Database
    ENGRAM_DB_PATH              SQLite database path (default ~/.engram/memory.db)

Embeddings (see src/embeddings.py)
    ENGRAM_EMBED_MODEL          Embedding model name (default nomic-embed-text)
    ENGRAM_EMBEDDING_MODEL      Deprecated alias of ENGRAM_EMBED_MODEL
    ENGRAM_EMBED_URL            unset → Ollama; "disabled" → embeddings off;
                                any other URL → OpenAI-compatible /v1/embeddings
    ENGRAM_EMBED_API_KEY        Bearer token for the OpenAI-compatible backend
    OLLAMA_HOST                 Ollama base URL (default http://localhost:11434)

LLM layer (see src/llm.py)
    ENGRAM_LLM_BASE_URL         OpenAI-compatible chat base URL (default: Ollama /v1)
    ENGRAM_LLM_MODEL            Default chat model (default llama3.2)
    ENGRAM_LLM_API_KEY          Bearer token for the chat backend
    ENGRAM_LLM_EXTRACT_MODEL    Per-task override: memory extraction
    ENGRAM_LLM_AUDIT_MODEL      Per-task override: consolidation audit

Logging / limits
    ENGRAM_AUDIT_LOG            Path for search-audit JSONL (unset → no auditing)
    ENGRAM_SESSION_HELP_LOG     Path for session-help scores (default ~/.engram/session-help.jsonl)
    ENGRAM_MAX_CONTEXT_CHARS    MCP context budget in characters (default 8000)

Integrations
    CLAW_PATH                   Path to the claw binary (optional)
"""
from __future__ import annotations

import json
import os

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_LLM_MODEL = "llama3.2"
DEFAULT_MAX_CONTEXT_CHARS = 8000
DEFAULT_SESSION_HELP_LOG = "~/.engram/session-help.jsonl"


def ollama_host() -> str:
    """Ollama base URL without trailing slash."""
    return os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")


def ollama_keep_alive() -> str:
    """How long Ollama keeps the embed model resident after a request.

    Engram embeds on nearly every turn (recall/guard hooks), so the default 5m
    eviction means an idle gap forces the ~8s cold model reload on the next
    prompt — the recurring stall behind the "intermittent" feel. A longer
    keep-alive holds the model (~0.5GB RAM for nomic) so warm calls stay ~1s.
    Override with ENGRAM_OLLAMA_KEEP_ALIVE (Ollama syntax: "30m", "-1" for
    indefinite, "0" to disable and restore stock eviction).
    """
    return os.environ.get("ENGRAM_OLLAMA_KEEP_ALIVE", "30m").strip() or "30m"


def llm_base_url() -> str:
    """OpenAI-compatible chat API base URL (defaults to Ollama's /v1)."""
    return os.environ.get("ENGRAM_LLM_BASE_URL", f"{ollama_host()}/v1").rstrip("/")


def llm_model() -> str:
    return os.environ.get("ENGRAM_LLM_MODEL", DEFAULT_LLM_MODEL)


def llm_api_key() -> str:
    return os.environ.get("ENGRAM_LLM_API_KEY", "").strip()


def engram_dir() -> str:
    """Directory that holds the DB, audit log, and persistent config.

    Derived from ENGRAM_DB_PATH (default ``~/.engram``) so every per-store file
    lives together and multiple databases keep independent settings.
    """
    db = os.environ.get("ENGRAM_DB_PATH")
    if db:
        return os.path.dirname(os.path.abspath(os.path.expanduser(db)))
    return os.path.join(os.path.expanduser("~"), ".engram")


def _persistent_config_path() -> str:
    return os.path.join(engram_dir(), "config.json")


def read_persistent() -> dict:
    """Load the on-disk settings map (``{}`` if missing or unreadable)."""
    try:
        with open(_persistent_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_persistent(key: str, value) -> None:
    """Set one persistent setting, preserving the rest."""
    data = read_persistent()
    data[key] = value
    path = _persistent_config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def get_persistent(key: str, default=None):
    return read_persistent().get(key, default)


def default_audit_log_path() -> str:
    """Where auditing writes when enabled without an explicit ENGRAM_AUDIT_LOG."""
    return os.path.join(engram_dir(), "audit.jsonl")


def audit_log_path() -> str | None:
    """Resolve the search-audit log path.

    Precedence: an explicit ``ENGRAM_AUDIT_LOG`` env var always wins; otherwise,
    if auditing was turned on persistently (``engram audit on``), use the default
    path next to the database; otherwise auditing is off.
    """
    env = os.environ.get("ENGRAM_AUDIT_LOG")
    if env:
        return env
    if get_persistent("audit_enabled") is True:
        return default_audit_log_path()
    return None


def session_help_log_path() -> str:
    return os.path.expanduser(
        os.environ.get("ENGRAM_SESSION_HELP_LOG", DEFAULT_SESSION_HELP_LOG)
    )


def max_context_chars() -> int:
    raw = os.environ.get("ENGRAM_MAX_CONTEXT_CHARS", "")
    try:
        return int(raw) if raw else DEFAULT_MAX_CONTEXT_CHARS
    except ValueError:
        return DEFAULT_MAX_CONTEXT_CHARS


def embed_max_chars() -> int | None:
    """Optional cap on characters embedded per document (ENGRAM_EMBED_MAX_CHARS).

    Embedding time scales ~linearly with input length; measured on Apple
    Silicon, a 6000-char doc costs ~5.4s with nomic-embed-text. Halving the
    cap roughly halves bulk-ingest time at some recall cost. Unset = per-model
    default from the known-models table."""
    raw = os.environ.get("ENGRAM_EMBED_MAX_CHARS", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def defer_embed() -> bool:
    """When true (ENGRAM_DEFER_EMBED=1), writes skip inline embedding and mark the
    row pending; a batched ``engram reembed`` sweep generates vectors later.
    Use for bulk ingest — turns ~5s/doc into sub-second writes."""
    return os.environ.get("ENGRAM_DEFER_EMBED", "").strip().lower() in ("1", "true", "yes")


def claw_path() -> str | None:
    return os.environ.get("CLAW_PATH") or None
