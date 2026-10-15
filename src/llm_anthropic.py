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


# Besides ENGRAM_LLM_API_KEY the SDK resolves its own credential sources: an
# ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN, an `ant auth login` profile, or
# Workload Identity Federation (ANTHROPIC_FEDERATION_RULE_ID + token file).
_SDK_CREDENTIAL_ENVS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_PROFILE")


def credentials_present() -> bool:
    return bool(config.llm_api_key()) or any(os.environ.get(k) for k in _SDK_CREDENTIAL_ENVS)


def _client(timeout: float) -> Any:
    import anthropic  # pyright: ignore[reportMissingImports]  # optional extra

    return anthropic.Anthropic(
        api_key=config.llm_api_key() or None, timeout=float(timeout), max_retries=2
    )


def is_available(model: str, timeout: float = 2.0) -> bool:
    """True when the SDK is installed, the key is accepted and *model* exists."""
    if not sdk_installed() or not credentials_present():
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


def _usage_of(response: Any) -> dict[str, int] | None:
    u = getattr(response, "usage", None)
    if u is None:
        return None
    out: dict[str, int] = {}
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = getattr(u, k, None)
        if isinstance(v, int):
            out[k] = v
    return out or None


def generate(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    system: str = "",
    max_tokens: int = 4096,
    timeout: float = 60.0,
) -> str | None:
    """One Messages API call; text or None. Never raises."""
    return generate_ex(model=model, messages=messages, system=system, max_tokens=max_tokens, timeout=timeout)[0]


def generate_ex(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    system: str = "",
    max_tokens: int = 4096,
    timeout: float = 60.0,
) -> tuple[str | None, dict[str, int] | None]:
    """``generate`` plus the token usage (for the cost ledger)."""
    if not sdk_installed():
        logger.warning(
            "ENGRAM_LLM_PROVIDER=anthropic but the SDK is not installed: "
            "pip install 'engram-memory[anthropic]'"
        )
        return None, None
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
        return None, None
    except anthropic.NotFoundError:
        logger.error("anthropic: unknown model %r (ENGRAM_LLM_MODEL)", model)
        return None, None
    except anthropic.RateLimitError as exc:
        logger.warning("anthropic: rate limited (retry-after=%s)", exc.response.headers.get("retry-after"))
        return None, None
    except anthropic.APIStatusError as exc:
        logger.error("anthropic: API error %s: %s", exc.status_code, exc.message)
        return None, None
    except anthropic.APIConnectionError:
        logger.error("anthropic: connection error")
        return None, None
    except TypeError as exc:  # an option the installed SDK does not know (e.g. output_config)
        logger.error("anthropic: SDK rejected request options (%s); upgrade the SDK", exc)
        return None, None
    return _text_of(response), _usage_of(response)


def complete_ex(
    prompt: str, *, system: str = "", model: str, max_tokens: int = 4096, timeout: float = 60.0
) -> tuple[str | None, dict[str, int] | None]:
    return generate_ex(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        system=system,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def complete(prompt: str, *, system: str = "", model: str, max_tokens: int = 4096, timeout: float = 60.0) -> str | None:
    return complete_ex(prompt, system=system, model=model, max_tokens=max_tokens, timeout=timeout)[0]


def chat_ex(
    messages: list[dict], *, model: str, max_tokens: int = 800, timeout: float = 60.0
) -> tuple[str | None, dict[str, int] | None]:
    """OpenAI-style message list → Messages API (system turns become ``system``)."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system").strip()
    turns = [
        {"role": m["role"], "content": str(m.get("content") or "")}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "Continue."})
    return generate_ex(model=model, messages=turns, system=system, max_tokens=max_tokens, timeout=timeout)


def chat(messages: list[dict], *, model: str, max_tokens: int = 800, timeout: float = 60.0) -> str | None:
    return chat_ex(messages, model=model, max_tokens=max_tokens, timeout=timeout)[0]
