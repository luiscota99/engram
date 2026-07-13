"""Scheduler self-awareness: the monitor can now detect its own death — a
self-check that's scheduled in cron but never actually runs (e.g. macOS blocks
cron from a ~/Desktop install), plus a heartbeat of the last successful run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import maintenance
from src.database import init_db


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "mem.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    init_db(str(db))
    return str(db)


# ── scheduler_status ─────────────────────────────────────────────────

def test_status_not_scheduled_is_not_stale(store, monkeypatch):
    monkeypatch.setattr(maintenance, "_self_check_scheduled", lambda: False)
    monkeypatch.setattr(maintenance, "_path_is_tcc_protected", lambda p: False)
    s = maintenance.scheduler_status()
    assert s["scheduled"] is False and s["stale"] is False


def test_status_scheduled_but_never_ran_is_stale(store, monkeypatch):
    monkeypatch.setattr(maintenance, "_self_check_scheduled", lambda: True)
    monkeypatch.setattr(maintenance, "_path_is_tcc_protected", lambda p: False)
    s = maintenance.scheduler_status()
    assert s["scheduled"] is True
    assert s["last_success"] is None
    assert s["stale"] is True  # scheduled + never succeeded = blind monitor


def test_status_fresh_heartbeat_not_stale(store, monkeypatch):
    from src import config

    monkeypatch.setattr(maintenance, "_self_check_scheduled", lambda: True)
    monkeypatch.setattr(maintenance, "_path_is_tcc_protected", lambda p: False)
    config.set_persistent("last_self_check", datetime.now(timezone.utc).isoformat())
    assert maintenance.scheduler_status()["stale"] is False


def test_status_old_heartbeat_is_stale(store, monkeypatch):
    from src import config

    monkeypatch.setattr(maintenance, "_self_check_scheduled", lambda: True)
    monkeypatch.setattr(maintenance, "_path_is_tcc_protected", lambda p: False)
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    config.set_persistent("last_self_check", old)
    s = maintenance.scheduler_status()
    assert s["days_since"] is not None and s["days_since"] > 2
    assert s["stale"] is True


# ── heartbeat ────────────────────────────────────────────────────────

def test_heartbeat_writes_parseable_timestamp(store):
    from src import config

    maintenance.record_self_check_heartbeat()
    ts = config.get_persistent("last_self_check")
    assert ts is not None
    datetime.fromisoformat(ts)  # must parse


# ── TCC-protected path detection ─────────────────────────────────────

def test_tcc_warning_none_when_not_protected(monkeypatch):
    monkeypatch.setattr(maintenance, "_path_is_tcc_protected", lambda p: False)
    assert maintenance.install_path_tcc_warning() is None


def test_tcc_warning_text_when_protected(monkeypatch):
    monkeypatch.setattr(maintenance, "_path_is_tcc_protected", lambda p: True)
    w = maintenance.install_path_tcc_warning()
    assert w and "Full Disk Access" in w


def test_path_is_tcc_protected_matches_desktop_on_darwin(monkeypatch, tmp_path):
    import os
    import sys

    # sys/os are imported inside the function, so patch the real modules.
    monkeypatch.setattr(sys, "platform", "darwin")
    home = tmp_path
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    assert maintenance._path_is_tcc_protected(str(home / "Desktop" / "AI" / "engram")) is True
    assert maintenance._path_is_tcc_protected(str(home / "code" / "engram")) is False


# ── run_self_check surfaces a blind monitor + writes heartbeat ───────

def test_self_check_files_blind_monitor_and_heartbeats(store, monkeypatch):
    from src import config

    monkeypatch.setattr(maintenance, "_self_check_scheduled", lambda: True)
    monkeypatch.setattr(maintenance, "_path_is_tcc_protected", lambda p: False)
    assert config.get_persistent("last_self_check") is None

    filed = maintenance.run_self_check(db_path=store)["filed"]
    assert "scheduler:blind" in filed
    # heartbeat recorded after the sweep
    assert config.get_persistent("last_self_check") is not None


def test_self_check_no_blind_alert_when_not_scheduled(store, monkeypatch):
    monkeypatch.setattr(maintenance, "_self_check_scheduled", lambda: False)
    monkeypatch.setattr(maintenance, "_path_is_tcc_protected", lambda p: False)
    filed = maintenance.run_self_check(db_path=store)["filed"]
    assert "scheduler:blind" not in filed
