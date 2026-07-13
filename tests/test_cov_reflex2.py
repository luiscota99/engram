"""Coverage tests for src/reflex.py and src/cli/commands/reflex.py.

Focus: uncovered branches — change journaling, output clipping, syntax
validation edge cases, LLM draft params/fallback, approve/run error paths,
timeout, monitor firing, auto-demotion, promotion candidates, MCP tool
listing edge cases, and the CLI command functions.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
from unittest.mock import patch

import pytest

from src.database import get_connection
from src.reflex import (
    _clip_output,
    _related_mistakes,
    approve_reflex,
    get_promotion_candidates,
    get_reflex_success_rates,
    handle_reflex_call,
    list_reflexes,
    promote_skill,
    reflex_tools_for_mcp,
    run_reflex,
    sync_params_schema,
    validate_script_syntax,
)

# ── helpers ──────────────────────────────────────────────────────────

def _seed_skill(path, name, *, usage=0, workflow="echo hello"):
    with get_connection(path) as c:
        cur = c.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count) "
            "VALUES (?, 'ops', 't', ?, ?)",
            (name, workflow, usage),
        )
        return cur.lastrowid


def _insert_approved_reflex(
    path,
    *,
    name,
    script="echo x",
    interpreter="bash",
    kind="action",
    read_only=0,
    params_schema=None,
    approved=True,
    skill_name=None,
):
    """Insert a skill + a (by default approved) reflex, returning the reflex id."""
    sid = _seed_skill(path, skill_name or f"skill for {name}")
    h = hashlib.sha256(script.encode("utf-8")).hexdigest()
    with get_connection(path) as c:
        cur = c.execute(
            """INSERT INTO reflexes
               (skill_id, name, description, script, interpreter, params_schema,
                kind, read_only, approved_at, approved_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                name,
                f"Reflex desc for {name}",
                script,
                interpreter,
                params_schema,
                kind,
                read_only,
                "2026-01-01T00:00:00" if approved else None,
                h if approved else None,
            ),
        )
        return cur.lastrowid


def _capture(func, *args, **kwargs):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


# ── _clip_output ──────────────────────────────────────────────────────

def test_clip_output_short_text_passes_through():
    assert _clip_output("short") == "short"


def test_clip_output_elides_middle_of_long_text():
    text = "H" * 2000 + "M" * 5000 + "T" * 3000
    clipped = _clip_output(text)
    assert "chars elided" in clipped
    assert clipped.startswith("H")
    assert clipped.endswith("T")
    # head + tail budget only, plus the elision marker
    assert len(clipped) < len(text)


# ── validate_script_syntax ────────────────────────────────────────────

def test_validate_python3_valid_returns_none():
    assert validate_script_syntax("x = 1\nprint(x)", "python3") is None


def test_validate_unknown_interpreter():
    assert validate_script_syntax("whatever", "ruby") == "unknown interpreter 'ruby'"


def test_validate_python3_syntax_error_reported():
    err = validate_script_syntax("def foo(:\n  pass", "python3")
    assert err is not None
    assert "invalid syntax" in err or "SyntaxError" in err or "(" in err


def test_validate_python3_null_byte_is_a_failure():
    # A null byte in source is rejected on every Python, but the wrapping
    # differs: <=3.10 raises ValueError -> "validation failed: ...", while
    # 3.11+ raises SyntaxError -> the raw "source code string cannot contain
    # null bytes". Either way it must be a non-None error mentioning the cause.
    err = validate_script_syntax("x = 1\x00", "python3")
    assert err is not None
    assert err.startswith("validation failed:") or "null byte" in err


# ── sync_params_schema ────────────────────────────────────────────────

def test_sync_params_schema_recovers_from_invalid_existing_json():
    out = sync_params_schema('echo "$PARAM_ALPHA $PARAM_BETA"', "{ not valid json")
    schema = json.loads(out)
    assert set(schema["properties"]) == {"alpha", "beta"}
    assert schema["properties"]["alpha"]["description"] == "exported as PARAM_ALPHA"


