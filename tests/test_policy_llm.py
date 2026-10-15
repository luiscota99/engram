"""Policy layer + cost ledger: job routing, privacy refusals, fallback chains, spend reporting."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import llm
from src import policy as pol
from src.search_audit import append_llm_call_audit, summarize_audit_log


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    for k in (
        "ENGRAM_LLM_PROVIDER", "ENGRAM_LLM_API_KEY", "ENGRAM_LLM_BASE_URL", "ENGRAM_LLM_MODEL",
        "ENGRAM_LLM_EXTRACT_MODEL", "ENGRAM_LLM_AUDIT_MODEL", "ENGRAM_LLM_DECIDE_MODEL", "OLLAMA_HOST",
    ):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _ledger(tmp_path):
    p = tmp_path / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# --- policy file -----------------------------------------------------------------------


def test_defaults_merge_set_value_and_prices(env):
    p = pol.load_policy()
    assert p["generate"]["default"] == {"privacy": "personal", "mode": "realtime", "fallback": []}
    assert p["decide"]["default_backend"] == "haiku"
    path = pol.set_value("generate.jobs.extract.model", "claude-haiku-5-5")
    pol.set_value("generate.prices_usd_per_mtok.my-model", [1.0, 2.0])
    on_disk = json.load(open(path))
    assert on_disk["generate"]["jobs"]["extract"]["model"] == "claude-haiku-5-5"
    p = pol.load_policy()
    assert p["generate"]["jobs"]["audit"] == {}  # untouched defaults survive a partial file
    assert pol.prices(p)["my-model"] == [1.0, 2.0] and "claude-opus-5-5" in pol.prices(p)
    with pytest.raises(ValueError):
        pol.set_value(".bad", 1)


def test_estimate_cost():
    assert pol.estimate_cost_usd("claude-haiku-5-5", {"input_tokens": 1000, "output_tokens": 500}) == 0.00035
    assert pol.estimate_cost_usd("claude-opus-5-5", {"input_tokens": 1_000_000}) == 4.0
    assert pol.estimate_cost_usd("llama3.2", {"input_tokens": 10}) is None
    assert pol.estimate_cost_usd("claude-opus-5-5", None) is None
    cached = pol.estimate_cost_usd("claude-opus-5-5", {"cache_read_input_tokens": 1_000_000})
    assert cached == 0.4  # 10% of the input price


# --- routing plans ---------------------------------------------------------------------


def test_default_plan_single_local_provider_no_fallback(env):
    plan = llm.resolve_job("extract")
    assert plan["providers"] == ["openai"] and plan["refused"] == []  # Ollama's /v1 on localhost
    assert plan["mode"] == "realtime" and plan["privacy"] == "personal"


def test_remote_openai_endpoint_refused_for_personal_until_cleared(env, monkeypatch):
    monkeypatch.setenv("ENGRAM_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    plan = llm.resolve_job("extract")
    assert plan["providers"] == [] and plan["refused"][0][0] == "openai"
    assert llm.call_chat_completion([{"role": "user", "content": "x"}], task="extract") is None
    rows = _ledger(env)
    assert rows[-1]["source"] == "llm_call" and rows[-1]["outcome"].startswith("refused")
    pol.set_value("generate.jobs.extract.privacy", "public")
    assert llm.resolve_job("extract")["providers"] == ["openai"]
    pol.set_value("generate.jobs.extract.privacy", "personal")
    pol.set_value("generate.providers.openai.privacy", ["personal", "public"])
    assert llm.resolve_job("extract")["providers"] == ["openai"]


def test_fallback_chain_logs_every_attempt_with_cost(env, monkeypatch):
    monkeypatch.setenv("ENGRAM_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-ant-x")
    pol.set_value("generate.jobs.extract.fallback", ["ollama"])
    seen: list = []
    monkeypatch.setattr(llm, "_anthropic_chat", lambda messages, model, max_tokens, timeout: (seen.append(("anthropic", model)) or (None, None)))

    def fake_openai(messages, model, *, base_url, api_key, temperature, max_tokens, timeout):
        seen.append(("openai-compatible", model, base_url, api_key))
        return "ok", {"input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(llm, "_openai_chat", fake_openai)
    assert llm.call_chat_completion([{"role": "user", "content": "x"}], task="extract") == "ok"
    assert seen[0] == ("anthropic", "claude-opus-5-5")
    assert seen[1][1] == "llama3.2" and seen[1][2].endswith("/v1") and seen[1][3] == ""  # Ollama, not Claude
    rows = [r for r in _ledger(env) if r["source"] == "llm_call"]
    assert [(r["provider"], r["outcome"]) for r in rows] == [("anthropic", "unavailable"), ("ollama", "ok")]
    assert rows[1]["cost_usd"] == 0.0 and rows[1]["input_tokens"] == 10


def test_cost_recorded_for_anthropic_usage_and_summarized(env, monkeypatch):
    monkeypatch.setenv("ENGRAM_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-ant-x")
    monkeypatch.setattr(llm, "_anthropic_chat", lambda m, model, mt, t: ("yes", {"input_tokens": 1000, "output_tokens": 500}))
    assert llm.call_chat_completion([{"role": "user", "content": "x"}], task="decide") == "yes"
    row = _ledger(env)[-1]
    assert row["job"] == "decide" and row["model"] == "claude-haiku-5-5" and row["cost_usd"] == 0.00035
    s = summarize_audit_log()["llm"]
    assert s["calls"] == 1 and s["ok"] == 1 and s["cost_usd"] == 0.00035
    assert s["by_job"]["decide"]["input_tokens"] == 1000 and "p50_ms" in s["by_job"]["decide"]
    assert s["by_model"]["claude-haiku-5-5"]["calls"] == 1


def test_model_precedence_and_fit(env, monkeypatch):
    monkeypatch.setenv("ENGRAM_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-ant-x")
    assert llm.resolve_llm_model(task="extract") == "claude-opus-5-5"
    pol.set_value("generate.jobs.extract.model", "claude-sonnet-5-5")
    assert llm.resolve_llm_model(task="extract") == "claude-sonnet-5-5"
    monkeypatch.setenv("ENGRAM_LLM_EXTRACT_MODEL", "claude-haiku-5-5")
    assert llm.resolve_llm_model(task="extract") == "claude-haiku-5-5"  # env override wins
    assert llm.resolve_llm_model("explicit-model", task="extract") == "explicit-model"
    # a Claude name cannot run on Ollama: a fallback hop gets the provider default
    assert llm.resolve_llm_model(task="extract", provider="ollama") == "llama3.2"


def test_status_exposes_resolved_policy(env, monkeypatch):
    monkeypatch.setattr(llm, "is_llm_available", lambda *a, **k: False)
    status = llm.get_llm_status()
    jobs = status["policy"]["jobs"]
    assert jobs["extract"]["providers"] == ["openai"] and jobs["extract"]["privacy"] == "personal"
    assert status["policy"]["path"].endswith("policy.json")


def test_roi_prints_spend_section(env, test_db, monkeypatch, capsys):
    append_llm_call_audit(job="extract", provider="anthropic", model="claude-haiku-5-5", mode="realtime",
                          usage={"input_tokens": 2000, "output_tokens": 100}, latency_ms=420.0, outcome="ok", cost_usd=0.00025)
    append_llm_call_audit(job="audit", provider="openai", model="", mode="realtime", usage=None,
                          latency_ms=0.0, outcome="refused: personal content is not cleared for openai", cost_usd=None)
    from src.cli.commands.maintenance import cmd_roi

    cmd_roi(SimpleNamespace())
    out = capsys.readouterr().out
    assert "LLM spend" in out and "refused by privacy policy 1" in out and "extract" in out


def test_cli_policy_set_show_path(env, capsys):
    from src.cli.commands.policy import cmd_policy_path, cmd_policy_set, cmd_policy_show

    cmd_policy_set(SimpleNamespace(key="generate.jobs.summarize.model", value="claude-haiku-5-5"))
    cmd_policy_set(SimpleNamespace(key="generate.providers.openai.privacy", value='["personal","public"]'))
    cmd_policy_show(SimpleNamespace(jobs=False))
    cmd_policy_path(SimpleNamespace())
    out = capsys.readouterr().out
    assert '"model": "claude-haiku-5-5"' in out and '"personal"' in out
    assert pol.load_policy()["generate"]["providers"]["openai"]["privacy"] == ["personal", "public"]
