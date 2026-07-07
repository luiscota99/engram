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

import os

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_LLM_MODEL = "llama3.2"
DEFAULT_MAX_CONTEXT_CHARS = 8000
DEFAULT_SESSION_HELP_LOG = "~/.engram/session-help.jsonl"


def ollama_host() -> str:
    """Ollama base URL without trailing slash."""
    return os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")


def llm_base_url() -> str:
    """OpenAI-compatible chat API base URL (defaults to Ollama's /v1)."""
    return os.environ.get("ENGRAM_LLM_BASE_URL", f"{ollama_host()}/v1").rstrip("/")


def llm_model() -> str:
    return os.environ.get("ENGRAM_LLM_MODEL", DEFAULT_LLM_MODEL)


def llm_api_key() -> str:
    return os.environ.get("ENGRAM_LLM_API_KEY", "").strip()


def audit_log_path() -> str | None:
    return os.environ.get("ENGRAM_AUDIT_LOG") or None


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


def claw_path() -> str | None:
    return os.environ.get("CLAW_PATH") or None
