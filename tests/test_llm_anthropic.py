"""Anthropic-native LLM backend: provider selection, dispatch, SDK shapes, degradation.

A fake ``anthropic`` module is injected per test — no network, no real SDK needed.
"""

from __future__ import annotations

import sys
import types

import pytest

from src import llm


class _Block:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class _Resp:
    def __init__(self, blocks, stop_reason: str = "end_turn") -> None:
        self.content = blocks
        self.stop_reason = stop_reason
        self.stop_details = None


def _fake_sdk(monkeypatch, *, beta_fallbacks: bool = True, response=None):
    """Install a fake ``anthropic`` module. ``state["raise"]`` makes create() raise."""
    calls: list = []
    state: dict = {"raise": None}
    mod = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class APIStatusError(APIError):
        def __init__(self, status_code: int = 500, message: str = "boom") -> None:
            super().__init__(message)
            self.status_code = status_code
            self.message = message
            self.response = types.SimpleNamespace(headers={"retry-after": "7"})

    class AuthenticationError(APIStatusError):
        pass

    class NotFoundError(APIStatusError):
        pass

    class RateLimitError(APIStatusError):
        pass

    class APIConnectionError(APIError):
        pass

    class _Messages:
        def __init__(self, beta: bool) -> None:
            self.beta = beta

        def create(self, **kw):
            if self.beta and not beta_fallbacks and "fallbacks" in kw:
                raise TypeError("create() got an unexpected keyword argument 'fallbacks'")
            calls.append(("beta" if self.beta else "plain", kw))
            if state["raise"] is not None:
                raise state["raise"]
            return response if response is not None else _Resp([_Block("text", "ok")])

    class _Models:
        def retrieve(self, model):
            calls.append(("models.retrieve", model))
            return {"id": model}

    class Anthropic:
        def __init__(self, api_key=None, timeout=None, max_retries=None) -> None:
            calls.append(("client", {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}))
            self.messages = _Messages(False)
            self.beta = types.SimpleNamespace(messages=_Messages(True))
            self.models = _Models()

    for name, cls in {
        "Anthropic": Anthropic, "APIError": APIError, "APIStatusError": APIStatusError,
        "AuthenticationError": AuthenticationError, "NotFoundError": NotFoundError,
        "RateLimitError": RateLimitError, "APIConnectionError": APIConnectionError,
    }.items():
        setattr(mod, name, cls)
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    mod.state = state  # type: ignore[attr-defined]
    return mod, calls