# ── _related_mistakes error path ──────────────────────────────────────

def test_related_mistakes_swallows_search_errors(test_db):
    with patch("src.search.search", side_effect=RuntimeError("boom")):
        out = _related_mistakes({"name": "x", "trigger_desc": "y"}, db_path=test_db["path"])
    assert out == []


# ── promote: LLM param inference branches + fallback ──────────────────

def test_promote_llm_params_mixed_entries(test_db):
    sid = _seed_skill(test_db["path"], "Mixed Params")
    reply = json.dumps({
        "script": "set -euo pipefail\necho \"$PARAM_A $PARAM_B\"",
        "params": [
            {"name": "a", "description": "first", "required": True},
            "not-a-dict",                       # skipped: not a dict
            {"no_name": True},                  # skipped: no name
            {"name": "b", "description": "second"},  # kept, not required
        ],
    })
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", return_value=reply):
        r = promote_skill(sid, db_path=test_db["path"])
    assert r["drafted_by"] == "llm"
    schema = json.loads(
        [x for x in list_reflexes(db_path=test_db["path"]) if x["id"] == r["id"]][0]["params_schema"]
    )
    assert set(schema["properties"]) == {"a", "b"}
    assert schema["required"] == ["a"]


def test_promote_falls_back_to_template_when_llm_raises(test_db):
    sid = _seed_skill(test_db["path"], "LLM Boom", workflow="step one\nstep two")
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", side_effect=RuntimeError("llm down")):
        r = promote_skill(sid, db_path=test_db["path"])
    assert r["drafted_by"] == "template"
    assert "step one" in r["script"]


def test_promote_duplicate_reflex_name_raises(test_db):
    sid = _seed_skill(test_db["path"], "Dup Skill")
    with patch("src.llm.is_llm_available", return_value=False):
        promote_skill(sid, db_path=test_db["path"])
    sid2 = _seed_skill(test_db["path"], "dup skill")  # same tool-safe slug
    with patch("src.llm.is_llm_available", return_value=False):
        with pytest.raises(ValueError, match="already exists"):
            promote_skill(sid2, db_path=test_db["path"])


# ── approve / run not-found ───────────────────────────────────────────

def test_approve_missing_reflex_raises(test_db):
    with pytest.raises(ValueError, match="9999 not found"):
        approve_reflex(9999, db_path=test_db["path"])


def test_run_missing_reflex_raises(test_db):
    with pytest.raises(ValueError, match="4242 not found"):
        run_reflex(4242, db_path=test_db["path"])


# ── run_reflex: interpreter guard ─────────────────────────────────────

def test_run_rejects_disallowed_interpreter(test_db):
    rid = _insert_approved_reflex(
        test_db["path"], name="ruby_reflex", script="echo x", interpreter="ruby"
    )
    result = run_reflex(rid, db_path=test_db["path"])
    assert result["ok"] is False
    assert result["error"] == "Interpreter 'ruby' not allowed."


# ── run_reflex: change journaling ─────────────────────────────────────

def test_run_journals_reported_changes(test_db):
    script = (
        'echo "ENGRAM_CHANGE target=conf.yml before=1 after=2"\n'
        'echo "ENGRAM_CHANGE target=flag after=on"\n'
        'echo done\n'
    )
    rid = _insert_approved_reflex(test_db["path"], name="journaler", script=script)
    result = run_reflex(rid, db_path=test_db["path"])
    assert result["ok"] is True
    with get_connection(test_db["path"]) as c:
        rows = c.execute(
            "SELECT target, before_value, after_value FROM reflex_changes ORDER BY id"
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("conf.yml", "1", "2"),
        ("flag", None, "on"),
    ]


# ── run_reflex: timeout ───────────────────────────────────────────────

