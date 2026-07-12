"""Tests for skill validation — Superpowers' 'watch it fail first' rigor."""
from __future__ import annotations

from unittest.mock import patch

from src.validation import (
    add_skill_test,
    run_skill_test,
    validated_item_ids,
    validation_status,
)


def _seed_skill(conn, name="WAL Skill", workflow="enable WAL mode to avoid writer starvation"):
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, 'db', 'sqlite locks', ?)",
        (name, workflow),
    )
    return conn.execute("SELECT id FROM skills WHERE name = ?", (name,)).fetchone()[0]


def _mock_llm(baseline_answer, treatment_answer):
    calls = {"n": 0}

    def fake(messages, **kw):
        calls["n"] += 1
        return baseline_answer if calls["n"] == 1 else treatment_answer

    return fake


def test_validated_when_baseline_fails_and_treatment_passes(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    tid = add_skill_test("skill", sid, "How to avoid SQLite writer starvation?", "WAL",
                         db_path=test_db["path"])

    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", side_effect=_mock_llm("use a mutex", "enable WAL mode")):
        res = run_skill_test(tid, db_path=test_db["path"])

    assert res["result"] == "validated"
    assert validation_status("skill", sid, db_path=test_db["path"]) == "validated"
    assert ("skill", sid) in validated_item_ids(db_path=test_db["path"])


def test_redundant_when_baseline_already_passes(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    tid = add_skill_test("skill", sid, "avoid writer starvation", "WAL", db_path=test_db["path"])

    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", side_effect=_mock_llm("use WAL", "use WAL mode")):
        res = run_skill_test(tid, db_path=test_db["path"])
    assert res["result"] == "redundant"  # model already knew it — proves nothing


def test_ineffective_when_treatment_fails(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    tid = add_skill_test("skill", sid, "avoid starvation", "WAL", db_path=test_db["path"])

    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", side_effect=_mock_llm("dunno", "still dunno")):
        res = run_skill_test(tid, db_path=test_db["path"])
    assert res["result"] == "ineffective"


def test_untested_without_llm(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    tid = add_skill_test("skill", sid, "scenario", "assertion", db_path=test_db["path"])

    with patch("src.llm.is_llm_available", return_value=False):
        res = run_skill_test(tid, db_path=test_db["path"])
    assert res["result"] == "untested"


def test_self_check_flags_unvalidated_proven_skill(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_NOTIFY_MIN_SEVERITY", "critical")
    from src.maintenance import run_self_check

    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count) "
        "VALUES ('Heavily Used', 'ops', 't', 'w', 8)"
    )
    conn.commit()
    r = run_self_check(db_path=test_db["path"])
    assert any(k.startswith("unvalidated:skill:") for k in r["filed"])
