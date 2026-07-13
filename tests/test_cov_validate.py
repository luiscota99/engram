"""Coverage tests for src/validation.py and src/cli/commands/validate.py."""
from __future__ import annotations

import io
import sys

import pytest

import src.llm as llm_mod
from src.database import get_connection
from src.validation import (
    _grade,
    _memory_content,
    add_skill_test,
    run_all_tests,
    run_skill_test,
    validated_item_ids,
    validation_status,
)

# ── helpers ──────────────────────────────────────────────────────────

def _seed_skill(db_path: str, name: str = "Docker Deploy") -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, ?, ?, ?)",
            (name, "engineering", "When deploying", "Run docker build then push"),
        )
        return cur.lastrowid


def _capture(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


def _no_llm(monkeypatch):
    monkeypatch.setattr(llm_mod, "is_llm_available", lambda *a, **k: False)


def _fake_llm(monkeypatch, answers):
    """answers: list consumed per call_chat_completion invocation."""
    monkeypatch.setattr(llm_mod, "is_llm_available", lambda *a, **k: True)
    calls = iter(answers)

    def _cc(messages, **kwargs):
        return next(calls)

    monkeypatch.setattr(llm_mod, "call_chat_completion", _cc)


# ── add_skill_test ───────────────────────────────────────────────────

class TestAddSkillTest:
    def test_inserts_row_and_returns_id(self, test_db):
        sid = _seed_skill(test_db["path"])
        tid = add_skill_test(
            "skill", sid, "How to deploy?", "docker", db_path=test_db["path"]
        )
        assert isinstance(tid, int)
        with get_connection(test_db["path"]) as c:
            row = dict(c.execute(
                "SELECT * FROM skill_tests WHERE id = ?", (tid,)
            ).fetchone())
        assert row["item_type"] == "skill"
        assert row["item_id"] == sid
        assert row["scenario"] == "How to deploy?"
        assert row["assertion"] == "docker"
        assert row["grader"] == "contains"

    def test_rejects_unknown_grader(self, test_db):
        with pytest.raises(ValueError) as exc:
            add_skill_test(
                "skill", 1, "s", "a", grader="bogus", db_path=test_db["path"]
            )
        assert "grader must be one of" in str(exc.value)

    def test_accepts_llm_judge_grader(self, test_db):
        sid = _seed_skill(test_db["path"])
        tid = add_skill_test(
            "skill", sid, "s", "a", grader="llm_judge", db_path=test_db["path"]
        )
        with get_connection(test_db["path"]) as c:
            grader = c.execute(
                "SELECT grader FROM skill_tests WHERE id = ?", (tid,)
            ).fetchone()["grader"]
        assert grader == "llm_judge"


# ── _memory_content ──────────────────────────────────────────────────

class TestMemoryContent:
    def test_flattens_present_fields(self, test_db):
        sid = _seed_skill(test_db["path"], name="Deploy Skill")
        content = _memory_content("skill", sid, db_path=test_db["path"])
        assert "Deploy Skill" in content
        assert "When deploying" in content
        assert "Run docker build then push" in content

    def test_missing_item_returns_empty(self, test_db):
        assert _memory_content("skill", 99999, db_path=test_db["path"]) == ""


# ── _grade ───────────────────────────────────────────────────────────

class TestGrade:
    def test_contains_case_insensitive_match(self):
        assert _grade("The DOCKER build passed", "docker", "contains") is True

    def test_contains_no_match(self):
        assert _grade("nothing here", "docker", "contains") is False

    def test_contains_handles_none_answer(self):
        assert _grade(None, "docker", "contains") is False

    def test_llm_judge_falls_back_to_contains_when_unavailable(self, monkeypatch):
        _no_llm(monkeypatch)
        assert _grade("has docker inside", "docker", "llm_judge") is True
        assert _grade("no match", "docker", "llm_judge") is False

    def test_llm_judge_yes_verdict(self, monkeypatch):
        _fake_llm(monkeypatch, ["YES, it satisfies"])
        assert _grade("some answer", "requirement", "llm_judge") is True

    def test_llm_judge_no_verdict(self, monkeypatch):
        _fake_llm(monkeypatch, ["NO"])
        assert _grade("some answer", "requirement", "llm_judge") is False

    def test_llm_judge_exception_falls_back_to_contains(self, monkeypatch):
        monkeypatch.setattr(llm_mod, "is_llm_available", lambda *a, **k: True)

        def _boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(llm_mod, "call_chat_completion", _boom)
        # falls back to contains: assertion present -> True
        assert _grade("contains the term xyz", "xyz", "llm_judge") is True


# ── run_skill_test ───────────────────────────────────────────────────

class TestRunSkillTest:
    def test_missing_test_raises(self, test_db):
        with pytest.raises(ValueError) as exc:
            run_skill_test(4242, db_path=test_db["path"])
        assert "4242 not found" in str(exc.value)

    def test_untested_when_no_llm(self, test_db, monkeypatch):
        sid = _seed_skill(test_db["path"])
        tid = add_skill_test("skill", sid, "s", "a", db_path=test_db["path"])
        _no_llm(monkeypatch)
        res = run_skill_test(tid, db_path=test_db["path"])
        assert res["result"] == "untested"
        assert res["reason"] == "no LLM backend reachable"
        with get_connection(test_db["path"]) as c:
            row = c.execute(
                "SELECT last_result, last_run_at FROM skill_tests WHERE id = ?", (tid,)
            ).fetchone()
        assert row["last_result"] == "untested"
        assert row["last_run_at"] is not None

    def test_untested_when_memory_empty(self, test_db, monkeypatch):
        # Point the test at a non-existent item so content is empty.
        tid = add_skill_test("skill", 99999, "s", "a", db_path=test_db["path"])
        monkeypatch.setattr(llm_mod, "is_llm_available", lambda *a, **k: True)
        res = run_skill_test(tid, db_path=test_db["path"])
        assert res["result"] == "untested"
        assert res["reason"] == "memory item empty/missing"

    def test_validated_baseline_fails_treatment_passes(self, test_db, monkeypatch):
        sid = _seed_skill(test_db["path"])
        tid = add_skill_test("skill", sid, "How deploy?", "docker", db_path=test_db["path"])
        # baseline lacks assertion, treatment contains it
        _fake_llm(monkeypatch, ["I have no idea", "you run docker build"])
        res = run_skill_test(tid, db_path=test_db["path"])
        assert res["result"] == "validated"
        assert res["baseline_passed"] is False
        assert res["treatment_passed"] is True
        with get_connection(test_db["path"]) as c:
            row = c.execute(
                "SELECT last_result, baseline_passed, treatment_passed "
                "FROM skill_tests WHERE id = ?", (tid,)
            ).fetchone()
        assert row["last_result"] == "validated"
        assert row["baseline_passed"] == 0
        assert row["treatment_passed"] == 1

    def test_redundant_both_pass(self, test_db, monkeypatch):
        sid = _seed_skill(test_db["path"])
        tid = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        _fake_llm(monkeypatch, ["docker already known", "docker again"])
        res = run_skill_test(tid, db_path=test_db["path"])
        assert res["result"] == "redundant"

    def test_ineffective_treatment_fails(self, test_db, monkeypatch):
        sid = _seed_skill(test_db["path"])
        tid = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        _fake_llm(monkeypatch, ["no term here", "still no term"])
        res = run_skill_test(tid, db_path=test_db["path"])
        assert res["result"] == "ineffective"

    def test_baseline_passes_treatment_fails_is_ineffective(self, test_db, monkeypatch):
        # b_ok=True, t_ok=False: the `elif not t_ok` branch catches this before the
        # unreachable `else: regressed` (validation.py:128 is dead defensive code).
        sid = _seed_skill(test_db["path"])
        tid = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        _fake_llm(monkeypatch, ["docker present", "term absent now"])
        res = run_skill_test(tid, db_path=test_db["path"])
        assert res["result"] == "ineffective"


# ── run_all_tests ────────────────────────────────────────────────────

class TestRunAllTests:
    def test_no_tests_reports_zero(self, test_db):
        r = run_all_tests(db_path=test_db["path"])
        assert r == {"ran": 0, "by_result": {}}

    def test_tallies_results(self, test_db, monkeypatch):
        sid = _seed_skill(test_db["path"])
        t1 = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        t2 = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        assert t1 != t2
        # each test triggers 2 chat calls (baseline, treatment)
        _fake_llm(monkeypatch, [
            "no", "docker",   # t1 -> validated
            "no", "docker",   # t2 -> validated
        ])
        r = run_all_tests(db_path=test_db["path"])
        assert r["ran"] == 2
        assert r["by_result"] == {"validated": 2}

    def test_only_stale_filters_already_run(self, test_db, monkeypatch):
        sid = _seed_skill(test_db["path"])
        t1 = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        # Mark t1 as already run.
        with get_connection(test_db["path"]) as c:
            c.execute(
                "UPDATE skill_tests SET last_run_at = datetime('now'), "
                "last_result = 'validated' WHERE id = ?", (t1,)
            )
        t2 = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        assert t2 != t1
        _fake_llm(monkeypatch, ["no", "docker"])  # only t2 runs
        r = run_all_tests(only_stale=True, db_path=test_db["path"])
        assert r["ran"] == 1
        assert r["by_result"] == {"validated": 1}


# ── validation_status / validated_item_ids ───────────────────────────

class TestValidationStatus:
    def test_none_when_no_tests(self, test_db):
        assert validation_status("skill", 1, db_path=test_db["path"]) is None

    def test_none_when_tests_never_run(self, test_db):
        sid = _seed_skill(test_db["path"])
        add_skill_test("skill", sid, "q", "a", db_path=test_db["path"])
        # last_result is NULL -> filtered out -> None
        assert validation_status("skill", sid, db_path=test_db["path"]) is None

    def test_validated_wins_over_others(self, test_db):
        sid = _seed_skill(test_db["path"])
        with get_connection(test_db["path"]) as c:
            c.execute(
                "INSERT INTO skill_tests (item_type, item_id, scenario, assertion, "
                "grader, last_result) VALUES ('skill', ?, 's', 'a', 'contains', 'ineffective')",
                (sid,),
            )
            c.execute(
                "INSERT INTO skill_tests (item_type, item_id, scenario, assertion, "
                "grader, last_result) VALUES ('skill', ?, 's', 'a', 'contains', 'validated')",
                (sid,),
            )
        assert validation_status("skill", sid, db_path=test_db["path"]) == "validated"

    def test_returns_first_result_when_no_validated(self, test_db):
        sid = _seed_skill(test_db["path"])
        with get_connection(test_db["path"]) as c:
            c.execute(
                "INSERT INTO skill_tests (item_type, item_id, scenario, assertion, "
                "grader, last_result) VALUES ('skill', ?, 's', 'a', 'contains', 'redundant')",
                (sid,),
            )
        assert validation_status("skill", sid, db_path=test_db["path"]) == "redundant"


class TestValidatedItemIds:
    def test_collects_only_validated(self, test_db):
        with get_connection(test_db["path"]) as c:
            c.execute(
                "INSERT INTO skill_tests (item_type, item_id, scenario, assertion, "
                "grader, last_result) VALUES ('skill', 5, 's', 'a', 'contains', 'validated')"
            )
            c.execute(
                "INSERT INTO skill_tests (item_type, item_id, scenario, assertion, "
                "grader, last_result) VALUES ('mistake', 7, 's', 'a', 'contains', 'ineffective')"
            )
        ids = validated_item_ids(db_path=test_db["path"])
        assert ("skill", 5) in ids
        assert ("mistake", 7) not in ids


# ── cmd_validate ─────────────────────────────────────────────────────

class TestCmdValidateAdd:
    def test_add_prints_confirmation(self, test_db):
        from src.cli.commands.validate import cmd_validate

        sid = _seed_skill(test_db["path"])

        class Args:
            vaction = "add"
            type = "skill"
            id = sid
            scenario = "How to deploy?"
            assert_ = "docker"
            grader = "contains"

        out = _capture(cmd_validate, Args())
        assert f"attached to skill #{sid}" in out
        assert "Run it: engram validate run" in out

    def test_add_invalid_grader_exits(self, test_db):
        from src.cli.commands.validate import cmd_validate

        class Args:
            vaction = "add"
            type = "skill"
            id = 1
            scenario = "s"
            assert_ = "a"
            grader = "nonsense"

        buf = io.StringIO()
        old_err, old_out = sys.stderr, sys.stdout
        sys.stderr = buf
        sys.stdout = io.StringIO()
        try:
            with pytest.raises(SystemExit) as exc:
                cmd_validate(Args())
        finally:
            sys.stderr, sys.stdout = old_err, old_out
        assert exc.value.code == 1
        assert "grader must be one of" in buf.getvalue()


class TestCmdValidateRun:
    def _args(self, tid):
        class Args:
            vaction = "run"
            id = tid
        return Args()

    def test_run_untested_shows_reason(self, test_db, monkeypatch):
        from src.cli.commands.validate import cmd_validate

        sid = _seed_skill(test_db["path"])
        tid = add_skill_test("skill", sid, "s", "a", db_path=test_db["path"])
        _no_llm(monkeypatch)
        out = _capture(cmd_validate, self._args(tid))
        assert f"Test #{tid}" in out
        assert "UNTESTED" in out
        assert "no LLM backend reachable" in out

    def test_run_validated_output(self, test_db, monkeypatch):
        from src.cli.commands.validate import cmd_validate

        sid = _seed_skill(test_db["path"])
        tid = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        _fake_llm(monkeypatch, ["no", "docker"])
        out = _capture(cmd_validate, self._args(tid))
        assert "VALIDATED" in out
        # validated branch prints no baseline/treatment detail line
        assert "baseline_passed" not in out

    def test_run_redundant_output(self, test_db, monkeypatch):
        from src.cli.commands.validate import cmd_validate

        sid = _seed_skill(test_db["path"])
        tid = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        _fake_llm(monkeypatch, ["docker", "docker"])
        out = _capture(cmd_validate, self._args(tid))
        assert "REDUNDANT" in out
        assert "baseline_passed=True treatment_passed=True" in out
        assert "already knew this" in out

    def test_run_ineffective_output(self, test_db, monkeypatch):
        from src.cli.commands.validate import cmd_validate

        sid = _seed_skill(test_db["path"])
        tid = add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        _fake_llm(monkeypatch, ["nope", "still nope"])
        out = _capture(cmd_validate, self._args(tid))
        assert "INEFFECTIVE" in out
        assert "did not fix the behavior" in out


class TestCmdValidateRunAll:
    def test_no_tests_prints_hint(self, test_db):
        from src.cli.commands.validate import cmd_validate

        class Args:
            vaction = None

        out = _capture(cmd_validate, Args())
        assert "No validation tests yet" in out

    def test_prints_tally_and_summary(self, test_db, monkeypatch):
        from src.cli.commands.validate import cmd_validate

        sid = _seed_skill(test_db["path"])
        add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        add_skill_test("skill", sid, "q", "docker", db_path=test_db["path"])
        _fake_llm(monkeypatch, ["no", "docker", "docker", "docker"])

        class Args:
            vaction = None

        out = _capture(cmd_validate, Args())
        assert "validated" in out
        assert "redundant" in out
        assert "memories proven to change behavior" in out
        assert "1/2" in out
