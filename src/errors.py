"""Structured Engram errors for CLI and MCP surfaces."""

from __future__ import annotations

from typing import Any


class EngramError(Exception):
    """Base error with a stable machine-readable ``code``."""

    code = "engram_error"
    hint = ""

    def __init__(self, message: str, *, hint: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload

    def to_json_line(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False)


class NotFoundError(EngramError):
    code = "not_found"
    hint = "Check the item type and id (e.g. skill:12)."


class DuplicateBlocked(EngramError):
    code = "duplicate_blocked"
    hint = "Use --force / force=true to insert anyway, or merge with the existing item."


class EmbeddingUnavailable(EngramError):
    code = "embedding_unavailable"
    hint = "Start Ollama or set ENGRAM_EMBED_URL; lexical search still works."


class WorkflowViolation(EngramError):
    code = "workflow_violation"
    hint = "Advance the session phase or use an allowed write path."


class ValidationError(EngramError):
    code = "validation_error"
    hint = "Fix the arguments and retry."


class ElicitationFailedError(EngramError):
    """Structured twin of ``mcp.protocol.ElicitationFailed`` (keep that name for callers)."""

    code = "elicitation_failed"
    hint = "Confirmation round-trip failed; refusing the mutating action."


def format_error(exc: BaseException) -> str:
    """Human-readable one-liner for CLI stderr."""
    if isinstance(exc, EngramError):
        base = f"Error [{exc.code}]: {exc.message}"
        return f"{base} ({exc.hint})" if exc.hint else base
    return f"Error: {exc}"