def test_run_reports_timeout(test_db):
    import subprocess as _sp
    rid = _insert_approved_reflex(test_db["path"], name="slowpoke", script="echo hi")
    with patch(
        "src.reflex.subprocess.run",
        side_effect=_sp.TimeoutExpired(cmd="bash", timeout=300),
    ):
        result = run_reflex(rid, db_path=test_db["path"])
    assert result["status"] == "timeout"
    assert "Timed out after 300s" in result["output"]
    with get_connection(test_db["path"]) as c:
        st = c.execute("SELECT last_status FROM reflexes WHERE id = ?", (rid,)).fetchone()[0]
    assert st == "timeout"


# ── run_reflex: monitor firing files an alert (no demotion) ───────────

def test_monitor_failure_files_alert_and_does_not_demote(test_db):
    rid = _insert_approved_reflex(
        test_db["path"], name="disk_watch", script="exit 1", kind="monitor"
    )
    result = run_reflex(rid, db_path=test_db["path"])
    assert result["ok"] is False
    assert result["status"] == "exit_1"
    with get_connection(test_db["path"]) as c:
        alert = c.execute(
            "SELECT title, severity, source, finding_key FROM inbox"
        ).fetchone()
        refl = c.execute(
            "SELECT approved_at, run_count FROM reflexes WHERE id = ?", (rid,)
        ).fetchone()
    assert "Monitor disk_watch fired" in alert["title"]
    assert alert["severity"] == "high"
    assert alert["finding_key"] == "monitor:disk_watch"
    # A monitor firing is a finding, not a broken script: still approved.
    assert refl["approved_at"] is not None
    assert refl["run_count"] == 1


# ── run_reflex: auto-demotion after consecutive failures ──────────────

