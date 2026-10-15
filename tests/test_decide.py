"""Typed decisions (hub spec `decide` interface): registry, policy, backends, log, dataset."""

from __future__ import annotations

import json
import sys
import types

import pytest

from src import decide as dm
from src import llm


@pytest.fixture
def env(test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("ENGRAM_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-ant-test")
    monkeypatch.setattr(dm, "_ROUTER", None)
    return test_db


def _haiku(monkeypatch, reply):
    """Stub the shared LLM layer the haiku backend calls (no network)."""
    seen: list = []
    monkeypatch.setattr(llm, "is_llm_available", lambda *a, **k: True)

    def fake(messages, **kw):
        seen.append((messages, kw))
        return reply(messages) if callable(reply) else reply

    monkeypatch.setattr(llm, "call_chat_completion", fake)
    return seen


def _laya(monkeypatch, answers: dict, *, routing_model="multilingual", raise_exc=None):
    calls: list = []
    mod = types.ModuleType("laya")

    class Router:
        def __init__(self, **kw):
            calls.append(("init", kw))

        def predict(self, state, questions, **kw):
            calls.append(("predict", state, questions, kw))
            if raise_exc:
                raise raise_exc
            return {"answers": answers, "routing": {"model": routing_model}}

    mod.Router = Router
    monkeypatch.setitem(sys.modules, "laya", mod)
    return calls


# --- registry + policy ---------------------------------------------------------------


def test_questions_are_versioned_and_typed():
    q = dm.get_question("worth_remembering")
    assert q.key == "worth_remembering@v1" and q.kind == "yesno"
    assert dm.get_question("route_rung").options == ("reflex", "recall", "reason")
    with pytest.raises(KeyError):
        dm.get_question("nope")


def test_policy_defaults_pin_and_reload(env):
    assert dm.backend_for(dm.get_question("worth_remembering")) == "haiku"
    path = dm.set_backend("worth_remembering@v1", "laya")
    assert json.load(open(path))["decide"]["backends"]["worth_remembering@v1"] == "laya"
    assert dm.backend_for(dm.get_question("worth_remembering")) == "laya"
    assert dm.backend_for(dm.get_question("test_passes")) == "haiku"  # untouched
    assert dm.load_policy()["decide"]["laya"]["max_latency_ms"] == 300  # defaults survive
    with pytest.raises(ValueError):
        dm.set_backend("x@v1", "gpt")


# --- haiku backend -------------------------------------------------------------------


def test_haiku_json_answer_is_logged_with_ledger_row(env, monkeypatch, tmp_path):
    seen = _haiku(monkeypatch, '{"answer": true, "confidence": 0.91}')
    d = dm.decide("worth_remembering", "Use WAL mode for concurrent SQLite readers", session_id="s1")
    assert d is not None and d.answer is True and d.confidence == 0.91
    assert d.backend == "haiku" and d.model == "claude-haiku-5-5"
    assert seen[0][1]["task"] == "decide"
    assert seen[0][0][0]["role"] == "system" and "JSON only" in seen[0][0][0]["content"]
    row = env["conn"].execute("SELECT * FROM decisions WHERE id = ?", (d.id,)).fetchone()
    assert row["question"] == "worth_remembering" and row["version"] == 1
    assert json.loads(row["answer"]) is True and row["backend"] == "haiku" and row["session_id"] == "s1"
    ledger = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert ledger[-1]["source"] == "decision" and ledger[-1]["question"] == "worth_remembering@v1"


@pytest.mark.parametrize("reply, expected", [("YES", True), ("no.", False), ('{"answer": "false"}', False)])
def test_haiku_tolerates_bare_yes_no(env, monkeypatch, reply, expected):
    _haiku(monkeypatch, reply)
    d = dm.decide("test_passes", "the response", context="Requirement: mention WAL")
    assert d is not None and d.answer is expected


def test_haiku_choice_and_unparseable(env, monkeypatch):
    _haiku(monkeypatch, "Answer: recall, because a skill matches.")
    d = dm.decide("route_rung", "deploy the thing")
    assert d is not None and d.answer == "recall"
    _haiku(monkeypatch, "maybe?")
    assert dm.decide("route_rung", "deploy the thing") is None


def test_returns_none_when_llm_unavailable(env, monkeypatch):
    monkeypatch.setattr(llm, "is_llm_available", lambda *a, **k: False)
    assert dm.decide("worth_remembering", "x") is None
    assert env["conn"].execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


# --- laya backend ----------------------------------------------------------------------


def test_laya_pinned_by_policy_reads_noul_probability(env, monkeypatch):
    dm.set_backend("worth_remembering@v1", "laya")
    calls = _laya(monkeypatch, {"worth_remembering": {"noul": 0.82, "answer_confidence": 0.82}})
    d = dm.decide("worth_remembering", "Always run migrations before restarting the service")
    assert d is not None and d.backend == "laya" and d.answer is True and d.confidence == 0.82
    assert d.model == "multilingual"
    init = next(kw for kind, kw in [c[:2] for c in calls] if kind == "init")
    assert init["default"] == "multilingual"
    predict = next(c for c in calls if c[0] == "predict")
    assert predict[2] == {"worth_remembering": {"type": "noul", "instructions": dm.get_question("worth_remembering").instructions}}
    assert predict[3]["model"] == "multilingual"


def test_laya_threshold_from_policy_and_low_confidence_flag(env, monkeypatch):
    dm.set_backend("worth_remembering@v1", "laya")
    policy = dm.load_policy()
    policy["decide"]["thresholds"]["worth_remembering@v1"] = 0.9
    dm.save_policy(policy)
    _laya(monkeypatch, {"worth_remembering": {"noul": 0.82, "low_confidence": True}})
    d = dm.decide("worth_remembering", "x")
    assert d is not None and d.answer is False and d.low_confidence is True


def test_laya_choice_question_shape_and_probabilities(env, monkeypatch):
    dm.set_backend("route_rung@v1", "laya")
    calls = _laya(monkeypatch, {"route_rung": {"choice": "recall", "probabilities": {"reflex": 0.1, "recall": 0.7, "reason": 0.2}}})
    d = dm.decide("route_rung", "fix the flaky test")
    assert d is not None and d.answer == "recall" and d.confidence == 0.7
    spec = next(c for c in calls if c[0] == "predict")[2]["route_rung"]
    assert spec["type"] == "choice" and set(spec["criteria"]) == {"reflex", "recall", "reason"}


def test_laya_failure_falls_back_to_haiku(env, monkeypatch):
    dm.set_backend("worth_remembering@v1", "laya")
    _laya(monkeypatch, {}, raise_exc=RuntimeError("no weights"))
    _haiku(monkeypatch, '{"answer": false, "confidence": 0.6}')
    d = dm.decide("worth_remembering", "x")
    assert d is not None and d.backend == "haiku" and d.answer is False


def test_laya_missing_package_uses_haiku(env, monkeypatch):
    dm.set_backend("worth_remembering@v1", "laya")
    monkeypatch.setitem(sys.modules, "laya", None)
    _haiku(monkeypatch, '{"answer": true, "confidence": 0.7}')
    d = dm.decide("worth_remembering", "x")
    assert d is not None and d.backend == "haiku"


# --- outcomes, stats, export, bench -----------------------------------------------------


def test_outcome_stats_and_export(env, monkeypatch, tmp_path):
    _haiku(monkeypatch, '{"answer": true, "confidence": 0.8}')
    d1 = dm.decide("worth_remembering", "fact one")
    d2 = dm.decide("worth_remembering", "fact two")
    assert d1 and d2
    assert dm.record_outcome(d1.id, "kept", source="test") is True
    assert dm.record_outcome(d2.id, "dismissed", source="test") is True
    assert dm.record_outcome(99999, "kept") is False
    stats = dm.decision_stats()["worth_remembering@v1"]
    assert stats["n"] == 2 and stats["by_backend"] == {"haiku": 2}
    assert stats["outcomes"] == {"kept": 1, "dismissed": 1}
    assert stats["latency_ms"]["haiku"]["n"] == 2 and stats["agreement"]["pairs"] == 0

    out = tmp_path / "ds.jsonl"
    assert dm.export_dataset("worth_remembering", str(out)) == 2
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows[0]["labels"] == {"worth_remembering": True} and rows[1]["labels"] == {"worth_remembering": False}
    assert rows[0]["questions"]["worth_remembering"]["type"] == "noul"
    assert rows[0]["meta"]["question_version"] == "worth_remembering@v1"


def test_bench_reports_agreement_and_latency_gate(env, monkeypatch):
    _haiku(monkeypatch, '{"answer": true, "confidence": 0.8}')
    for t in ("a", "b", "c"):
        dm.decide("worth_remembering", t)
    _laya(monkeypatch, {"worth_remembering": {"noul": 0.9}})
    report = dm.bench("worth_remembering", limit=10)
    assert report["inputs"] == 3 and report["laya_installed"] is True
    assert report["agreement"] == {"pairs": 3, "rate": 1.0}
    assert report["haiku"]["n"] == 3 and report["laya"]["n"] == 3
    assert report["laya_request_path_ok"] is True and report["max_latency_ms"] == 300


# --- consumers ----------------------------------------------------------------------


def test_auto_extract_screens_candidates_and_fails_open(env, monkeypatch):
    from src import auto_extract

    monkeypatch.setattr(auto_extract, "_llm_extract", lambda messages: [
        {"type": "pattern", "title": "WAL", "summary": "Use WAL mode for concurrent readers."},
        {"type": "fact", "title": "Lunch", "summary": "User had tacos today."},
    ])
    monkeypatch.setattr(auto_extract, "_regex_extract", lambda messages: [])
    monkeypatch.setattr(auto_extract, "is_llm_available", lambda: True)
    _haiku(monkeypatch, lambda messages: '{"answer": true, "confidence": 0.9}' if "WAL" in messages[-1]["content"] else '{"answer": false, "confidence": 0.95}')
    result = auto_extract.extract_from_messages([{"role": "user", "content": "x"}], screen=True)
    assert [c["title"] for c in result["candidates"]] == ["WAL"]
    assert result["screened_out"] == 1 and result["candidates"][0]["decision_id"]
    assert "Screened out 1" in auto_extract.format_auto_extract_result(result)

    monkeypatch.setattr(llm, "is_llm_available", lambda *a, **k: False)  # no decide backend
    result = auto_extract.extract_from_messages([{"role": "user", "content": "x"}], screen=True)
    assert len(result["candidates"]) == 2 and result["screened_out"] == 0


def test_validation_llm_judge_routes_through_decide(env, monkeypatch):
    from src import validation

    _haiku(monkeypatch, '{"answer": true, "confidence": 0.7}')
    assert validation._grade("we enabled WAL mode", "mentions WAL", "llm_judge") is True
    _haiku(monkeypatch, "NO")
    assert validation._grade("nothing relevant", "mentions WAL", "llm_judge") is False
    monkeypatch.setattr(llm, "is_llm_available", lambda *a, **k: False)
    assert validation._grade("mentions wal here", "mentions WAL", "llm_judge") is True  # contains fallback