@pytest.fixture
def anthropic_env(monkeypatch):
    monkeypatch.setenv("ENGRAM_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-ant-test-0000")
    for k in ("ENGRAM_LLM_MODEL", "ENGRAM_LLM_EXTRACT_MODEL", "ENGRAM_LLM_AUDIT_MODEL", "ENGRAM_LLM_EFFORT", "ENGRAM_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


# --- provider selection ---------------------------------------------------------


def test_provider_detection(monkeypatch):
    for k in ("ENGRAM_LLM_PROVIDER", "ENGRAM_LLM_API_KEY", "ENGRAM_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert llm.llm_provider() == "openai"
    monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-ant-abc")
    assert llm.llm_provider() == "anthropic"
    monkeypatch.setenv("ENGRAM_LLM_PROVIDER", "openai")  # explicit wins over the key prefix
    assert llm.llm_provider() == "openai"
    monkeypatch.setenv("ENGRAM_LLM_PROVIDER", "ollama")
    assert llm.llm_provider() == "openai"
    monkeypatch.delenv("ENGRAM_LLM_PROVIDER")
    monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-or-xyz")
    monkeypatch.setenv("ENGRAM_LLM_BASE_URL", "https://api.anthropic.com/v1")
    assert llm.llm_provider() == "anthropic"


def test_default_model_is_opus_under_anthropic(anthropic_env, monkeypatch):
    assert llm.resolve_llm_model() == "claude-opus-5-5"
    monkeypatch.setenv("ENGRAM_LLM_EXTRACT_MODEL", "claude-haiku-5-5")
    assert llm.resolve_llm_model(task="extract") == "claude-haiku-5-5"
    assert llm.resolve_llm_model(task="audit") == "claude-opus-5-5"
    monkeypatch.setenv("ENGRAM_LLM_MODEL", "claude-sonnet-5-5")
    assert llm.resolve_llm_model() == "claude-sonnet-5-5"


# --- dispatch + SDK request shapes ------------------------------------------------


def test_generate_dispatches_with_fallbacks_for_opus(anthropic_env, monkeypatch):
    _, calls = _fake_sdk(monkeypatch, response=_Resp([_Block("thinking", "…"), _Block("text", "Hello "), _Block("text", "world")]))
    out = llm.call_ollama_generate("say hi", system="be brief", task="merge", timeout=42)
    assert out == "Hello world"
    client = next(kw for kind, kw in calls if kind == "client")
    assert client == {"api_key": "sk-ant-test-0000", "timeout": 42.0, "max_retries": 2}
    kind, kw = next(c for c in calls if c[0] in ("beta", "plain"))
    assert kind == "beta"
    assert kw["model"] == "claude-opus-5-5"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert kw["system"] == "be brief"
    assert kw["messages"] == [{"role": "user", "content": "say hi"}]
    assert "output_config" not in kw


def test_haiku_uses_plain_create_without_fallbacks(anthropic_env, monkeypatch):
    _, calls = _fake_sdk(monkeypatch)
    monkeypatch.setenv("ENGRAM_LLM_MODEL", "claude-haiku-5-5")
    assert llm.call_ollama_generate("x") == "ok"
    kind, kw = next(c for c in calls if c[0] in ("beta", "plain"))
    assert kind == "plain" and "fallbacks" not in kw


def test_older_sdk_without_fallbacks_falls_back_to_plain_create(anthropic_env, monkeypatch):
    _, calls = _fake_sdk(monkeypatch, beta_fallbacks=False)
    assert llm.call_ollama_generate("x") == "ok"
    assert [c[0] for c in calls if c[0] in ("beta", "plain")] == ["plain"]


def test_effort_env_sets_output_config(anthropic_env, monkeypatch):
    _, calls = _fake_sdk(monkeypatch)
    monkeypatch.setenv("ENGRAM_LLM_EFFORT", "low")
    llm.call_ollama_generate("x")
    _, kw = next(c for c in calls if c[0] in ("beta", "plain"))
    assert kw["output_config"] == {"effort": "low"}


def test_chat_merges_system_turns_and_keeps_roles(anthropic_env, monkeypatch):
    _, calls = _fake_sdk(monkeypatch)
    msgs = [
        {"role": "system", "content": "rule one"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "system", "content": "rule two"},
        {"role": "user", "content": "q2"},
    ]
    assert llm.call_chat_completion(msgs, max_tokens=123) == "ok"
    _, kw = next(c for c in calls if c[0] in ("beta", "plain"))
    assert kw["system"] == "rule one\n\nrule two"
    assert [m["role"] for m in kw["messages"]] == ["user", "assistant", "user"]
    assert kw["max_tokens"] == 123


def test_chat_prepends_user_turn_when_history_starts_with_assistant(anthropic_env, monkeypatch):
    _, calls = _fake_sdk(monkeypatch)
    llm.call_chat_completion([{"role": "assistant", "content": "hi"}])
    _, kw = next(c for c in calls if c[0] in ("beta", "plain"))
    assert kw["messages"][0] == {"role": "user", "content": "Continue."}


# --- degradation: never raise ----------------------------------------------------


def test_refusal_returns_none(anthropic_env, monkeypatch):
    _fake_sdk(monkeypatch, response=_Resp([], stop_reason="refusal"))
    assert llm.call_ollama_generate("x") is None


@pytest.mark.parametrize("exc_name", ["AuthenticationError", "NotFoundError", "RateLimitError", "APIStatusError", "APIConnectionError"])
def test_sdk_errors_return_none(anthropic_env, monkeypatch, exc_name):
    mod, _ = _fake_sdk(monkeypatch)
    exc_cls = getattr(mod, exc_name)
    mod.state["raise"] = exc_cls() if exc_name != "APIConnectionError" else exc_cls("down")
    assert llm.call_ollama_generate("x") is None


def test_missing_sdk_degrades_and_status_reports_it(anthropic_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises ImportError
    assert llm.call_ollama_generate("x") is None
    assert llm.is_llm_available() is False
    status = llm.get_llm_status()
    assert status["provider"] == "anthropic"
    assert status["sdk_installed"] is False
    assert status["base_url"] == "https://api.anthropic.com"
    assert status["model"] == "claude-opus-5-5"


def test_availability_probes_the_resolved_model(anthropic_env, monkeypatch):
    _, calls = _fake_sdk(monkeypatch)
    assert llm.is_llm_available() is True
    assert ("models.retrieve", "claude-opus-5-5") in calls
    monkeypatch.setenv("ENGRAM_LLM_API_KEY", "")
    monkeypatch.setenv("ENGRAM_LLM_PROVIDER", "anthropic")
    assert llm.is_llm_available() is False  # no key → never probe


def test_summarizer_routes_through_llm_layer_under_anthropic(anthropic_env, monkeypatch):
    from src import summarize

    seen: list = []
    monkeypatch.setattr(llm, "call_ollama_generate", lambda prompt, **kw: seen.append((prompt, kw)) or "sum")
    assert summarize._call_ollama("describe", "llama3.2") == "sum"
    assert seen[-1][1]["model"] is None  # an Ollama model name is not forwarded to Claude
    assert summarize._call_ollama("describe", "claude-haiku-5-5") == "sum"
    assert seen[-1][1]["model"] == "claude-haiku-5-5"
    monkeypatch.setattr(llm, "is_llm_available", lambda *a, **k: True)
    assert summarize.ollama_available() is True
