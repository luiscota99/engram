"""Coverage tests for the CLI parser + entry point (src/cli/main.py)."""
from __future__ import annotations

import argparse
import importlib
import io
import sys

import pytest

from src.cli.main import build_parser, main

# `src.cli.__init__` re-exports `main` as an attribute, which shadows the
# submodule under attribute access — grab the real module from sys.modules.
main_mod = importlib.import_module("src.cli.main")
db_mod = importlib.import_module("src.database")


def _capture(func, *args, stderr=False, **kwargs):
    """Run func, returning (stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return out.getvalue(), err.getvalue()


# ── build_parser ──────────────────────────────────────────────────────

class TestBuildParser:
    def test_returns_configured_parser(self):
        parser = build_parser()
        assert isinstance(parser, argparse.ArgumentParser)
        assert parser.prog == "engram"

    def test_search_command_defaults_and_func(self):
        from src.cli.commands.memory import cmd_search

        parser = build_parser()
        ns = parser.parse_args(["search", "docker", "deploy", "-n", "5"])
        assert ns.command == "search"
        assert ns.query == ["docker", "deploy"]
        assert ns.limit == 5
        assert ns.func is cmd_search
        # defaults for the un-passed flags
        assert ns.no_project is False
        assert ns.include_superseded is False

    def test_recent_default_limit(self):
        from src.cli.commands.memory import cmd_recent

        ns = build_parser().parse_args(["recent"])
        assert ns.n == 10
        assert ns.func is cmd_recent

    def test_add_mistake_subparser(self):
        from src.cli.commands.memory import cmd_add

        ns = build_parser().parse_args(
            [
                "add",
                "mistake",
                "--date",
                "2026-01-01",
                "--context",
                "ctx",
                "--mistake",
                "did wrong",
                "--fix",
                "did right",
            ]
        )
        assert ns.kind == "mistake"
        assert ns.mistake == "did wrong"
        assert ns.force is False
        assert ns.func is cmd_add

    def test_list_choices_enforced(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["list", "bogus"])
        ns = parser.parse_args(["list", "skills"])
        assert ns.kind == "skills"

    def test_reflex_run_param_append(self):
        from src.cli.commands.reflex import cmd_reflex

        ns = build_parser().parse_args(
            ["reflex", "run", "7", "--param", "a=1", "--param", "b=2"]
        )
        assert ns.action == "run"
        assert ns.id == "7"
        assert ns.param == ["a=1", "b=2"]
        assert ns.func is cmd_reflex

    def test_reflex_requires_action(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["reflex"])

    def test_merge_projects_dest_names(self):
        from src.cli.commands.maintenance import cmd_merge_projects

        ns = build_parser().parse_args(
            ["merge-projects", "--from", "old", "--into", "new"]
        )
        assert ns.merge_from == "old"
        assert ns.merge_into == "new"
        assert ns.execute is False
        assert ns.func is cmd_merge_projects

    def test_bootstrap_mutually_exclusive_mcp(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["bootstrap", "--setup-mcp", "--no-mcp"])
        ns = parser.parse_args(["bootstrap", "--no-mcp"])
        assert ns.setup_mcp is False

    def test_llm_subcommand_defaults(self):
        ns = build_parser().parse_args(["llm", "audit", "--threshold", "0.5"])
        assert ns.llm_command == "audit"
        assert ns.threshold == 0.5
        assert ns.execute is False

    def test_gc_mode_choices(self):
        ns = build_parser().parse_args(["gc", "--mode", "archive", "--days", "30"])
        assert ns.mode == "archive"
        assert ns.days == 30

    def test_validate_add_dest_assert(self):
        ns = build_parser().parse_args(
            ["validate", "add", "skill", "3", "--scenario", "s", "--assert", "a"]
        )
        assert ns.vaction == "add"
        assert ns.type == "skill"
        assert ns.assert_ == "a"

    def test_retrieval_benchmark_remainder(self):
        ns = build_parser().parse_args(
            ["retrieval-benchmark", "--", "--mode", "compare"]
        )
        # argparse.REMAINDER keeps the leading "--" separator verbatim
        assert ns.bench_args == ["--", "--mode", "compare"]


# ── main() dispatch ─────────────────────────────────────────────────────

class TestMainDispatch:
    def test_no_command_prints_help_and_exits_zero(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["engram"])
        with pytest.raises(SystemExit) as exc:
            out, _ = _capture(main)
        assert exc.value.code == 0

    def test_normal_command_inits_and_dispatches(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(
            main_mod, "init_db", lambda *a, **k: calls.setdefault("init", True)
        )
        monkeypatch.setattr(
            db_mod,
            "verify_embedding_schema_match",
            lambda: calls.setdefault("verify", True) and None,
        )
        monkeypatch.setattr(
            main_mod,
            "cmd_stats",
            lambda args: calls.setdefault("dispatched", args.command),
        )
        monkeypatch.setattr(sys, "argv", ["engram", "stats"])
        main()
        assert calls["init"] is True
        assert calls["dispatched"] == "stats"

    def test_dim_mismatch_warning_on_stderr(self, monkeypatch):
        monkeypatch.setattr(main_mod, "init_db", lambda *a, **k: None)
        monkeypatch.setattr(
            db_mod,
            "verify_embedding_schema_match",
            lambda: "dim 384 != 768",
        )
        monkeypatch.setattr(main_mod, "cmd_stats", lambda args: None)
        monkeypatch.setattr(sys, "argv", ["engram", "stats"])
        _, err = _capture(main, stderr=True)
        assert "Warning: dim 384 != 768" in err

    def test_init_command_skips_init_db_and_verify(self, monkeypatch):
        state = {"init_db": False, "verify": False, "cmd": False}

        def _init(*a, **k):
            state["init_db"] = True

        def _verify():
            state["verify"] = True
            return None

        monkeypatch.setattr(main_mod, "init_db", _init)
        monkeypatch.setattr(db_mod, "verify_embedding_schema_match", _verify)
        monkeypatch.setattr(
            main_mod, "cmd_init", lambda args: state.__setitem__("cmd", True)
        )
        monkeypatch.setattr(sys, "argv", ["engram", "init"])
        main()
        assert state["cmd"] is True
        assert state["init_db"] is False  # init command skips the init_db() call
        assert state["verify"] is False  # and the verify block entirely

    def test_migrate_command_inits_but_skips_verify(self, monkeypatch):
        state = {"init_db": False, "verify": False}
        monkeypatch.setattr(
            main_mod,
            "init_db",
            lambda *a, **k: state.__setitem__("init_db", True),
        )

        def _verify():
            state["verify"] = True
            return None

        monkeypatch.setattr(db_mod, "verify_embedding_schema_match", _verify)
        monkeypatch.setattr(main_mod, "cmd_migrate", lambda args: None)
        monkeypatch.setattr(sys, "argv", ["engram", "migrate"])
        main()
        assert state["init_db"] is True  # migrate still initializes the DB
        assert state["verify"] is False  # but skips the embedding-schema check