def test_two_failures_auto_demote_and_capture_mistake(test_db):
    rid = _insert_approved_reflex(
        test_db["path"], name="flaky", script="exit 7", skill_name="Flaky Skill"
    )
    # first failure: streak 1, still approved
    r1 = run_reflex(rid, db_path=test_db["path"])
    assert r1["ok"] is False
    assert not r1.get("demoted")
    with get_connection(test_db["path"]) as c:
        assert c.execute(
            "SELECT approved_at FROM reflexes WHERE id = ?", (rid,)
        ).fetchone()[0] is not None

    # second failure: streak hits threshold → demote
    r2 = run_reflex(rid, db_path=test_db["path"])
    assert r2["demoted"] is True
    assert "auto-demoted" in r2["error"]
    with get_connection(test_db["path"]) as c:
        refl = c.execute(
            "SELECT approved_at, approved_hash, fail_streak FROM reflexes WHERE id = ?", (rid,)
        ).fetchone()
        mistake = c.execute(
            "SELECT context, mistake FROM mistakes ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert refl["approved_at"] is None
    assert refl["approved_hash"] is None
    assert refl["fail_streak"] == 0
    assert "flaky" in mistake["context"]
    assert "Auto-demoted" in mistake["mistake"]


# ── get_promotion_candidates ──────────────────────────────────────────

def test_promotion_candidates_filters_by_uses_and_existing_reflex(test_db):
    proven = _seed_skill(test_db["path"], "Proven", usage=6)
    _seed_skill(test_db["path"], "Barely Used", usage=1)  # below threshold
    compiled = _seed_skill(test_db["path"], "Already Compiled", usage=9)
    # give the compiled skill a reflex so it is excluded
    with get_connection(test_db["path"]) as c:
        c.execute(
            "INSERT INTO reflexes (skill_id, name, description, script) "
            "VALUES (?, 'already_compiled', 'd', 'echo x')",
            (compiled,),
        )
    cands = get_promotion_candidates(db_path=test_db["path"])
    ids = {row["id"] for row in cands}
    assert proven in ids
    assert compiled not in ids
    # below-threshold skill excluded
    names = {row["name"] for row in cands}
    assert "Barely Used" not in names


# ── get_reflex_success_rates ──────────────────────────────────────────

def test_success_rates_returns_empty_when_table_missing(test_db):
    # Drop the runs table (schema_meta stays, so get_connection won't recreate).
    with get_connection(test_db["path"]) as c:
        c.execute("DROP TABLE reflex_runs")
    assert get_reflex_success_rates(db_path=test_db["path"]) == {}


def test_success_rates_computes_rate_and_avg(test_db):
    rid = _insert_approved_reflex(test_db["path"], name="rated", script="echo ok")
    with get_connection(test_db["path"]) as c:
        c.executemany(
            "INSERT INTO reflex_runs (reflex_id, started_at, duration_ms, status) "
            "VALUES (?, '2026-01-01T00:00:00', ?, ?)",
            [(rid, 100, "ok"), (rid, 300, "ok"), (rid, 200, "exit_1")],
        )
    rates = get_reflex_success_rates(db_path=test_db["path"])
    st = rates[rid]
    assert st["runs"] == 3
    assert st["ok"] == 2
    assert st["rate"] == round(2 / 3, 3)
    assert st["avg_ms"] == 200


# ── reflex_tools_for_mcp edge cases ───────────────────────────────────

def test_tools_for_mcp_handles_bad_schema_json(test_db):
    _insert_approved_reflex(
        test_db["path"], name="badschema", script="echo x", params_schema="{ not json"
    )
    tools = reflex_tools_for_mcp(db_path=test_db["path"])
    assert len(tools) == 1
    assert tools[0]["name"] == "reflex_badschema"
    assert tools[0]["inputSchema"] == {"type": "object", "additionalProperties": True}
    # mutating (read_only=0) → mutating tag
    assert tools[0]["description"].startswith("[mutating")


def test_tools_for_mcp_read_only_tag(test_db):
    _insert_approved_reflex(
        test_db["path"], name="ro_tool", script="echo x", read_only=1,
        params_schema='{"type": "object", "properties": {}}',
    )
    tools = reflex_tools_for_mcp(db_path=test_db["path"])
    assert tools[0]["description"].startswith("[read-only]")


def test_tools_for_mcp_swallows_listing_errors(test_db):
    with patch("src.reflex.list_reflexes", side_effect=RuntimeError("db gone")):
        assert reflex_tools_for_mcp(db_path=test_db["path"]) == []


# ── handle_reflex_call ────────────────────────────────────────────────

def test_handle_call_unknown_reflex(test_db):
    out = handle_reflex_call("reflex_nope", {}, db_path=test_db["path"])
    assert out == "Error: no reflex named 'nope'."


def test_handle_call_elicitation_error_proceeds(test_db):
    rid = _insert_approved_reflex(
        test_db["path"], name="mutator2", script='echo "did $PARAM_X"'
    )
    assert rid  # mutating (read_only=0) → elicitation attempted
    with patch("src.mcp.protocol.elicit_confirmation", side_effect=RuntimeError("no client")):
        out = handle_reflex_call("reflex_mutator2", {"x": "it"}, db_path=test_db["path"])
    assert "did it" in out


def test_handle_call_reports_failure(test_db):
    _insert_approved_reflex(
        test_db["path"], name="ro_fail", script="exit 1", read_only=1
    )
    out = handle_reflex_call("reflex_ro_fail", {}, db_path=test_db["path"])
    assert out.startswith("Reflex ro_fail failed (exit_1)")


# ── CLI: cmd_promote ──────────────────────────────────────────────────

def test_cmd_promote_prints_script_and_next_steps(test_db):
    from src.cli.commands.reflex import cmd_promote

    sid = _seed_skill(test_db["path"], "CLI Promote", workflow="do a thing")

    class Args:
        skill_id = str(sid)

    with patch("src.llm.is_llm_available", return_value=False):
        out = _capture(cmd_promote, Args())
    assert "Drafted reflex" in out
    assert "cli_promote" in out
    assert "reflex approve" in out
    assert "do a thing" in out  # workflow embedded in template comment


def test_cmd_promote_missing_skill_exits(test_db):
    from src.cli.commands.reflex import cmd_promote

    class Args:
        skill_id = "999999"

    with pytest.raises(SystemExit) as exc:
        _capture(cmd_promote, Args())
    assert exc.value.code == 1


# ── CLI: cmd_reflex list / approve / run ──────────────────────────────

def test_cmd_reflex_list_empty(test_db):
    from src.cli.commands.reflex import cmd_reflex

    class Args:
        action = "list"

    out = _capture(cmd_reflex, Args())
    assert "No reflexes yet" in out


def test_cmd_reflex_list_shows_rows(test_db):
    from src.cli.commands.reflex import cmd_reflex

    _insert_approved_reflex(test_db["path"], name="listed", script="echo x", read_only=1)

    class Args:
        action = "list"

    out = _capture(cmd_reflex, Args())
    assert "listed" in out
    assert "approved" in out
    assert "read-only" in out


def test_cmd_reflex_approve_read_only(test_db):
    from src.cli.commands.reflex import cmd_reflex

    # unapproved reflex we can approve via the CLI path
    rid = _insert_approved_reflex(
        test_db["path"], name="approve_me", script="echo hi", approved=False
    )

    class Args:
        action = "approve"
        id = str(rid)
        read_only = True
        mutating = False

    out = _capture(cmd_reflex, Args())
    assert "Approved reflex 'approve_me'" in out
    assert "read-only" in out
    assert "reflex_approve_me" in out
    with get_connection(test_db["path"]) as c:
        ro = c.execute("SELECT read_only FROM reflexes WHERE id = ?", (rid,)).fetchone()[0]
    assert ro == 1


def test_cmd_reflex_approve_default_mutating_hint(test_db):
    from src.cli.commands.reflex import cmd_reflex

    rid = _insert_approved_reflex(
        test_db["path"], name="approve_mut", script="echo hi", approved=False
    )

    class Args:
        action = "approve"
        id = str(rid)
        read_only = False
        mutating = False

    out = _capture(cmd_reflex, Args())
    assert "Mutating by default" in out
    assert "--read-only" in out


def test_cmd_reflex_approve_error_exits(test_db):
    from src.cli.commands.reflex import cmd_reflex

    class Args:
        action = "approve"
        id = "888888"
        read_only = False
        mutating = False

    with pytest.raises(SystemExit) as exc:
        _capture(cmd_reflex, Args())
    assert exc.value.code == 1


def test_cmd_reflex_run_prints_output_and_completes(test_db, capsys):
    from src.cli.commands.reflex import cmd_reflex

    rid = _insert_approved_reflex(
        test_db["path"], name="cli_run2", script='echo "ran $PARAM_WHO"', read_only=1
    )

    class Args:
        action = "run"
        id = str(rid)
        param = ["who=bob"]

    with pytest.raises(SystemExit) as exc:
        cmd_reflex(Args())
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "completed" in captured.out
    assert "ran bob" in captured.out


def test_cmd_reflex_run_failure_exits_one(test_db, capsys):
    from src.cli.commands.reflex import cmd_reflex

    rid = _insert_approved_reflex(
        test_db["path"], name="cli_fail", script="exit 5", read_only=1
    )

    class Args:
        action = "run"
        id = str(rid)
        param = None

    with pytest.raises(SystemExit) as exc:
        cmd_reflex(Args())
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Reflex failed" in captured.out


# ── CLI: cmd_route ────────────────────────────────────────────────────

def test_cmd_route_prints_rung(test_db):
    from src.cli.commands.reflex import cmd_route

    class Args:
        task = ["fix", "the", "flaky", "test"]

    out = _capture(cmd_route, Args())
    assert "Route:" in out
    # route_task always returns one of the three rungs, upper-cased in the header
    assert any(r in out for r in ("REFLEX", "RECALL", "REASON"))
