"""
Shared LLM helpers — policy-routed, cost-ledgered, graceful-unavailability
wrappers over three backends.

* **anthropic** — Claude Messages API via the official SDK (``llm_anthropic.py``).
* **openai** — any OpenAI-compatible chat endpoint (``ENGRAM_LLM_BASE_URL``,
  default Ollama's ``/v1``; OpenRouter, Gemini, DeepSeek, …).
* **ollama** — the local Ollama (``OLLAMA_HOST``), native ``/api/generate`` or
  its ``/v1`` chat.

Every call is a *job* (``extract``, ``audit``, ``gc``, ``merge``, ``decide``,
``summarize``, …). The policy file (``policy.py``) maps a job to a provider,
model, mode, privacy class and fallback chain; the provider chain is tried in
order, a provider not cleared for the job's privacy class is skipped and the
refusal logged, and every attempt lands in the ledger with tokens and cost
(``source: llm_call``) so ``engram roi`` reports spend per job and model.

Precedence: explicit call args > per-task env override > policy job entry >
provider default. With no policy file the behaviour is exactly the pre-policy
one: a single provider, no fallback.

Contract everywhere: text or ``None``; never raises.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from . import config

logger = logging.getLogger(__name__)

# Generation timeout (seconds). CPU-only Ollama needs headroom: a cold
# llama3.2 load plus a multi-sentence answer regularly exceeds 60s.
_LLM_TIMEOUT = int(os.environ.get("ENGRAM_LLM_TIMEOUT", "60"))
_DEFAULT_MODEL = "llama3.2"

_TASK_MODEL_ENV: dict[str, str] = {
    "extract": "ENGRAM_LLM_EXTRACT_MODEL",
    "audit": "ENGRAM_LLM_AUDIT_MODEL",
    "gc": "ENGRAM_LLM_AUDIT_MODEL",
    "merge": "ENGRAM_LLM_MODEL",
    "decide": "ENGRAM_LLM_DECIDE_MODEL",
}
# The hub spec bootstraps every decision question on Haiku (judgements, not prose).
ANTHROPIC_DECIDE_MODEL = "claude-haiku-5-5"

PROVIDER_ENV = "ENGRAM_LLM_PROVIDER"  # anthropic | openai | ollama | (unset = policy/auto)
PROVIDERS = ("anthropic", "openai", "ollama")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


# ---------------------------------------------------------------------------
# URLs + providers
# ---------------------------------------------------------------------------


def _ollama_base_url() -> str:
    return config.ollama_host()


def resolve_llm_base_url() -> str:
    """Return the OpenAI-compatible chat API base URL."""
    return config.llm_base_url()


def _is_ollama_chat_backend(base_url: str) -> bool:
    ollama_root = _ollama_base_url()
    return base_url.startswith(ollama_root)


def _is_local_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


def _auto_provider() -> str:
    """Pre-policy detection: an ``sk-ant-`` key or an ``anthropic.com`` base URL
    selects Anthropic; everything else is the OpenAI-compatible path."""
    if config.llm_api_key().startswith("sk-ant-"):
        return "anthropic"
    if "anthropic.com" in os.environ.get("ENGRAM_LLM_BASE_URL", ""):
        return "anthropic"
    return "openai"


def _job_policy(task: str | None, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    from .policy import load_policy

    gen = (policy or load_policy())["generate"]
    job = dict(gen.get("default") or {})
    if task:
        job.update(gen.get("jobs", {}).get(task) or {})
    return job


def llm_provider(task: str | None = None) -> str:
    """Primary provider for *task*: explicit env > policy job entry > auto-detect.
    Returns ``anthropic``, ``openai`` or ``ollama``."""
    explicit = os.environ.get(PROVIDER_ENV, "").strip().lower()
    if explicit == "anthropic":
        return "anthropic"
    if explicit == "ollama":
        return "ollama"
    if explicit == "openai":
        return "openai"
    if task:
        provider = _job_policy(task).get("provider")
        if provider in PROVIDERS:
            return str(provider)
    return _auto_provider()


def _provider_allows(provider: str, privacy: str, gen: dict[str, Any]) -> bool:
    """May *privacy*-class content go to *provider*? A local OpenAI-compatible
    endpoint (Ollama's /v1 on localhost) is always cleared; remote ones only
    when the policy says so."""
    if provider == "openai" and _is_local_url(config.llm_base_url()):
        return True
    cfg = (gen.get("providers") or {}).get(provider) or {}
    classes = cfg.get("privacy")
    if not isinstance(classes, list):
        return provider != "openai"
    return privacy in classes


def resolve_job(task: str | None, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """The routing plan for *task*: ordered allowed providers, refusals, mode, privacy."""
    from .policy import load_policy

    pol = policy or load_policy()
    gen = pol["generate"]
    job = _job_policy(task, pol)
    primary = llm_provider(task)
    chain = [primary] + [
        str(p) for p in (job.get("fallback") or []) if p in PROVIDERS and p != primary
    ]
    privacy = str(job.get("privacy") or "personal")
    allowed: list[str] = []
    refused: list[tuple[str, str]] = []
    for p in chain:
        if _provider_allows(p, privacy, gen):
            allowed.append(p)
        else:
            refused.append((p, f"{privacy} content is not cleared for {p}"))
    return {
        "job": task or "chat",
        "primary": primary,
        "providers": allowed,
        "refused": refused,
        "mode": str(job.get("mode") or "realtime"),
        "privacy": privacy,
    }


def _model_fits(provider: str, model: str) -> bool:
    is_claude = model.startswith("claude-")
    return is_claude if provider == "anthropic" else not is_claude


def resolve_llm_model(
    model: str | None = None, task: str | None = None, provider: str | None = None
) -> str:
    """Model for *task* on *provider*: explicit > per-task env > policy job > provider default.

    A model that cannot run on the provider (a ``claude-*`` name on Ollama, or
    the reverse — e.g. a fallback hop) falls through to the provider default.
    """
    if model:
        return model
    provider = provider or llm_provider(task)
    if task:
        env_key = _TASK_MODEL_ENV.get(task)
        override = os.environ.get(env_key, "").strip() if env_key else ""
        if override and _model_fits(provider, override):
            return override
        job_model = str(_job_policy(task).get("model") or "").strip()
        if job_model and _model_fits(provider, job_model):
            return job_model
    if provider == "anthropic":
        if task == "decide":
            return ANTHROPIC_DECIDE_MODEL
        configured = os.environ.get("ENGRAM_LLM_MODEL", "").strip()
        if configured and configured.startswith("claude-"):
            return configured
        from .llm_anthropic import DEFAULT_MODEL

        return DEFAULT_MODEL
    configured = config.llm_model()
    return configured if _model_fits(provider, configured) else _DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Availability + status
# ---------------------------------------------------------------------------


def is_llm_available(timeout: float = 2.0, task: str | None = None) -> bool:
    """Return True if the primary backend for *task* responds."""
    provider = llm_provider(task)
    if provider == "anthropic":
        from .llm_anthropic import is_available

        return is_available(resolve_llm_model(task=task, provider="anthropic"), timeout=timeout)
    base = _ollama_base_url() + "/v1" if provider == "ollama" else resolve_llm_base_url()
    if _is_ollama_chat_backend(base):
        url = f"{_ollama_base_url()}/api/tags"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    api_key = config.llm_api_key()
    for path in ("/models", ""):
        url = f"{base}{path}" if path else base
        req = urllib.request.Request(url, method="GET")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status == 200:
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return False


def get_llm_status() -> dict:
    """Return LLM configuration, availability and the resolved job policy for CLI/MCP."""
    from .policy import policy_path

    available = is_llm_available()
    tasks_enabled: list[str] = []
    if available:
        tasks_enabled = ["consolidation_audit", "gc_scoring", "auto_extract", "merge"]
    provider = llm_provider()
    status: dict = {
        "provider": provider,
        "base_url": "https://api.anthropic.com" if provider == "anthropic" else resolve_llm_base_url(),
        "model": resolve_llm_model(),
        "audit_model": resolve_llm_model(task="audit"),
        "extract_model": resolve_llm_model(task="extract"),
        "available": available,
        "tasks_enabled": tasks_enabled,
        "api_key_set": bool(config.llm_api_key()),
    }
    if provider == "anthropic":
        from .llm_anthropic import sdk_installed

        status["sdk_installed"] = sdk_installed()
    jobs: dict[str, dict] = {}
    for job in ("extract", "audit", "gc", "merge", "decide", "summarize"):
        plan = resolve_job(job)
        jobs[job] = {
            "providers": plan["providers"],
            "refused": [f"{p}: {why}" for p, why in plan["refused"]],
            "model": resolve_llm_model(task=job, provider=plan["providers"][0]) if plan["providers"] else None,
            "mode": plan["mode"],
            "privacy": plan["privacy"],
        }
    status["policy"] = {"path": policy_path(), "jobs": jobs}
    return status


# ---------------------------------------------------------------------------
# Executors: (text, usage) — never raise
# ---------------------------------------------------------------------------


def _usage_from_openai(result: dict) -> dict[str, int] | None:
    u = result.get("usage") or {}
    if not isinstance(u, dict) or not u:
        return None
    return {
        "input_tokens": int(u.get("prompt_tokens") or 0),
        "output_tokens": int(u.get("completion_tokens") or 0),
    }


def _ollama_generate(prompt: str, system: str, model: str, timeout: int) -> tuple[str | None, dict | None]:
    """Native Ollama ``/api/generate``."""
    url = f"{_ollama_base_url()}/api/generate"
    payload: dict = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode())
            text = (result.get("response") or "").strip()
            usage = None
            if result.get("prompt_eval_count") is not None or result.get("eval_count") is not None:
                usage = {
                    "input_tokens": int(result.get("prompt_eval_count") or 0),
                    "output_tokens": int(result.get("eval_count") or 0),
                }
            return (text or None), usage
    except Exception:
        logger.exception("Ollama generate failed (model=%s)", model)
        return None, None


def _openai_chat(
    messages: list[dict],
    model: str,
    *,
    base_url: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
) -> tuple[str | None, dict | None]:
    """OpenAI-compatible ``/chat/completions``."""
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode())
            choices = result.get("choices") or []
            if not choices:
                return None, _usage_from_openai(result)
            message = choices[0].get("message") or {}
            content = (message.get("content") or "").strip()
            return (content or None), _usage_from_openai(result)
    except Exception:
        logger.exception("Chat completion failed (model=%s, url=%s)", model, url)
        return None, None


def _anthropic_chat(messages: list[dict], model: str, max_tokens: int, timeout: int) -> tuple[str | None, dict | None]:
    from .llm_anthropic import chat_ex

    return chat_ex(messages, model=model, max_tokens=max_tokens, timeout=timeout)


def _anthropic_generate(prompt: str, system: str, model: str, timeout: int) -> tuple[str | None, dict | None]:
    from .llm_anthropic import complete_ex

    return complete_ex(prompt, system=system, model=model, timeout=timeout)


def _ledger(
    job: str, provider: str, model: str, mode: str, usage: dict | None, latency_ms: float, outcome: str
) -> None:
    """One ``llm_call`` ledger row per attempt (cost from the policy price table; local = 0)."""
    try:
        from .policy import estimate_cost_usd
        from .search_audit import append_llm_call_audit

        local = provider == "ollama" or (provider == "openai" and _is_local_url(config.llm_base_url()))
        cost = 0.0 if local else estimate_cost_usd(model, usage)
        append_llm_call_audit(
            job=job, provider=provider, model=model, mode=mode, usage=usage,
            latency_ms=latency_ms, outcome=outcome, cost_usd=cost,
        )
    except Exception:
        logger.debug("llm ledger write failed", exc_info=True)


def _run_chain(job: str, model: str | None, task: str | None, attempt) -> str | None:
    """Try the job's allowed providers in order; log every attempt and refusal."""
    plan = resolve_job(task)
    for p, why in plan["refused"]:
        _ledger(job, p, "", plan["mode"], None, 0.0, f"refused: {why}")
        logger.warning(
            "llm policy: %s skipped for job %r — %s. Clear it with "
            "`engram policy set generate.providers.%s.privacy '[\"personal\",\"public\"]'` "
            "or mark the job public.", p, job, why, p,
        )
    for provider in plan["providers"]:
        m = resolve_llm_model(model if provider == plan["primary"] else None, task=task, provider=provider)
        t0 = time.perf_counter()
        text, usage = attempt(provider, m)
        latency = (time.perf_counter() - t0) * 1000.0
        _ledger(job, provider, m, plan["mode"], usage, latency, "ok" if text else "unavailable")
        if text:
            return text
    return None


# ---------------------------------------------------------------------------
# Public calls
# ---------------------------------------------------------------------------


def call_ollama_generate(
    prompt: str,
    *,
    system: str = "",
    model: str | None = None,
    task: str | None = None,
    timeout: int = _LLM_TIMEOUT,
) -> str | None:
    """Generate text for *task* through the policy chain.

    Historical name: on the OpenAI-compatible/Ollama providers this is the
    native Ollama ``/api/generate``; under Anthropic it is the Messages API.
    """
    job = task or "generate"

    def attempt(provider: str, m: str) -> tuple[str | None, dict | None]:
        if provider == "anthropic":
            return _anthropic_generate(prompt, system, m, timeout)
        return _ollama_generate(prompt, system, m, timeout)

    return _run_chain(job, model, task, attempt)


def call_chat_completion(
    messages: list[dict],
    *,
    model: str | None = None,
    task: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 800,
    timeout: int = _LLM_TIMEOUT,
) -> str | None:
    """Chat-completion for *task* through the policy chain (OpenAI-compatible
    endpoint, Ollama's ``/v1``, or the Anthropic Messages API)."""
    job = task or "chat"

    def attempt(provider: str, m: str) -> tuple[str | None, dict | None]:
        if provider == "anthropic":
            return _anthropic_chat(messages, m, max_tokens, timeout)
        if provider == "ollama":
            return _openai_chat(
                messages, m, base_url=f"{_ollama_base_url()}/v1", api_key="",
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            )
        return _openai_chat(
            messages, m, base_url=resolve_llm_base_url(), api_key=config.llm_api_key(),
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        )

    return _run_chain(job, model, task, attempt)


def parse_json_from_llm(raw: str) -> object | None:
    """Extract JSON from an LLM response (handles markdown fences)."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if text.startswith("```json"):
        text = text[7:].strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
