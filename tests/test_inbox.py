"""Tests for the inbox: filing, dedup, decisions, monitor semantics, change journal."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.inbox import decide, file_item, list_items, open_counts
from src.reflex import approve_reflex, promote_skill, run_reflex


def test_file_and_list_ordered_by_severity(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_NOTIFY_MIN_SEVERITY", "critical")  # keep notify quiet
    file_item(title="minor", severity="info", db_path=test_db["path"])
    file_item(title="urgent", severity="critical", db_path=test_db["path"])
    items = list_items(db_path=test_db["path"])
    assert [i["title"] for i in items] == ["urgent", "minor"]
    assert open_counts(db_path=test_db["path"]) == {"critical": 1, "info": 1}


def test_finding_key_dedups_while_open(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_NOTIFY_MIN_SEVERITY", "critical")
    a = file_item(title="x", finding_key="k1", db_path=test_db["path"])
    b = file_item(title="x again", finding_key="k1", db_path=test_db["path"])
    assert a is not None and b is None
    decide(a, "acknowledge", db_path=test_db["path"])
    c = file_item(title="x resurfaces", finding_key="k1", db_path=test_db["path"])
    assert c is not None  # closed item no longer blocks


def test_decide_approve_run_executes_proposed_reflex(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_NOTIFY_MIN_SEVERITY", "critical")
    from src.database import get_connection

    conn = test_db["conn"]
    conn.execute("INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('Fix It', 'ops', 't', 'w')")
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(1, db_path=test_db["path"])
    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = ? WHERE id = ?", ('echo "did $PARAM_WHAT"', r["id"]))
    approve_reflex(r["id"], db_path=test_db["path"])

    item = file_item(
        kind="decision", title="apply fix?", proposed_reflex_id=r["id"],
        proposed_params={"what": "the-thing"}, db_path=test_db["path"],
    )
    result = decide(item, "approve", run=True, db_path=test_db["path"])
    assert result["status"] == "executed"
    assert "did the-thing" in result["run"]["output"]


def test_decide_rejects_and_blocks_rerun(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_NOTIFY_MIN_SEVERITY", "critical")
    item = file_item(kind="decision", title="risky?", db_path=test_db["path"])
    decide(item, "reject", db_path=test_db["path"])
    with pytest.raises(ValueError, match="already rejected"):
        decide(item, "approve", db_path=test_db["path"])


def _mk_reflex(test_db, script, kind="action", name="Mon Test"):
    from src.database import get_connection

    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, 'ops', 't', 'w')", (name,)
    )
    conn.commit()
    sid = conn.execute("SELECT id FROM skills WHERE name = ?", (name,)).fetchone()[0]
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])
    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = ?, kind = ? WHERE id = ?", (script, kind, r["id"]))
    approve_reflex(r["id"], db_path=test_db["path"])
    return r["id"]


def test_monitor_failure_files_alert_not_demotion(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_NOTIFY_MIN_SEVERITY", "critical")
    from src.database import get_connection

    rid = _mk_reflex(test_db, 'echo "anomaly found"; exit 2', kind="monitor")
    run_reflex(rid, db_path=test_db["path"])
    run_reflex(rid, db_path=test_db["path"])  # twice: would demote an action

    with get_connection(test_db["path"]) as c:
        row = c.execute("SELECT approved_at, fail_streak FROM reflexes WHERE id = ?", (rid,)).fetchone()
    assert row["approved_at"] is not None, "monitors must never auto-demote for firing"
    items = list_items(db_path=test_db["path"])
    assert len(items) == 1, "open monitor alert dedups on refire"
    assert "Mon Test".lower().replace(" ", "_") in items[0]["title"] or "mon_test" in items[0]["title"]


def test_engram_change_lines_are_journaled(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_NOTIFY_MIN_SEVERITY", "critical")
    from src.database import get_connection

    rid = _mk_reflex(
        test_db,
        'echo "ENGRAM_CHANGE target=webapp/WEB_CONCURRENCY before=unset after=3"',
        name="Changer",
    )
    run_reflex(rid, db_path=test_db["path"])
    with get_connection(test_db["path"]) as c:
        rows = c.execute("SELECT * FROM reflex_changes").fetchall()
    assert len(rows) == 1
    assert rows[0]["target"] == "webapp/WEB_CONCURRENCY"
    assert rows[0]["before_value"] == "unset" and rows[0]["after_value"] == "3"


def test_self_check_is_idempotent(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_NOTIFY_MIN_SEVERITY", "critical")
    from src.maintenance import run_self_check

    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count) "
        "VALUES ('Hot Skill', 'ops', 't', 'w', 9)"
    )
    conn.commit()

    first = run_self_check(db_path=test_db["path"])
    second = run_self_check(db_path=test_db["path"])
    assert any(k.startswith("promote:skill:") for k in first["filed"])
    assert second["count"] == 0, "open findings must not re-file daily"
