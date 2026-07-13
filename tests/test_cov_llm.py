"""Coverage tests for src/llm.py and src/cli/commands/llm.py.

Library functions in src/llm.py talk to Ollama / OpenAI-compatible backends
over urllib; we mock ``urllib.request.urlopen`` at the module boundary so the
tests are hermetic. CLI commands mock the maintenance entry points they call.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from unittest import mock

import pytest

import src.llm as llm

# ── helpers ──────────────────────────────────────────────────────────

class _FakeResp:
    """Minimal urlopen context-manager stand-in."""

    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _json_resp(obj) -> _FakeResp:
    return _FakeResp(200, json.dumps(obj).encode("utf-8"))


def _capture(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


# ── resolve_llm_model ────────────────────────────────────────────────

class TestResolveModel:
    def test_explicit_model_wins(self):
        assert llm.resolve_llm_model("my-model", task="audit") == "my-model"

    def test_task_env_override(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_LLM_AUDIT_MODEL", "audit-x")
        assert llm.resolve_llm_model(task="audit") == "audit-x"

    def test_task_env_blank_falls_through_to_default(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_LLM_AUDIT_MODEL", "   ")
        monkeypatch.setenv("ENGRAM_LLM_MODEL", "default-x")
        assert llm.resolve_llm_model(task="audit") == "default-x"

    def test_unknown_task_uses_config_default(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_LLM_MODEL", "cfg-model")
        assert llm.resolve_llm_model(task="nonexistent") == "cfg-model"

    def test_no_args_uses_config_default(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_LLM_MODEL", raising=False)
        assert llm.resolve_llm_model() == "llama3.2"


# ── base url / backend detection ─────────────────────────────────────

class TestBaseUrl:
    def test_default_base_is_ollama_v1(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        assert llm.resolve_llm_base_url() == "http://localhost:11434/v1"

    def test_default_backend_is_ollama_chat(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        assert llm._is_ollama_chat_backend(llm.resolve_llm_base_url()) is True

    def test_external_backend_not_ollama(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_LLM_BASE_URL", "https://openrouter.ai/api/v1")
        assert llm._is_ollama_chat_backend(llm.resolve_llm_base_url()) is False


# ── is_llm_available ─────────────────────────────────────────────────

class TestIsAvailable:
    def _ollama_env(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)

    def test_ollama_reachable(self, monkeypatch):
        self._ollama_env(monkeypatch)
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(200)):
            assert llm.is_llm_available() is True

    def test_ollama_unreachable(self, monkeypatch):
        self._ollama_env(monkeypatch)
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            assert llm.is_llm_available() is False

    def test_openai_reachable_via_models(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_LLM_BASE_URL", "https://api.example.com/v1")
        monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-test")
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(200)) as m:
            assert llm.is_llm_available() is True
        # Authorization header carried the bearer token.
        sent_req = m.call_args[0][0]
        assert sent_req.get_header("Authorization") == "Bearer sk-test"

    def test_openai_auth_rejected_returns_false(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_LLM_BASE_URL", "https://api.example.com/v1")
        monkeypatch.setenv("ENGRAM_LLM_API_KEY", "bad-key")
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            assert llm.is_llm_available() is False

    def test_openai_all_paths_fail_returns_false(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_LLM_BASE_URL", "https://api.example.com/v1")
        monkeypatch.delenv("ENGRAM_LLM_API_KEY", raising=False)
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("x")):
            assert llm.is_llm_available() is False


# ── get_llm_status ───────────────────────────────────────────────────

class TestGetStatus:
    def test_available_enables_tasks(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_LLM_API_KEY", raising=False)
        with mock.patch.object(llm, "is_llm_available", return_value=True):
            status = llm.get_llm_status()
        assert status["available"] is True
        assert status["tasks_enabled"] == [
            "consolidation_audit",
            "gc_scoring",
            "auto_extract",
            "merge",
        ]
        assert status["api_key_set"] is False

    def test_unavailable_disables_tasks(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-abc")
        with mock.patch.object(llm, "is_llm_available", return_value=False):
            status = llm.get_llm_status()
        assert status["available"] is False
        assert status["tasks_enabled"] == []
        assert status["api_key_set"] is True


# ── call_ollama_generate ─────────────────────────────────────────────

class TestOllamaGenerate:
    def test_returns_stripped_text(self):
        with mock.patch("urllib.request.urlopen", return_value=_json_resp({"response": "  hi  "})):
            assert llm.call_ollama_generate("prompt") == "hi"

    def test_empty_response_returns_none(self):
        with mock.patch("urllib.request.urlopen", return_value=_json_resp({"response": "   "})):
            assert llm.call_ollama_generate("prompt") is None

    def test_exception_returns_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            assert llm.call_ollama_generate("prompt") is None

    def test_system_prompt_included_in_payload(self):
        captured = {}

        def _fake(req, timeout=None):
            captured["data"] = json.loads(req.data.decode())
            return _json_resp({"response": "ok"})

        with mock.patch("urllib.request.urlopen", side_effect=_fake):
            assert llm.call_ollama_generate("p", system="be terse", model="m1") == "ok"
        assert captured["data"]["system"] == "be terse"
        assert captured["data"]["model"] == "m1"
        assert captured["data"]["stream"] is False


# ── call_chat_completion ─────────────────────────────────────────────

class TestChatCompletion:
    def test_returns_content(self):
        body = {"choices": [{"message": {"content": " answer "}}]}
        with mock.patch("urllib.request.urlopen", return_value=_json_resp(body)):
            assert llm.call_chat_completion([{"role": "user", "content": "q"}]) == "answer"

    def test_no_choices_returns_none(self):
        with mock.patch("urllib.request.urlopen", return_value=_json_resp({"choices": []})):
            assert llm.call_chat_completion([]) is None

    def test_blank_content_returns_none(self):
        body = {"choices": [{"message": {"content": "  "}}]}
        with mock.patch("urllib.request.urlopen", return_value=_json_resp(body)):
            assert llm.call_chat_completion([]) is None

    def test_exception_returns_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("net")):
            assert llm.call_chat_completion([]) is None

    def test_api_key_sets_auth_header(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_LLM_API_KEY", "sk-xyz")
        captured = {}

        def _fake(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return _json_resp({"choices": [{"message": {"content": "z"}}]})

        with mock.patch("urllib.request.urlopen", side_effect=_fake):
            assert llm.call_chat_completion([]) == "z"
        assert captured["auth"] == "Bearer sk-xyz"


# ── parse_json_from_llm ──────────────────────────────────────────────

class TestParseJson:
    def test_empty_returns_none(self):
        assert llm.parse_json_from_llm("") is None
        assert llm.parse_json_from_llm("   ") is None

    def test_plain_json(self):
        assert llm.parse_json_from_llm('{"a": 1}') == {"a": 1}

    def test_fenced_json_block(self):
        raw = '```json\n{"x": [1, 2]}\n```'
        assert llm.parse_json_from_llm(raw) == {"x": [1, 2]}

    def test_fenced_plain_block(self):
        raw = "```\n[1, 2, 3]\n```"
        assert llm.parse_json_from_llm(raw) == [1, 2, 3]

    def test_invalid_json_returns_none(self):
        assert llm.parse_json_from_llm("not json at all") is None


# ── CLI: cmd_llm_status ──────────────────────────────────────────────

_STATUS = {
    "base_url": "http://localhost:11434/v1",
    "model": "llama3.2",
    "audit_model": "audit-m",
    "extract_model": "extract-m",
    "available": True,
    "tasks_enabled": ["consolidation_audit", "merge"],
    "api_key_set": True,
}


class TestCmdLlmStatus:
    def test_reachable_lists_tasks(self):
        import src.cli.commands.llm as cli

        with mock.patch.object(cli, "get_llm_status", return_value=dict(_STATUS)), \
             mock.patch.object(cli, "is_llm_available", return_value=True):
            out = _capture(cli.cmd_llm_status, object())
        assert "http://localhost:11434/v1" in out
        assert "audit-m" in out
        assert "extract-m" in out
        assert "API key:" in out and "set" in out
        assert "reachable" in out
        assert "consolidation_audit, merge" in out

    def test_unreachable_shows_fallback_note(self):
        import src.cli.commands.llm as cli

        st = dict(_STATUS)
        st["api_key_set"] = False
        with mock.patch.object(cli, "get_llm_status", return_value=st), \
             mock.patch.object(cli, "is_llm_available", return_value=False):
            out = _capture(cli.cmd_llm_status, object())
        assert "not set" in out
        assert "not reachable" in out
        assert "regex/fallback" in out


# ── CLI: cmd_llm_audit ───────────────────────────────────────────────

class _AuditArgs:
    def __init__(self, execute=False, threshold=0.8, force_rescan=False):
        self.execute = execute
        self.threshold = threshold
        self.force_rescan = force_rescan


class TestCmdLlmAudit:
    def test_unchanged_skip(self):
        import src.cli.commands.llm as cli

        with mock.patch.object(cli, "run_llm_consolidation_audit",
                               return_value={"skip_reason": "unchanged"}):
            out = _capture(cli.cmd_llm_audit, _AuditArgs())
        assert "No changes since last consolidation scan" in out
        assert "--force-rescan" in out

    def test_fallback_lists_clusters(self):
        import src.cli.commands.llm as cli

        report = {
            "clusters_found": 1,
            "llm_available": False,
            "fallback": "LLM unavailable; showing clusters",
            "clusters": [{
                "item_type": "mistake",
                "avg_similarity": 0.91,
                "cluster_size": 2,
                "items": [
                    {"item_id": 5, "title": "First"},
                    {"item_id": 6, "title": "Second"},
                ],
            }],
        }
        with mock.patch.object(cli, "run_llm_consolidation_audit", return_value=report):
            out = _capture(cli.cmd_llm_audit, _AuditArgs())
        assert "Clusters found: 1" in out
        assert "showing clusters" in out
        assert "ID:5" in out and "First" in out
        assert "ID:6" in out and "Second" in out

    def test_no_decisions(self):
        import src.cli.commands.llm as cli

        report = {"clusters_found": 0, "llm_available": True, "decisions": []}
        with mock.patch.object(cli, "run_llm_consolidation_audit", return_value=report):
            out = _capture(cli.cmd_llm_audit, _AuditArgs())
        assert "No LLM decisions returned" in out

    def test_dry_run_prints_decisions(self):
        import src.cli.commands.llm as cli

        report = {
            "clusters_found": 1,
            "llm_available": True,
            "decisions": [
                {"decision": "auto_merge", "item_type": "skill", "ids": [1, 2],
                 "reason": "duplicates"},
            ],
        }
        with mock.patch.object(cli, "run_llm_consolidation_audit", return_value=report):
            out = _capture(cli.cmd_llm_audit, _AuditArgs(execute=False))
        assert "Decisions (1)" in out
        assert "[AUTO_MERGE]" in out
        assert "IDs: 1, 2" in out
        assert "duplicates" in out
        assert "Re-run with --execute" in out

    def test_blocked_report(self):
        import src.cli.commands.llm as cli

        report = {
            "clusters_found": 1,
            "llm_available": True,
            "decisions": [{"decision": "auto_merge", "item_type": "skill", "ids": [1]}],
            "blocked": True,
            "reason": "too risky",
        }
        with mock.patch.object(cli, "run_llm_consolidation_audit", return_value=report):
            out = _capture(cli.cmd_llm_audit, _AuditArgs())
        assert "Audit blocked" in out
        assert "too risky" in out

    def test_execute_reports_applied(self):
        import src.cli.commands.llm as cli

        report = {
            "clusters_found": 1,
            "llm_available": True,
            "decisions": [{"decision": "auto_merge", "item_type": "skill", "ids": [1, 2]}],
            "applied": [
                {"applied": True, "item_type": "skill", "merged_id": 9,
                 "archived_ids": [1, 2]},
                {"applied": False, "reason": "conflict"},
            ],
        }
        with mock.patch.object(cli, "run_llm_consolidation_audit", return_value=report):
            out = _capture(cli.cmd_llm_audit, _AuditArgs(execute=True))
        assert "Applied 1 auto_merge operation(s)" in out
        assert "new ID 9" in out
        assert "Skipped: conflict" in out


# ── CLI: cmd_llm_gc ──────────────────────────────────────────────────

class _GcArgs:
    def __init__(self, archive=False, days=30):
        self.archive = archive
        self.days = days


class TestCmdLlmGc:
    def test_scored_output(self):
        import src.cli.commands.llm as cli

        report = {
            "candidates": [{"item_id": 1}, {"item_id": 2}],
            "llm_available": True,
            "scored": [
                {"item_type": "mistake", "item_id": 1, "decision": "discard",
                 "reason": "obsolete"},
                {"item_type": "skill", "item_id": 2, "decision": "keep",
                 "reason": "still used"},
            ],
            "to_discard": [{"item_type": "mistake", "item_id": 1}],
        }
        with mock.patch.object(cli, "run_llm_gc", return_value=report):
            out = _capture(cli.cmd_llm_gc, _GcArgs())
        assert "Candidates:   2" in out
        assert "LLM scored 2 item(s), 1 marked discard" in out
        assert "[DISCARD]" in out and "obsolete" in out
        assert "[keep]" in out and "still used" in out
        assert "Re-run with --archive" in out

    def test_fallback_lists_candidates(self):
        import src.cli.commands.llm as cli

        report = {
            "candidates": [{"item_id": 1}],
            "llm_available": False,
            "fallback": "LLM unavailable",
            "scored": [],
            "to_discard": [{"item_type": "pattern", "item_id": 7}],
        }
        with mock.patch.object(cli, "run_llm_gc", return_value=report):
            out = _capture(cli.cmd_llm_gc, _GcArgs())
        assert "LLM unavailable" in out
        assert "1 candidate(s) for archive" in out
        assert "ID:7" in out

    def test_blocked(self):
        import src.cli.commands.llm as cli

        report = {"candidates": [], "llm_available": True, "blocked": True,
                  "reason": "gc off"}
        with mock.patch.object(cli, "run_llm_gc", return_value=report):
            out = _capture(cli.cmd_llm_gc, _GcArgs())
        assert "GC blocked" in out
        assert "gc off" in out

    def test_archive_reports_processed(self):
        import src.cli.commands.llm as cli

        report = {"candidates": [], "llm_available": True, "processed": 4}
        with mock.patch.object(cli, "run_llm_gc", return_value=report):
            out = _capture(cli.cmd_llm_gc, _GcArgs(archive=True))
        assert "Archived 4 item(s)" in out


# ── CLI: cmd_llm dispatch ────────────────────────────────────────────

class TestCmdLlmDispatch:
    @pytest.mark.parametrize("sub,target", [
        ("status", "cmd_llm_status"),
        ("audit", "cmd_llm_audit"),
        ("gc", "cmd_llm_gc"),
    ])
    def test_dispatch_routes(self, sub, target):
        import src.cli.commands.llm as cli

        args = mock.Mock(llm_command=sub)
        with mock.patch.object(cli, target) as m:
            cli.cmd_llm(args)
        m.assert_called_once_with(args)

    def test_unknown_prints_usage(self):
        import src.cli.commands.llm as cli

        args = mock.Mock(llm_command="bogus")
        out = _capture(cli.cmd_llm, args)
        assert "Usage: engram llm {status|audit|gc}" in out
