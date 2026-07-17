"""`engram brain` — per-agent scoped 'mini brains': creation, launcher, listing,
path, and (the whole point) isolation between brains."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from src.cli.commands import brain
from src.database import get_connection
from src.memory_ops import create_mistake


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ENGRAM_DB_PATH", raising=False)
    return tmp_path


def test_brain_new_creates_dir_db_and_launcher(home, capsys):
    brain.cmd_brain_new(SimpleNamespace(name="alpha", seed=False))
    out = capsys.readouterr().out
    assert "created" in out

    bdir = brain._brain_dir("alpha")
    assert os.path.isfile(os.path.join(bdir, "memory.db"))
    launcher = os.path.join(bdir, "brain")
    assert os.path.isfile(launcher)
    # launcher is executable and pins ENGRAM_DB_PATH to this brain's db
    assert os.stat(launcher).st_mode & stat.S_IXUSR
    body = open(launcher).read()
    assert 'ENGRAM_DB_PATH="' + brain._brain_db("alpha") + '"' in body


def test_brain_new_rejects_duplicate(home):
    brain.cmd_brain_new(SimpleNamespace(name="dup", seed=False))
    with pytest.raises(SystemExit):
        brain.cmd_brain_new(SimpleNamespace(name="dup", seed=False))


def test_brain_new_rejects_bad_name(home):
    with pytest.raises(SystemExit):
        brain.cmd_brain_new(SimpleNamespace(name="bad name/../x", seed=False))


def test_brain_list_shows_brains(home, capsys):
    brain.cmd_brain_new(SimpleNamespace(name="one", seed=False))
    brain.cmd_brain_new(SimpleNamespace(name="two", seed=False))
    capsys.readouterr()  # drain
    brain.cmd_brain_list(SimpleNamespace())
    out = capsys.readouterr().out
    assert "one" in out and "two" in out and "Brains (2)" in out


def test_brain_list_empty(home, capsys):
    brain.cmd_brain_list(SimpleNamespace())
    assert "No brains" in capsys.readouterr().out


def test_brain_path_prints_db(home, capsys):
    brain.cmd_brain_new(SimpleNamespace(name="p", seed=False))
    capsys.readouterr()
    brain.cmd_brain_path(SimpleNamespace(name="p"))
    assert brain._brain_db("p") in capsys.readouterr().out.strip()


def test_brain_path_missing_exits(home):
    with pytest.raises(SystemExit):
        brain.cmd_brain_path(SimpleNamespace(name="ghost"))


def test_brains_are_isolated(home, capsys):
    """A memory written to one brain is invisible to another — the core promise."""
    brain.cmd_brain_new(SimpleNamespace(name="rails", seed=False))
    brain.cmd_brain_new(SimpleNamespace(name="mtg", seed=False))

    rails_db = brain._brain_db("rails")
    with get_connection(rails_db) as conn:
        create_mistake(conn, date="2026-07-14", context="puma", mistake="WEB_CONCURRENCY too high", fix="tune")

    with get_connection(rails_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0] == 1
    with get_connection(brain._brain_db("mtg")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0] == 0
