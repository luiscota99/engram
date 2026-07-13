"""Coverage tests for capture, workflow, config, temporal, search_audit.

Every test asserts concrete behavior: return values, DB side effects,
formatted-output content, or error strings.
"""
from __future__ import annotations

import json

import pytest

from src import config, search_audit
from src.capture import (
    SESSION_INFLUENCE_PROMPT,
    format_capture_suggestion,
    suggest_capture,
)
from src.database import get_connection, index_in_fts
from src.temporal import invalidate_memory
from src.workflow import (
    WorkflowViolationError,
    advance_phase,
    check_decision_allowed,
    get_session_state,
    init_session_state,
    record_role_contribution,
)

# ─────────────────────────── config.py ───────────────────────────

def test_llm_base_url_defaults_to_ollama_v1(monkeypatch):
    monkeypatch.delenv("ENGRAM_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert config.llm_base_url() == "http://localhost:11434/v1"


def test_llm_base_url_explicit_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("ENGRAM_LLM_BASE_URL", "https://api.example.com/v1/")
    assert config.llm_base_url() == "https://api.example.com/v1"


def test_llm_model_and_api_key(monkeypatch):
    monkeypatch.setenv("ENGRAM_LLM_MODEL", "gpt-mini")
    monkeypatch.setenv("ENGRAM_LLM_API_KEY", "  sk-abc  ")
    assert config.llm_model() == "gpt-mini"
    assert config.llm_api_key() == "sk-abc"


def test_llm_model_default(monkeypatch):
    monkeypatch.delenv("ENGRAM_LLM_MODEL", raising=False)
    assert config.llm_model() == "llama3.2"


def test_session_help_log_path_expands_user(monkeypatch):
    monkeypatch.setenv("ENGRAM_SESSION_HELP_LOG", "~/foo/help.jsonl")
    path = config.session_help_log_path()
    assert path.endswith("/foo/help.jsonl")
    assert "~" not in path


def test_max_context_chars_valid_int(monkeypatch):
    monkeypatch.setenv("ENGRAM_MAX_CONTEXT_CHARS", "1234")
    assert config.max_context_chars() == 1234


def test_max_context_chars_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("ENGRAM_MAX_CONTEXT_CHARS", "not-a-number")
    assert config.max_context_chars() == config.DEFAULT_MAX_CONTEXT_CHARS


def test_max_context_chars_unset_default(monkeypatch):
    monkeypatch.delenv("ENGRAM_MAX_CONTEXT_CHARS", raising=False)
    assert config.max_context_chars() == 8000


def test_embed_max_chars_variants(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_MAX_CHARS", "500")
    assert config.embed_max_chars() == 500
    monkeypatch.setenv("ENGRAM_EMBED_MAX_CHARS", "bogus")
    assert config.embed_max_chars() is None
    monkeypatch.delenv("ENGRAM_EMBED_MAX_CHARS", raising=False)
    assert config.embed_max_chars() is None


def test_claw_path(monkeypatch):
    monkeypatch.setenv("CLAW_PATH", "/usr/bin/claw")
    assert config.claw_path() == "/usr/bin/claw"
    monkeypatch.delenv("CLAW_PATH", raising=False)
    assert config.claw_path() is None


# ─────────────────────────── search_audit.py ───────────────────────────

def test_append_search_audit_noop_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    # Should be a silent no-op; nothing written anywhere.
    search_audit.append_search_audit(
        query="q", results=[], semantic_status="ok", source="cli",
        item_type=None, tags=None, limit=5, project_path=None,
    )
    assert list(tmp_path.iterdir()) == []


def test_append_search_audit_writes_jsonl(monkeypatch, tmp_path):
    log_path = tmp_path / "nested" / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log_path))
    results = [
        {"item_type": "mistake", "item_id": 1, "title": "T" * 200},
        {"item_type": "skill", "item_id": 2, "title": "second"},
        {"item_type": "pattern", "item_id": 3, "title": "third"},
        {"item_type": "pattern", "item_id": 4, "title": "fourth"},
        {"item_type": "pattern", "item_id": 5, "title": "fifth"},
        {"item_type": "pattern", "item_id": 6, "title": "sixth-should-be-truncated"},
    ]
    search_audit.append_search_audit(
        query="how to fix", results=results, semantic_status="ready",
        source="mcp", item_type="mistake", tags=["a", "b"], limit=5,
        project_path="/repo",
    )
    assert log_path.exists()
    line = json.loads(log_path.read_text().strip())
    assert line["source"] == "mcp"
    assert line["query"] == "how to fix"
    assert line["semantic_status"] == "ready"
    assert line["item_type_filter"] == "mistake"
    assert line["tags_filter"] == ["a", "b"]
    assert line["result_count"] == 6
    # top_k capped at 5
    assert len(line["top_k"]) == 5
    # title truncated to 120 chars
    assert len(line["top_k"][0]["title"]) == 120


