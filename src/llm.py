"""
Shared LLM helpers — local Ollama generate/chat with graceful unavailability.

Per-task model routing via ENGRAM_LLM_EXTRACT_MODEL and ENGRAM_LLM_AUDIT_MODEL.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

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
}


def _ollama_base_url() -> str:
    return config.ollama_host()


def resolve_llm_base_url() -> str:
    """Return the OpenAI-compatible chat API base URL."""
    return config.llm_base_url()


def resolve_llm_model(model: str | None = None, task: str | None = None) -> str:
    """Resolve model name with optional per-task override."""
    if model:
        return model
    if task:
        env_key = _TASK_MODEL_ENV.get(task)
        if env_key:
            override = os.environ.get(env_key, "").strip()
            if override:
                return override
    return config.llm_model()


def _is_ollama_chat_backend(base_url: str) -> bool:
    ollama_root = _ollama_base_url()
    return base_url.startswith(ollama_root)


def is_llm_available(timeout: float = 2.0) -> bool:
    """Return True if the configured LLM backend responds."""
    base = resolve_llm_base_url()
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
    """Return LLM configuration and availability for CLI/MCP."""
    available = is_llm_available()
    tasks_enabled: list[str] = []
    if available:
        tasks_enabled = ["consolidation_audit", "gc_scoring", "auto_extract", "merge"]
    return {
        "base_url": resolve_llm_base_url(),
        "model": resolve_llm_model(),
        "audit_model": resolve_llm_model(task="audit"),
        "extract_model": resolve_llm_model(task="extract"),
        "available": available,
        "tasks_enabled": tasks_enabled,
        "api_key_set": bool(config.llm_api_key()),
    }


def call_ollama_generate(
    prompt: str,
    *,
    system: str = "",
    model: str | None = None,
    task: str | None = None,
    timeout: int = _LLM_TIMEOUT,
) -> str | None:
    """Call Ollama ``/api/generate`` and return response text."""
    ollama_model = resolve_llm_model(model, task=task)
    url = f"{_ollama_base_url()}/api/generate"
    payload: dict = {
        "model": ollama_model,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        payload["system"] = system
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode())
            text = (result.get("response") or "").strip()
            return text or None
    except Exception:
        logger.exception("Ollama generate failed (model=%s)", ollama_model)
        return None


def call_chat_completion(
    messages: list[dict],
    *,
    model: str | None = None,
    task: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 800,
    timeout: int = _LLM_TIMEOUT,
) -> str | None:
    """Call OpenAI-compatible ``/v1/chat/completions`` (Ollama, OpenRouter, etc.)."""
    base = resolve_llm_base_url()
    api_key = config.llm_api_key()
    chat_model = resolve_llm_model(model, task=task)
    url = f"{base}/chat/completions"
    payload = {
        "model": chat_model,
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
                return None
            message = choices[0].get("message") or {}
            content = (message.get("content") or "").strip()
            return content or None
    except Exception:
        logger.exception("Chat completion failed (model=%s, url=%s)", chat_model, url)
        return None


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
