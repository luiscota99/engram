"""Coverage tests for src/benchmark.py — LLM provider benchmark harness.

No DB is involved; all network I/O (urllib) is mocked at the module boundary
so tests are hermetic and fast.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager
from unittest import mock

import pytest

from src import benchmark

# ── Helpers ──────────────────────────────────────────────────────────

class _FakeResponse:
    """Minimal context-manager stand-in for urllib's HTTPResponse."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _fake_urlopen(payload: dict, captured: dict | None = None):
    """Return a urlopen replacement that yields *payload* and records the request."""

    def _open(req, timeout=None):
        if captured is not None:
            captured["req"] = req
            captured["timeout"] = timeout
        return _FakeResponse(payload)

    return _open


@contextmanager
def _capture_stdout():
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


CONFIG = {
    "env_var": "GROQ_API_KEY",
    "base_url": "https://example.test/v1/chat/completions",
    "model": "test-model",
}
MESSAGES = [{"role": "user", "content": "hi"}]


# ── make_request ─────────────────────────────────────────────────────

def test_make_request_missing_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    result = benchmark.make_request("Groq", CONFIG, MESSAGES)
    assert result == {"error": "Missing GROQ_API_KEY"}


def test_make_request_success_parses_usage_and_content(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "secret-token")
    payload = {
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        "choices": [{"message": {"content": "the answer"}}],
    }
    captured = {}
    with mock.patch.object(
        benchmark.urllib.request, "urlopen", _fake_urlopen(payload, captured)
    ):
        result = benchmark.make_request("Groq", CONFIG, MESSAGES)

    assert result["content"] == "the answer"
    assert result["prompt_tokens"] == 11
    assert result["completion_tokens"] == 7
    assert result["total_tokens"] == 18
    assert result["latency"] >= 0
    assert "error" not in result

    # The request was built with the auth header and JSON body.
    req = captured["req"]
    assert req.get_header("Authorization") == "Bearer secret-token"
    assert req.method == "POST"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "test-model"
    assert body["temperature"] == 0.0
    assert body["messages"] == MESSAGES
    assert captured["timeout"] == 30


def test_make_request_defaults_when_fields_absent(monkeypatch):
    """Missing usage/choices should fall back to zeros and empty content."""
    monkeypatch.setenv("GROQ_API_KEY", "secret-token")
    with mock.patch.object(
        benchmark.urllib.request, "urlopen", _fake_urlopen({})
    ):
        result = benchmark.make_request("Groq", CONFIG, MESSAGES)

    assert result["content"] == ""
    assert result["prompt_tokens"] == 0
    assert result["completion_tokens"] == 0
    assert result["total_tokens"] == 0


def test_make_request_openrouter_extra_headers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-token")
    conf = {
        "env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.test/api",
        "model": "openrouter/free",
    }
    captured = {}
    with mock.patch.object(
        benchmark.urllib.request,
        "urlopen",
        _fake_urlopen({"choices": [{"message": {"content": "ok"}}]}, captured),
    ):
        result = benchmark.make_request("OpenRouter", conf, MESSAGES)

    assert result["content"] == "ok"
    req = captured["req"]
    # urllib normalizes header names to Title-Case.
    assert req.get_header("Http-referer") == "https://github.com/engram-memory/engram"
    assert req.get_header("X-title") == "Engram Benchmark"


def test_make_request_exception_returns_error(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "secret-token")

    def _boom(req, timeout=None):
        raise ConnectionError("network down")

    with mock.patch.object(benchmark.urllib.request, "urlopen", _boom):
        result = benchmark.make_request("Groq", CONFIG, MESSAGES)

    assert result["error"] == "network down"
    assert result["latency"] >= 0


# ── run_benchmark ────────────────────────────────────────────────────

def test_run_benchmark_no_providers(monkeypatch):
    for conf in benchmark.PROVIDERS.values():
        monkeypatch.delenv(conf["env_var"], raising=False)

    with _capture_stdout() as buf:
        benchmark.run_benchmark()

    out = buf.getvalue()
    assert "No provider API keys found in environment." in out
    # Every provider's env var is listed as a hint.
    for name, conf in benchmark.PROVIDERS.items():
        assert conf["env_var"] in out
        assert name in out


def test_run_benchmark_success_table(monkeypatch):
    # Only Groq active.
    for conf in benchmark.PROVIDERS.values():
        monkeypatch.delenv(conf["env_var"], raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "token")

    fake_result = {
        "latency": 1.2345,
        "content": "x",
        "prompt_tokens": 5,
        "completion_tokens": 5,
        "total_tokens": 42,
    }
    with mock.patch.object(benchmark, "make_request", return_value=fake_result):
        with _capture_stdout() as buf:
            benchmark.run_benchmark()

    out = buf.getvalue()
    assert "1 active provider(s): Groq" in out
    assert "### Benchmark Results" in out
    assert "| Provider | Model | Task | Latency (s) | Tokens | Status |" in out
    # One data row per (task x provider): 2 tasks -> 2 rows.
    assert out.count("| Groq | llama-3.3-70b-versatile |") == len(benchmark.TEST_TASKS)
    assert "1.23s" in out
    assert "42" in out
    assert "✅ OK" in out
    assert "Done." in out


def test_run_benchmark_failure_table(monkeypatch):
    for conf in benchmark.PROVIDERS.values():
        monkeypatch.delenv(conf["env_var"], raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "token")

    # No latency / tokens -> exercises the "N/A" formatting branches, and
    # the error is surfaced with the ❌ marker.
    err_result = {"error": "boom"}
    with mock.patch.object(benchmark, "make_request", return_value=err_result):
        with _capture_stdout() as buf:
            benchmark.run_benchmark()

    out = buf.getvalue()
    assert "[FAILED: boom]" in out
    assert "❌ boom" in out
    assert "| N/A | N/A |" in out  # latency N/A and tokens N/A adjacent


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