def test_append_search_audit_truncates_long_query(monkeypatch, tmp_path):
    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log_path))
    search_audit.append_search_audit(
        query="x" * 900, results=[], semantic_status="off", source="cli",
        item_type=None, tags=None, limit=10, project_path=None,
    )
    line = json.loads(log_path.read_text().strip())
    assert len(line["query"]) == 500


def test_append_search_audit_swallows_write_errors(monkeypatch):
    # Point at a path whose parent cannot be created (a file, not a dir).
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", "/dev/null/cannot/audit.jsonl")
    # Must not raise despite the OSError.
    search_audit.append_search_audit(
        query="q", results=[], semantic_status="ok", source="cli",
        item_type=None, tags=None, limit=1, project_path=None,
    )


# ─────────────────────────── temporal.py ───────────────────────────

def test_invalidate_unknown_type_returns_false(test_db):
    assert invalidate_memory("banana", 1, db_path=test_db["path"]) is False


def test_invalidate_missing_row_returns_false(test_db):
    assert invalidate_memory("mistake", 9999, db_path=test_db["path"]) is False


def _seed_mistake(path, mistake="original mistake"):
    with get_connection(path) as conn:
        cur = conn.execute(
            "INSERT INTO mistakes (date, context, mistake, fix) VALUES (?, ?, ?, ?)",
            ("2026-01-01", "ctx", mistake, "the fix"),
        )
        return cur.lastrowid


def test_invalidate_mistake_supersedes_and_prefixes_title(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "off")
    path = test_db["path"]
    mid = _seed_mistake(path)
    with get_connection(path) as conn:
        index_in_fts(conn, "mistake", mid, "original mistake", "ctx body", ["tag1"])

    ok = invalidate_memory(
        "mistake", mid, superseded_by=42, reason="outdated advice", db_path=path
    )
    assert ok is True
    with get_connection(path) as conn:
        row = conn.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
        assert row["superseded_by"] == 42
        assert row["mistake"].startswith("[SUPERSEDED] original mistake")
        # memory_facts invalidation row written
        fact = conn.execute(
            "SELECT * FROM memory_facts WHERE subject = ? AND predicate = 'invalidated'",
            (f"mistake:{mid}",),
        ).fetchone()
        assert fact is not None
        assert fact["object"] == "outdated advice"
        assert fact["source_type"] == "mistake"


def test_invalidate_skips_reprefix_when_already_superseded(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "off")
    path = test_db["path"]
    mid = _seed_mistake(path, mistake="[SUPERSEDED] already gone")
    ok = invalidate_memory("mistake", mid, db_path=path)
    assert ok is True
    with get_connection(path) as conn:
        row = conn.execute("SELECT mistake FROM mistakes WHERE id = ?", (mid,)).fetchone()
        # No double prefix
        assert row["mistake"] == "[SUPERSEDED] already gone"


def test_invalidate_pattern_uses_name_column(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "off")
    path = test_db["path"]
    with get_connection(path) as conn:
        cur = conn.execute(
            "INSERT INTO patterns (name, symptoms, root_cause, standard_fix) "
            "VALUES (?, ?, ?, ?)",
            ("Flaky Test Pattern", "sym", "rc", "fix"),
        )
        pid = cur.lastrowid
    ok = invalidate_memory("pattern", pid, db_path=path)
    assert ok is True
    with get_connection(path) as conn:
        row = conn.execute("SELECT name FROM patterns WHERE id = ?", (pid,)).fetchone()
        assert row["name"].startswith("[SUPERSEDED] Flaky Test Pattern")


def test_invalidate_no_reason_writes_no_fact(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "off")
    path = test_db["path"]
    mid = _seed_mistake(path)
    invalidate_memory("mistake", mid, db_path=path)
    with get_connection(path) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM memory_facts WHERE predicate = 'invalidated'"
        ).fetchone()["c"]
        assert cnt == 0


# ─────────────────────────── workflow.py ───────────────────────────

def _seed_session(path, session_id, workflow_used=None):
    with get_connection(path) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, title, date, domain, workflow_used) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, "t", "2026-01-01", "eng", workflow_used),
        )


def test_init_session_state_defaults(test_db):
    path = test_db["path"]
    _seed_session(path, "s1")
    state = init_session_state("s1", db_path=path)
    assert state["current_phase"] == "analysis"
    assert state["required_roles"] == ["Analyst"]
    assert state["completed_roles"] == []
    assert state["can_proceed"] is False
    assert state["missing_roles"] == ["Analyst"]


def test_get_session_state_unknown_session(test_db):
    state = get_session_state("nope", db_path=test_db["path"])
    assert state["current_phase"] is None
    assert state["can_proceed"] is False
    assert state["missing_roles"] == []


