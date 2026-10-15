"""Anthropic-native LLM backend (Claude Messages API via the official SDK).

Selected by ``ENGRAM_LLM_PROVIDER=anthropic`` (or auto-detected from an
``sk-ant-`` key / an ``anthropic.com`` base URL — see ``llm.llm_provider``).
The OpenAI-compatible path in ``llm.py`` is untouched: Ollama, OpenRouter and
friends keep working exactly as before.

Contract matches the rest of the LLM layer: return text or ``None``, never
raise — a missing SDK, a bad key, a rate limit or a refusal all degrade to
"LLM unavailable" and are logged. The SDK is an optional extra
(``pip install 'engram-memory[anthropic]'``) so the core install stays lean.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any

from . import config

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
EFFORT_ENV = "ENGRAM_LLM_EFFORT"  # low | medium | high | xhigh | max (SDK >= 1.0)
# Server-side refusal fallbacks exist only for these models; sending the
# parameter for others (e.g. Haiku) is a 400.
_FALLBACK_MODELS = frozenset(
    {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5"}
)
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def sdk_installed() -> bool:
    try:
        import anthropic  # noqa: F401  # pyright: ignore[reportMissingImports]
    except ImportError:
        return False
    return True


def _client(timeout: float) -> Any:
    import anthropic  # pyright: ignore[reportMissingImports]  # optional extra

    return anthropic.Anthropic(
        api_key=config.llm_api_key() or None, timeout=float(timeout), max_retries=2
    )


def is_available(model: str, timeout: float = 2.0) -> bool:
    """True when the SDK is installed, the key is accepted and *model* exists."""
    if not sdk_installed() or not config.llm_api_key():
        return False
    try:
        _client(timeout).models.retrieve(model)
        return True
    except Exception as exc:  # auth, 404 model, network — all mean "not available"
        logger.debug("anthropic availability probe failed: %s", type(exc).__name__)
        return False


def _text_of(response: Any) -> str | None:
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        logger.warning(
            "anthropic declined the request (category=%s)",
            getattr(details, "category", None) if details else None,
        )
        return None
    parts = [
        getattr(block, "text", "")
        for block in getattr(response, "content", []) or []
        if getattr(block, "type", "") == "text"
    ]
    text = "".join(parts).strip()
    return text or None


def _create(client: Any, model: str, **kwargs: Any) -> Any:
    """``messages.create`` with server-side refusal fallbacks where the model supports them."""
    if model in _FALLBACK_MODELS:
        try:
            return client.beta.messages.create(
                model=model, betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
            )
        except TypeError:
            logger.debug("installed anthropic SDK predates fallbacks; plain messages.create")
    return client.messages.create(model=model, **kwargs)


def generate(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    system: str = "",
    max_tokens: int = 4096,
    timeout: float = 60.0,
) -> str | None:
    """One Messages API call; text or None. Never raises."""
    if not sdk_installed():
        logger.warning(
            "ENGRAM_LLM_PROVIDER=anthropic but the SDK is not installed: "
            "pip install 'engram-memory[anthropic]'"
        )
        return None
    import anthropic  # pyright: ignore[reportMissingImports]

    kwargs: dict[str, Any] = {"max_tokens": int(max_tokens), "messages": list(messages)}
    if system:
        kwargs["system"] = system
    effort = os.environ.get(EFFORT_ENV, "").strip().lower()
    if effort:
        kwargs["output_config"] = {"effort": effort}
    try:
        response = _create(_client(timeout), model, **kwargs)
    except anthropic.AuthenticationError:
        logger.error("anthropic: invalid API key (ENGRAM_LLM_API_KEY)")
        return None
    except anthropic.NotFoundError:
        logger.error("anthropic: unknown model %r (ENGRAM_LLM_MODEL)", model)
        return None
    except anthropic.RateLimitError as exc:
        logger.warning("anthropic: rate limited (retry-after=%s)", exc.response.headers.get("retry-after"))
        return None
    except anthropic.APIStatusError as exc:
        logger.error("anthropic: API error %s: %s", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError:
        logger.error("anthropic: connection error")
        return None
    except TypeError as exc:  # an option the installed SDK does not know (e.g. output_config)
        logger.error("anthropic: SDK rejected request options (%s); upgrade the SDK", exc)
        return None
    return _text_of(response)


def complete(prompt: str, *, system: str = "", model: str, max_tokens: int = 4096, timeout: float = 60.0) -> str | None:
    return generate(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        system=system,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def chat(messages: list[dict], *, model: str, max_tokens: int = 800, timeout: float = 60.0) -> str | None:
    """OpenAI-style message list → Messages API (system turns become ``system``)."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system").strip()
    turns = [
        {"role": m["role"], "content": str(m.get("content") or "")}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "Continue."})
    return generate(model=model, messages=turns, system=system, max_tokens=max_tokens, timeout=timeout)
