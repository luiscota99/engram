import json
from unittest.mock import MagicMock, patch

import src.token_simulation as ts
from src.token_simulation import call_llm, estimate_tokens, run_simulation

# Env vars that PROVIDERS look for; cleared per-test so run_simulation is hermetic.
PROVIDER_ENV_VARS = [
    "GROQ_API_KEY",
    "CEREBRAS_API_KEY",
    "GITHUB_TOKEN",
    "OPENROUTER_API_KEY",
    "NVIDIA_API_KEY",
    "DEEPSEEK_API_KEY",
]


def _clear_providers(monkeypatch):
    for var in PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _fake_urlopen_returning(payload: dict):
    """Build a urlopen replacement whose context manager yields payload as JSON bytes."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


# --------------------------------------------------------------------------
# estimate_tokens
# --------------------------------------------------------------------------

def test_estimate_tokens_math():
    # 8 + 12 = 20 chars -> 20 // 4 = 5, + 50 overhead = 55
    messages = [
        {"role": "system", "content": "abcdefgh"},   # 8 chars
        {"role": "user", "content": "abcdefghijkl"},  # 12 chars
    ]
    assert estimate_tokens(messages) == 55


def test_estimate_tokens_empty():
    # No content -> just the base overhead.
    assert estimate_tokens([]) == 50


# --------------------------------------------------------------------------
# call_llm
# --------------------------------------------------------------------------

def test_call_llm_success_parses_tokens_and_content(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "secret-key")
    config = {
        "env_var": "GROQ_API_KEY",
        "model": "llama-test",
        "base_url": "https://example.test/chat",
    }
    payload = {
        "usage": {"total_tokens": 123},
        "choices": [{"message": {"content": "hello world"}}],
    }
    fake = _fake_urlopen_returning(payload)
    with patch.object(ts.urllib.request, "urlopen", fake):
        tokens, content = call_llm([{"role": "user", "content": "hi"}], "Groq", config)

    assert tokens == 123
    assert content == "hello world"
    # The request carried the model + messages as a JSON POST body.
    sent_req = fake.call_args[0][0]
    body = json.loads(sent_req.data.decode("utf-8"))
    assert body["model"] == "llama-test"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["temperature"] == 0.0


def test_call_llm_openrouter_adds_referer_header(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    config = {
        "env_var": "OPENROUTER_API_KEY",
        "model": "openrouter/free",
        "base_url": "https://openrouter.test/chat",
    }
    payload = {"usage": {"total_tokens": 7}, "choices": [{"message": {"content": "x"}}]}
    fake = _fake_urlopen_returning(payload)
    with patch.object(ts.urllib.request, "urlopen", fake):
        call_llm([{"role": "user", "content": "hi"}], "OpenRouter", config)

    sent_req = fake.call_args[0][0]
    # urllib capitalizes header keys: "HTTP-Referer" -> "Http-referer".
    assert sent_req.get_header("Http-referer") == "https://github.com/engram-memory/engram"


def test_call_llm_missing_usage_defaults_to_zero(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    config = {"env_var": "GROQ_API_KEY", "model": "m", "base_url": "https://e.test/c"}
    # No "usage" and empty choices -> defaults kick in.
    fake = _fake_urlopen_returning({})
    with patch.object(ts.urllib.request, "urlopen", fake):
        tokens, content = call_llm([{"role": "user", "content": "hi"}], "Groq", config)

    assert tokens == 0
    assert content == ""


def test_call_llm_exception_returns_zero_and_empty(monkeypatch, capsys):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    config = {"env_var": "GROQ_API_KEY", "model": "m", "base_url": "https://e.test/c"}
    boom = MagicMock(side_effect=RuntimeError("network down"))
    with patch.object(ts.urllib.request, "urlopen", boom):
        tokens, content = call_llm([{"role": "user", "content": "hi"}], "Groq", config)

    assert tokens == 0
    assert content == ""
    out = capsys.readouterr().out
    assert "Error calling Groq" in out
    assert "network down" in out


# --------------------------------------------------------------------------
# run_simulation
# --------------------------------------------------------------------------

def test_run_simulation_no_providers_no_mock(monkeypatch, capsys):
    _clear_providers(monkeypatch)
    run_simulation(mock=False)
    out = capsys.readouterr().out
    assert "No provider API keys found" in out
    # Should bail before printing any scenario headers.
    assert "SCENARIO A" not in out


def test_run_simulation_mock_full_output(monkeypatch, capsys):
    _clear_providers(monkeypatch)
    run_simulation(mock=True)
    out = capsys.readouterr().out

    # Header identifies the mock provider + model.
    assert "MockProvider (gpt-mock-4)" in out
    # All four scenarios ran.
    assert "SCENARIO A: Traditional Chat" in out
    assert "SCENARIO B: Engram Workflow" in out
    assert "SCENARIO D: Engram + Caveman Protocol" in out
    assert "SCENARIO C: Long Chat + Engram Context" in out
    # Final results table + summary rendered.
    assert "=== FINAL RESULTS ===" in out
    assert "A. Traditional Chat Total Tokens:" in out
    assert "Standard Engram (B) saved" in out
    assert "Conclusion: Caveman storage" in out
    # There are 10 mock conversation turns.
    assert "Turn 10 (Trad):" in out


def test_run_simulation_mock_engram_beats_traditional(monkeypatch, capsys):
    """The mock estimator must show accumulating traditional history costing
    more than the stateless Engram workflow (positive savings)."""
    _clear_providers(monkeypatch)
    run_simulation(mock=True)
    out = capsys.readouterr().out

    # Traditional total is on its own summary line; Engram stateless on another.
    trad_line = next(ln for ln in out.splitlines() if ln.startswith("A. Traditional Chat Total Tokens:"))
    engram_line = next(ln for ln in out.splitlines() if ln.startswith("B. Stateless Engram Total Tokens:"))
    trad_total = int(trad_line.split(":")[1].strip())
    engram_total = int(engram_line.split(":")[1].strip())
    assert trad_total > engram_total > 0


def test_run_simulation_real_provider_uses_call_llm(monkeypatch, capsys):
    """With a key set and mock=False, run_simulation should select the provider
    and route every turn through call_llm."""
    _clear_providers(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "secret")

    calls = []

    def fake_call_llm(messages, provider_name, config):
        calls.append((provider_name, config["model"]))
        return 42, "canned response"

    monkeypatch.setattr(ts, "call_llm", fake_call_llm)
    run_simulation(mock=False)
    out = capsys.readouterr().out

    assert "Starting Engram Token Simulation using Groq" in out
    # 4 scenarios x 10 turns = 40 LLM calls, all to Groq.
    assert len(calls) == 40
    assert all(name == "Groq" for name, _ in calls)
    assert all(model == "llama-3.3-70b-versatile" for _, model in calls)
    # Each turn reported the canned 42-token cost; cumulative for one scenario = 420.
    assert "Cumulative: 420" in out