def test_init_session_state_custom_workflow(test_db):
    path = test_db["path"]
    with get_connection(path) as conn:
        conn.execute(
            "INSERT INTO workflows (name, description, steps, phases, phase_requirements) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "duo", "d", "s",
                json.dumps(["draft", "ship"]),
                json.dumps({"draft": ["Writer"], "ship": ["Releaser"]}),
            ),
        )
    _seed_session(path, "s-custom", "duo")
    state = init_session_state("s-custom", workflow_name="duo", db_path=path)
    assert state["current_phase"] == "draft"
    assert state["required_roles"] == ["Writer"]


def test_init_session_state_bad_json_falls_back(test_db):
    path = test_db["path"]
    with get_connection(path) as conn:
        conn.execute(
            "INSERT INTO workflows (name, description, steps, phases, phase_requirements) "
            "VALUES (?, ?, ?, ?, ?)",
            ("broken", "d", "s", "{not json", "also-broken"),
        )
    _seed_session(path, "s-bad", "broken")
    state = init_session_state("s-bad", workflow_name="broken", db_path=path)
    # Falls back to defaults
    assert state["current_phase"] == "analysis"
    assert state["required_roles"] == ["Analyst"]


def test_init_session_state_unknown_workflow_name(test_db):
    _seed_session(test_db["path"], "s-unknown-wf")
    state = init_session_state("s-unknown-wf", workflow_name="ghost", db_path=test_db["path"])
    assert state["current_phase"] == "analysis"


def test_record_role_contribution_unknown_session(test_db):
    state = record_role_contribution("ghost", "Analyst", db_path=test_db["path"])
    assert state["current_phase"] is None


def test_record_role_contribution_completes_phase(test_db):
    path = test_db["path"]
    _seed_session(path, "s2")
    init_session_state("s2", db_path=path)
    state = record_role_contribution("s2", "Analyst", db_path=path)
    assert state["completed_roles"] == ["Analyst"]
    assert state["missing_roles"] == []
    assert state["can_proceed"] is True
    # Idempotent — recording same role again does not duplicate.
    state2 = record_role_contribution("s2", "Analyst", db_path=path)
    assert state2["completed_roles"] == ["Analyst"]


def test_advance_phase_no_state_raises(test_db):
    with pytest.raises(WorkflowViolationError, match="no workflow state"):
        advance_phase("never-init", db_path=test_db["path"])


def test_advance_phase_missing_roles_raises(test_db):
    path = test_db["path"]
    _seed_session(path, "s3")
    init_session_state("s3", db_path=path)
    with pytest.raises(WorkflowViolationError, match="roles still required: Analyst"):
        advance_phase("s3", db_path=path)


def test_advance_phase_moves_to_next_phase(test_db):
    path = test_db["path"]
    _seed_session(path, "s4")
    init_session_state("s4", db_path=path)
    record_role_contribution("s4", "Analyst", db_path=path)
    state = advance_phase("s4", db_path=path)
    assert state["current_phase"] == "research"
    assert state["required_roles"] == ["Researcher"]
    assert state["completed_roles"] == []
    assert state["can_proceed"] is False


def test_advance_phase_at_final_phase(test_db):
    path = test_db["path"]
    # Create a session with a single-phase custom workflow so we hit the final-phase branch.
    with get_connection(path) as conn:
        conn.execute(
            "INSERT INTO workflows (name, description, steps, phases, phase_requirements) "
            "VALUES (?, ?, ?, ?, ?)",
            ("solo", "d", "s", json.dumps(["only"]), json.dumps({"only": []})),
        )
    _seed_session(path, "s5", "solo")
    init_session_state("s5", workflow_name="solo", db_path=path)
    state = advance_phase("s5", db_path=path)
    assert "Already at final phase 'only'." == state["message"]


def test_advance_phase_uses_session_workflow(test_db):
    path = test_db["path"]
    with get_connection(path) as conn:
        conn.execute(
            "INSERT INTO workflows (name, description, steps, phases, phase_requirements) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "duo2", "d", "s",
                json.dumps(["draft", "ship"]),
                json.dumps({"draft": ["Writer"], "ship": ["Releaser"]}),
            ),
        )
    _seed_session(path, "s6", "duo2")
    init_session_state("s6", workflow_name="duo2", db_path=path)
    record_role_contribution("s6", "Writer", db_path=path)
    state = advance_phase("s6", db_path=path)
    # Loaded next phase + requirements from the session's named workflow.
    assert state["current_phase"] == "ship"
    assert state["required_roles"] == ["Releaser"]


def test_check_decision_allowed_no_state_returns_none(test_db):
    assert check_decision_allowed("no-session", db_path=test_db["path"]) is None


