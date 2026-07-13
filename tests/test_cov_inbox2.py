"""Coverage tests for src/inbox.py and src/cli/commands/inbox.py."""
from __future__ import annotations

import io
import json
import sys
from unittest import mock

import pytest

from src.database import get_connection

# ── Helpers ──────────────────────────────────────────────────────────

def _capture_output(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


def _seed_notify_reflex(db_path: str, *, approved: bool = True) -> int:
    """Insert a skill + a reflex named 'notify' and return the reflex id."""
    with get_connection(db_path) as c:
        sid = c.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow) "
            "VALUES ('Notify', 'system', 't', 'w')"
        ).lastrowid
        rid = c.execute(
            "INSERT INTO reflexes (skill_id, name, description, script, approved_at) "
            "VALUES (?, 'notify', 'd', 'echo hi', ?)",
            (sid, "2026-01-01T00:00:00" if approved else None),
        ).lastrowid
    return rid


# ── src/inbox.py: _severity_rank ─────────────────────────────────────

class TestSeverityRank:
    def test_known_severities_ordered(self):
        from src.inbox import _severity_rank

        assert _severity_rank("info") == 0
        assert _severity_rank("warning") == 1
        assert _severity_rank("high") == 2
        assert _severity_rank("critical") == 3

    def test_unknown_defaults_to_warning_rank(self):
        from src.inbox import _severity_rank

        assert _severity_rank("bogus") == 1


# ── src/inbox.py: file_item ──────────────────────────────────────────

class TestFileItem:
    def test_inserts_row_and_returns_id(self, test_db):
        from src.inbox import file_item

        item_id = file_item(
            title="Disk full", body="line1\nline2", severity="warning",
            source="monitor", db_path=test_db["path"],
        )
        assert isinstance(item_id, int)
        with get_connection(test_db["path"]) as c:
            row = dict(c.execute("SELECT * FROM inbox WHERE id = ?", (item_id,)).fetchone())
        assert row["title"] == "Disk full"
        assert row["body"] == "line1\nline2"
        assert row["source"] == "monitor"
        assert row["status"] == "open"
        assert row["kind"] == "alert"

    def test_invalid_severity_coerced_to_warning(self, test_db):
        from src.inbox import file_item

        item_id = file_item(
            title="weird", severity="apocalyptic", db_path=test_db["path"],
        )
        with get_connection(test_db["path"]) as c:
            row = dict(c.execute("SELECT severity FROM inbox WHERE id = ?", (item_id,)).fetchone())
        assert row["severity"] == "warning"

    def test_proposed_params_serialized_as_json(self, test_db):
        from src.inbox import file_item

        item_id = file_item(
            title="deploy?", kind="decision", severity="high",
            proposed_reflex_id=None, proposed_params={"env": "prod", "n": 3},
            db_path=test_db["path"],
        )
        with get_connection(test_db["path"]) as c:
            row = dict(c.execute(
                "SELECT proposed_params FROM inbox WHERE id = ?", (item_id,)
            ).fetchone())
        assert json.loads(row["proposed_params"]) == {"env": "prod", "n": 3}

    def test_finding_key_dedups_open_item(self, test_db):
        from src.inbox import file_item

        first = file_item(title="Recurring", finding_key="dup:1", db_path=test_db["path"])
        second = file_item(title="Recurring again", finding_key="dup:1", db_path=test_db["path"])
        assert isinstance(first, int)
        assert second is None
        with get_connection(test_db["path"]) as c:
            n = c.execute("SELECT COUNT(*) AS n FROM inbox").fetchone()["n"]
        assert n == 1

    def test_finding_key_refiles_after_item_resolved(self, test_db):
        from src.inbox import file_item

        first = file_item(title="Recurring", finding_key="dup:2", db_path=test_db["path"])
        with get_connection(test_db["path"]) as c:
            c.execute("UPDATE inbox SET status = 'acknowledged' WHERE id = ?", (first,))
        second = file_item(title="Recurring", finding_key="dup:2", db_path=test_db["path"])
        assert isinstance(second, int)
        assert second != first


# ── src/inbox.py: _maybe_notify (exercised via file_item) ────────────