def test_check_decision_allowed_missing_roles_raises(test_db):
    path = test_db["path"]
    _seed_session(path, "s7")
    init_session_state("s7", db_path=path)
    with pytest.raises(WorkflowViolationError, match="required roles have not yet contributed"):
        check_decision_allowed("s7", db_path=path)


def test_check_decision_allowed_when_satisfied(test_db):
    path = test_db["path"]
    _seed_session(path, "s8")
    init_session_state("s8", db_path=path)
    record_role_contribution("s8", "Analyst", db_path=path)
    assert check_decision_allowed("s8", db_path=path) is None


# ─────────────────────────── capture.py ───────────────────────────

def test_suggest_capture_mistake_from_errors():
    s = suggest_capture(
        task_description="Fix the login endpoint",
        outcome="The fix was to add a null check; resolved the crash.",
        errors_encountered="Got a NoneType exception traceback when token was missing",
    )
    assert "mistake" in s["suggested_types"]
    assert s["domain"] in ("backend", "security", "debugging")
    assert s["draft_mistake"] is not None
    assert s["draft_mistake"]["mistake"].startswith("Got a NoneType")
    assert s["draft_mistake"]["fix"].startswith("The fix was")
    assert 0 < s["confidence"]["mistake"] <= 1.0
    assert s["keywords"]  # non-empty
    assert s["influence_prompt"].startswith("### Engram influence")


def test_suggest_capture_pattern_detection():
    s = suggest_capture(
        task_description="This bug keeps happening again, same error every time",
        outcome="Fixed by clearing the cache",
        errors_encountered="recurring stale cache error",
    )
    assert "pattern" in s["suggested_types"]
    assert s["draft_pattern"] is not None
    assert s["draft_pattern"]["name"].endswith("Pattern")
    assert s["draft_pattern"]["symptoms"].startswith("recurring stale cache")


def test_suggest_capture_skill_from_files_changed():
    s = suggest_capture(
        task_description="Add a deploy pipeline",
        outcome="Successfully completed the CI workflow; done.",
        files_changed=["a.py", "b.py", "c.py", "d.py"],
    )
    assert "skill" in s["suggested_types"]
    assert s["draft_skill"] is not None
    assert s["draft_skill"]["domain"] == "devops"
    assert s["draft_skill"]["name"].endswith("Workflow")
    assert "a.py" in s["draft_skill"]["key_files"]


def test_suggest_capture_default_skill_fallback():
    # Neutral text with an outcome but no strong signals → default skill suggestion.
    s = suggest_capture(
        task_description="Rename a variable",
        outcome="Renamed foo to bar.",
    )
    assert s["suggested_types"] == ["skill"]
    assert s["confidence"]["skill"] == 0.4
    assert s["draft_skill"]["pitfalls"] is None


def test_suggest_capture_no_outcome_no_suggestions():
    s = suggest_capture(task_description="just some neutral note", outcome="")
    assert s["suggested_types"] == []
    assert s["draft_mistake"] is None
    assert s["draft_skill"] is None


def test_suggest_capture_reuse_hints(monkeypatch):
    fake_rates = {"skill": {"eligible": 20, "reused": 2, "rate": 0.1}}
    monkeypatch.setattr("src.maintenance.get_reuse_rates", lambda: fake_rates)
    s = suggest_capture(
        task_description="Rename a variable",
        outcome="Renamed foo to bar.",
    )
    assert "skill" in s["reuse_hints"]
    assert "10%" in s["reuse_hints"]["skill"]
    assert "2/20" in s["reuse_hints"]["skill"]


def test_suggest_capture_reuse_hints_swallows_errors(monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr("src.maintenance.get_reuse_rates", boom)
    s = suggest_capture(task_description="x", outcome="did a thing")
    assert s["reuse_hints"] == {}


def test_format_capture_suggestion_no_signals():
    s = suggest_capture(task_description="neutral note", outcome="")
    out = format_capture_suggestion(s)
    assert "No strong signals detected" in out
    assert SESSION_INFLUENCE_PROMPT.rstrip() in out


def test_format_capture_suggestion_full(monkeypatch):
    fake_rates = {"mistake": {"eligible": 15, "reused": 1, "rate": 0.05}}
    monkeypatch.setattr("src.maintenance.get_reuse_rates", lambda: fake_rates)
    s = suggest_capture(
        task_description="Fix the recurring login crash bug that keeps happening again",
        outcome="Successfully fixed by adding a null check; the workflow is documented.",
        errors_encountered="NoneType exception traceback, same error every time",
        files_changed=["auth.py", "session.py"],
    )
    out = format_capture_suggestion(s)
    assert out.startswith("## Engram Memory Capture Suggestion")
    assert "Domain:" in out
    assert "Reuse check (mistake)" in out
    assert "### Mistake" in out
    assert "### Pattern" in out
    assert "### Skill" in out
    assert "**Root cause:**" in out
    assert "Key files:" in out
    assert "Reply 'save'" in out