class TestMaybeNotify:
    def test_below_threshold_does_not_run_reflex(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.inbox import file_item

        monkeypatch.delenv("ENGRAM_NOTIFY_MIN_SEVERITY", raising=False)
        called = mock.MagicMock()
        monkeypatch.setattr(reflex_mod, "run_reflex", called)
        _seed_notify_reflex(test_db["path"])

        file_item(title="minor", severity="warning", db_path=test_db["path"])
        called.assert_not_called()

    def test_source_notify_never_notifies(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.inbox import file_item

        monkeypatch.delenv("ENGRAM_NOTIFY_MIN_SEVERITY", raising=False)
        called = mock.MagicMock()
        monkeypatch.setattr(reflex_mod, "run_reflex", called)
        _seed_notify_reflex(test_db["path"])

        file_item(title="crit", severity="critical", source="notify", db_path=test_db["path"])
        called.assert_not_called()

    def test_above_threshold_runs_approved_notify_reflex(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.inbox import file_item

        monkeypatch.delenv("ENGRAM_NOTIFY_MIN_SEVERITY", raising=False)
        called = mock.MagicMock(return_value={"ok": True})
        monkeypatch.setattr(reflex_mod, "run_reflex", called)
        rid = _seed_notify_reflex(test_db["path"], approved=True)

        file_item(title="Boom", body="x" * 600, severity="critical", db_path=test_db["path"])
        called.assert_called_once()
        args, kwargs = called.call_args
        assert args[0] == rid
        assert kwargs["params"]["title"] == "Boom"
        assert kwargs["params"]["severity"] == "critical"
        # body is truncated to 500 chars
        assert len(kwargs["params"]["body"]) == 500

    def test_no_approved_reflex_is_a_noop(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.inbox import file_item

        monkeypatch.delenv("ENGRAM_NOTIFY_MIN_SEVERITY", raising=False)
        called = mock.MagicMock()
        monkeypatch.setattr(reflex_mod, "run_reflex", called)
        _seed_notify_reflex(test_db["path"], approved=False)  # draft, not approved

        item_id = file_item(title="Boom", severity="high", db_path=test_db["path"])
        assert isinstance(item_id, int)
        called.assert_not_called()

    def test_reflex_failure_is_swallowed(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.inbox import file_item

        monkeypatch.delenv("ENGRAM_NOTIFY_MIN_SEVERITY", raising=False)
        boom = mock.MagicMock(side_effect=RuntimeError("channel down"))
        monkeypatch.setattr(reflex_mod, "run_reflex", boom)
        _seed_notify_reflex(test_db["path"], approved=True)

        # Must not raise even though delivery fails.
        item_id = file_item(title="Boom", severity="critical", db_path=test_db["path"])
        assert isinstance(item_id, int)
        boom.assert_called_once()


# ── src/inbox.py: list_items / open_counts ───────────────────────────

class TestListItems:
    def test_orders_by_severity_then_newest(self, test_db):
        from src.inbox import file_item, list_items

        p = test_db["path"]
        file_item(title="info-old", severity="info", db_path=p)
        file_item(title="crit", severity="critical", db_path=p)
        file_item(title="warn", severity="warning", db_path=p)
        file_item(title="high", severity="high", db_path=p)
        file_item(title="info-new", severity="info", db_path=p)

        items = list_items(status="open", db_path=p)
        titles = [i["title"] for i in items]
        assert titles == ["crit", "high", "warn", "info-new", "info-old"]

    def test_filters_by_status(self, test_db):
        from src.inbox import file_item, list_items

        p = test_db["path"]
        oid = file_item(title="open one", db_path=p)
        with get_connection(p) as c:
            c.execute("UPDATE inbox SET status = 'rejected' WHERE id = ?", (oid,))
        assert list_items(status="open", db_path=p) == []
        rejected = list_items(status="rejected", db_path=p)
        assert len(rejected) == 1
        assert rejected[0]["title"] == "open one"


class TestOpenCounts:
    def test_counts_open_by_severity(self, test_db):
        from src.inbox import file_item, open_counts

        p = test_db["path"]
        file_item(title="a", severity="high", db_path=p)
        file_item(title="b", severity="high", db_path=p)
        file_item(title="c", severity="critical", db_path=p)
        resolved = file_item(title="d", severity="info", db_path=p)
        with get_connection(p) as c:
            c.execute("UPDATE inbox SET status = 'acknowledged' WHERE id = ?", (resolved,))

        counts = open_counts(db_path=p)
        assert counts == {"high": 2, "critical": 1}


# ── src/inbox.py: decide ─────────────────────────────────────────────

class TestDecide:
    def test_approve_marks_approved(self, test_db):
        from src.inbox import decide, file_item

        p = test_db["path"]
        iid = file_item(title="Ship it", kind="decision", db_path=p)
        result = decide(iid, "approve", db_path=p)
        assert result == {"id": iid, "status": "approved", "title": "Ship it"}
        with get_connection(p) as c:
            row = dict(c.execute("SELECT status, decided_at FROM inbox WHERE id = ?", (iid,)).fetchone())
        assert row["status"] == "approved"
        assert row["decided_at"] is not None

    def test_reject_and_acknowledge(self, test_db):
        from src.inbox import decide, file_item

        p = test_db["path"]
        r = file_item(title="r", db_path=p)
        a = file_item(title="a", db_path=p)
        assert decide(r, "reject", db_path=p)["status"] == "rejected"
        assert decide(a, "acknowledge", db_path=p)["status"] == "acknowledged"

    def test_missing_item_raises(self, test_db):
        from src.inbox import decide

        with pytest.raises(ValueError, match="9999 not found"):
            decide(9999, "approve", db_path=test_db["path"])

    def test_already_decided_raises(self, test_db):
        from src.inbox import decide, file_item

        p = test_db["path"]
        iid = file_item(title="x", db_path=p)
        decide(iid, "acknowledge", db_path=p)
        with pytest.raises(ValueError, match="already acknowledged"):
            decide(iid, "approve", db_path=p)

    def test_unknown_decision_raises(self, test_db):
        from src.inbox import decide, file_item

        p = test_db["path"]
        iid = file_item(title="x", db_path=p)
        with pytest.raises(ValueError, match="Unknown decision"):
            decide(iid, "maybe", db_path=p)

    def test_approve_with_run_executes_reflex(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.inbox import decide, file_item

        p = test_db["path"]
        rid = _seed_notify_reflex(p, approved=True)
        iid = file_item(
            title="Do the thing", kind="decision",
            proposed_reflex_id=rid, proposed_params={"x": 1}, db_path=p,
        )
        outcome = {"ok": True, "output": "done"}
        run = mock.MagicMock(return_value=outcome)
        monkeypatch.setattr(reflex_mod, "run_reflex", run)

        result = decide(iid, "approve", run=True, db_path=p)
        assert result["status"] == "executed"
        assert result["run"] == outcome
        run.assert_called_once_with(rid, params={"x": 1}, db_path=p)
        with get_connection(p) as c:
            status = c.execute("SELECT status FROM inbox WHERE id = ?", (iid,)).fetchone()["status"]
        assert status == "executed"

    def test_approve_run_without_proposed_reflex_does_not_execute(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.inbox import decide, file_item

        p = test_db["path"]
        iid = file_item(title="no reflex", kind="decision", db_path=p)
        run = mock.MagicMock()
        monkeypatch.setattr(reflex_mod, "run_reflex", run)
        result = decide(iid, "approve", run=True, db_path=p)
        assert result["status"] == "approved"
        assert "run" not in result
        run.assert_not_called()


# ── src/cli/commands/inbox.py: cmd_inbox ─────────────────────────────

class TestCmdInbox:
    def test_empty_inbox_message(self, test_db):
        from src.cli.commands.inbox import cmd_inbox

        class Args:
            status = "open"

        out = _capture_output(cmd_inbox, Args())
        assert "Inbox vacío" in out

    def test_lists_items_with_icon_body_and_hint(self, test_db, monkeypatch):
        from src.cli.commands.inbox import cmd_inbox
        from src.inbox import file_item

        p = test_db["path"]
        monkeypatch.delenv("ENGRAM_NOTIFY_MIN_SEVERITY", raising=False)
        rid = _seed_notify_reflex(p, approved=False)
        file_item(
            title="Deploy prod", kind="decision", severity="critical",
            body="first line body\nsecond line", proposed_reflex_id=rid, db_path=p,
        )

        class Args:
            status = "open"

        out = _capture_output(cmd_inbox, Args())
        assert "Inbox (1 abiertos)" in out
        assert "🔴" in out
        assert "DECISIÓN" in out
        assert "Deploy prod" in out
        assert "first line body" in out
        assert "second line" not in out  # only first line shown
        assert f"engram decide {1} --approve --run" not in out or "--approve --run" in out
        assert "--approve --run" in out

    def test_alert_kind_label(self, test_db):
        from src.cli.commands.inbox import cmd_inbox
        from src.inbox import file_item

        file_item(title="just info", kind="alert", severity="info", db_path=test_db["path"])

        class Args:
            status = "open"

        out = _capture_output(cmd_inbox, Args())
        assert "alerta" in out
        assert "·" in out  # info icon


# ── src/cli/commands/inbox.py: cmd_decide ────────────────────────────

class TestCmdDecide:
    def test_prints_confirmation(self, test_db):
        from src.cli.commands.inbox import cmd_decide
        from src.inbox import file_item

        iid = file_item(title="Approve me", db_path=test_db["path"])

        class Args:
            id = str(iid)
            approve = True
            reject = False
            run = False

        out = _capture_output(cmd_decide, Args())
        assert f"#{iid}" in out
        assert "approved" in out
        assert "Approve me" in out

    def test_reject_path(self, test_db):
        from src.cli.commands.inbox import cmd_decide
        from src.inbox import file_item

        iid = file_item(title="Nope", db_path=test_db["path"])

        class Args:
            id = str(iid)
            approve = False
            reject = True
            run = False

        out = _capture_output(cmd_decide, Args())
        assert "rejected" in out

    def test_error_exits_nonzero(self, test_db, capsys):
        from src.cli.commands.inbox import cmd_decide

        class Args:
            id = "99999"
            approve = True
            reject = False
            run = False

        with pytest.raises(SystemExit) as ei:
            cmd_decide(Args())
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_run_outcome_printed(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.cli.commands.inbox import cmd_decide
        from src.inbox import file_item

        p = test_db["path"]
        rid = _seed_notify_reflex(p, approved=True)
        iid = file_item(
            title="Run it", kind="decision", proposed_reflex_id=rid, db_path=p,
        )
        monkeypatch.setattr(
            reflex_mod, "run_reflex",
            mock.MagicMock(return_value={"ok": True, "output": "reflex output here"}),
        )

        class Args:
            id = str(iid)
            approve = True
            reject = False
            run = True

        out = _capture_output(cmd_decide, Args())
        assert "executed" in out
        assert "reflex output here" in out

    def test_run_failure_outcome_printed(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.cli.commands.inbox import cmd_decide
        from src.inbox import file_item

        p = test_db["path"]
        rid = _seed_notify_reflex(p, approved=True)
        iid = file_item(title="Run it", kind="decision", proposed_reflex_id=rid, db_path=p)
        monkeypatch.setattr(
            reflex_mod, "run_reflex",
            mock.MagicMock(return_value={"ok": False, "error": "it broke"}),
        )

        class Args:
            id = str(iid)
            approve = True
            reject = False
            run = True

        out = _capture_output(cmd_decide, Args())
        assert "✗" in out
        assert "it broke" in out


# ── src/cli/commands/inbox.py: cmd_self_check ────────────────────────

class TestCmdSelfCheck:
    def test_reports_new_findings(self, test_db, monkeypatch):
        import src.maintenance as maint
        from src.cli.commands.inbox import cmd_self_check

        monkeypatch.setattr(
            maint, "run_self_check",
            mock.MagicMock(return_value={"count": 2, "filed": ["promote:skill:1", "reflex:2"]}),
        )
        out = _capture_output(cmd_self_check, object())
        assert "2 hallazgo(s) nuevos" in out
        assert "promote:skill:1" in out
        assert "reflex:2" in out

    def test_reports_no_findings(self, test_db, monkeypatch):
        import src.maintenance as maint
        from src.cli.commands.inbox import cmd_self_check

        monkeypatch.setattr(
            maint, "run_self_check",
            mock.MagicMock(return_value={"count": 0, "filed": []}),
        )
        out = _capture_output(cmd_self_check, object())
        assert "sin hallazgos nuevos" in out


# ── src/cli/commands/inbox.py: crontab helpers ───────────────────────

class TestCrontabHelpers:
    def test_crontab_lines_parses_stdout(self, monkeypatch):
        import src.cli.commands.inbox as mod

        fake = mock.MagicMock(returncode=0, stdout="line a\nline b\n")
        monkeypatch.setattr(mod.subprocess, "run", mock.MagicMock(return_value=fake))
        assert mod._crontab_lines() == ["line a", "line b"]

    def test_crontab_lines_empty_when_no_crontab(self, monkeypatch):
        import src.cli.commands.inbox as mod

        fake = mock.MagicMock(returncode=1, stdout="")
        monkeypatch.setattr(mod.subprocess, "run", mock.MagicMock(return_value=fake))
        assert mod._crontab_lines() == []

    def test_crontab_lines_swallows_exception(self, monkeypatch):
        import src.cli.commands.inbox as mod

        monkeypatch.setattr(mod.subprocess, "run", mock.MagicMock(side_effect=OSError("no crontab")))
        assert mod._crontab_lines() == []

    def test_write_crontab_pipes_joined_lines(self, monkeypatch):
        import src.cli.commands.inbox as mod

        run = mock.MagicMock()
        monkeypatch.setattr(mod.subprocess, "run", run)
        mod._write_crontab(["a", "b"])
        args, kwargs = run.call_args
        assert args[0] == ["crontab", "-"]
        assert kwargs["input"] == "a\nb\n"

    def test_engram_invocation_contains_module_call(self):
        import src.cli.commands.inbox as mod

        inv = mod._engram_invocation()
        assert inv.startswith("cd ")
        assert "-m src.cli" in inv


# ── src/cli/commands/inbox.py: cmd_schedule ──────────────────────────

class TestCmdSchedule:
    def test_schedule_self_check(self, monkeypatch):
        import src.cli.commands.inbox as mod

        monkeypatch.setattr(mod, "_crontab_lines", mock.MagicMock(return_value=[]))
        written = {}
        monkeypatch.setattr(mod, "_write_crontab", lambda lines: written.setdefault("lines", lines))

        class Args:
            what = "self-check"
            cron = "0 9 * * *"
            remove = False

        out = _capture_output(mod.cmd_schedule, Args())
        assert "Programado (0 9 * * *)" in out
        line = written["lines"][-1]
        assert "self-check" in line
        assert "# engram:self-check" in line
        assert "0 9 * * *" in line

    def test_schedule_reflex_run(self, monkeypatch):
        import src.cli.commands.inbox as mod

        monkeypatch.setattr(mod, "_crontab_lines", mock.MagicMock(return_value=[]))
        written = {}
        monkeypatch.setattr(mod, "_write_crontab", lambda lines: written.setdefault("lines", lines))

        class Args:
            what = "deploy"
            cron = "*/5 * * * *"
            remove = False

        _capture_output(mod.cmd_schedule, Args())
        line = written["lines"][-1]
        assert "reflex run deploy" in line
        assert "# engram:deploy" in line

    def test_unschedule_removes_matching_lines(self, monkeypatch):
        import src.cli.commands.inbox as mod

        existing = [
            "0 9 * * * something # engram:self-check",
            "* * * * * other # engram:deploy",
        ]
        monkeypatch.setattr(mod, "_crontab_lines", mock.MagicMock(return_value=existing))
        written = {}
        monkeypatch.setattr(mod, "_write_crontab", lambda lines: written.setdefault("lines", lines))

        class Args:
            what = "self-check"
            cron = None
            remove = True

        out = _capture_output(mod.cmd_schedule, Args())
        assert "Desprogramado: self-check" in out
        assert written["lines"] == ["* * * * * other # engram:deploy"]


# ── src/cli/commands/inbox.py: cmd_notify_init ───────────────────────

class TestCmdNotifyInit:
    def test_creates_draft_notify_reflex(self, test_db, monkeypatch):
        import src.reflex as reflex_mod
        from src.cli.commands.inbox import NOTIFY_SCRIPT, cmd_notify_init

        # Force the template path so no LLM/network call happens.
        monkeypatch.setattr(reflex_mod, "_llm_draft_script", mock.MagicMock(return_value=None))

        out = _capture_output(cmd_notify_init, object())
        assert "Reflex 'notify' creado como borrador" in out
        with get_connection(test_db["path"]) as c:
            row = dict(c.execute(
                "SELECT script, approved_at FROM reflexes WHERE name = 'notify'"
            ).fetchone())
        assert row["script"] == NOTIFY_SCRIPT
        assert row["approved_at"] is None  # a draft, not yet approved

    def test_reports_when_already_exists(self, test_db):
        from src.cli.commands.inbox import cmd_notify_init

        rid = _seed_notify_reflex(test_db["path"], approved=True)
        out = _capture_output(cmd_notify_init, object())
        assert f"ya existe (#{rid})" in out
