"""Coverage tests for src/cli/commands/tools.py.

Exercises the thin delegator commands (benchmark / simulate / browse), the
retrieval-benchmark script loader, and the Claw-Code `run` command (binary
discovery, role-context prefixing, session-transcript logging, error paths).
All external I/O (subprocess to `claw`, benchmark/simulation/browser entry
points, the retrieval-benchmark module loader) is mocked at the module
boundary so the tests are hermetic.
"""
from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from src.cli.commands import tools
from src.database import get_connection


def _capture_output(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


class _FakeProc:
    """Minimal stand-in for subprocess.Popen result."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.waited = False

    def wait(self):
        self.waited = True


# ── Thin delegators ──────────────────────────────────────────────────

class TestDelegators:
    def test_benchmark_invokes_run_benchmark(self):
        with mock.patch("src.benchmark.run_benchmark") as m:
            tools.cmd_benchmark(SimpleNamespace())
        m.assert_called_once_with()

    def test_browse_invokes_run_browser(self):
        with mock.patch("src.browse.run_browser") as m:
            tools.cmd_browse(SimpleNamespace())
        m.assert_called_once_with()

    def test_simulate_forwards_mock_flag_true(self):
        with mock.patch("src.token_simulation.run_simulation") as m:
            tools.cmd_simulate(SimpleNamespace(mock=True))
        m.assert_called_once_with(mock=True)

    def test_simulate_forwards_mock_flag_false(self):
        with mock.patch("src.token_simulation.run_simulation") as m:
            tools.cmd_simulate(SimpleNamespace(mock=False))
        m.assert_called_once_with(mock=False)


# ── cmd_retrieval_benchmark ───────────────────────────────────────────

class TestRetrievalBenchmark:
    def test_missing_script_prints_error_and_exits(self):
        with mock.patch("pathlib.Path.is_file", return_value=False):
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                with pytest.raises(SystemExit) as exc:
                    tools.cmd_retrieval_benchmark(SimpleNamespace(bench_args=[]))
            finally:
                sys.stdout = old
        assert exc.value.code == 1
        assert "not found" in buf.getvalue()

    def test_loads_script_sets_and_restores_argv(self):
        captured = {}
        sentinel_argv = ["sentinel"]

        class FakeLoader:
            def __init__(self, name, path):
                self.name = name
                self.path = path

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                def _main():
                    captured["argv"] = list(sys.argv)
                module.main = _main

        sys.argv = list(sentinel_argv)
        try:
            with mock.patch(
                "importlib.machinery.SourceFileLoader", FakeLoader
            ):
                tools.cmd_retrieval_benchmark(
                    SimpleNamespace(bench_args=["--mode", "fts_only"])
                )
        finally:
            restored = sys.argv
            sys.argv = ["pytest"]  # neutral value for the rest of the suite

        # main() ran with argv = [<script path>, *bench_args]
        assert captured["argv"][1:] == ["--mode", "fts_only"]
        assert captured["argv"][0].endswith("engram_retrieval_bench.py")
        # argv restored to what it was before the call
        assert restored == sentinel_argv


# ── cmd_run ───────────────────────────────────────────────────────────

class TestCmdRun:
    def _base_args(self, **over):
        defaults = dict(
            prompt=["hello", "world"],
            role=None,
            model=None,
            session_id=None,
            claw_path="/fake/bin/claw",
        )
        defaults.update(over)
        return SimpleNamespace(**defaults)

    def test_binary_not_found_exits_with_guidance(self):
        args = self._base_args(claw_path=None)
        with mock.patch.object(tools.config, "claw_path", return_value=None), \
             mock.patch.object(tools.shutil, "which", return_value=None), \
             mock.patch.object(tools.os.path, "exists", return_value=False):
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                with pytest.raises(SystemExit) as exc:
                    tools.cmd_run(args)
            finally:
                sys.stdout = old
        assert exc.value.code == 1
        out = buf.getvalue()
        assert "not found" in out
        assert "CLAW_PATH" in out

    def test_runs_and_streams_output_no_session(self, test_db):
        args = self._base_args()
        proc = _FakeProc(["first line\n", "second line\n"])
        popen = mock.MagicMock(return_value=proc)
        with mock.patch.object(tools.subprocess, "Popen", popen):
            out = _capture_output(tools.cmd_run, args)
        # streamed stdout content appears verbatim
        assert "first line" in out
        assert "second line" in out
        # invoked the resolved binary with a bare `prompt` verb (no --model)
        cmd = popen.call_args[0][0]
        assert cmd[0] == "/fake/bin/claw"
        assert cmd[1] == "prompt"
        assert cmd[-1] == "hello world"
        assert proc.waited is True

    def test_model_flag_included_in_command(self, test_db):
        args = self._base_args(model="opus-4")
        proc = _FakeProc([])
        popen = mock.MagicMock(return_value=proc)
        with mock.patch.object(tools.subprocess, "Popen", popen):
            _capture_output(tools.cmd_run, args)
        cmd = popen.call_args[0][0]
        assert cmd[:4] == ["/fake/bin/claw", "--model", "opus-4", "prompt"]
        assert cmd[-1] == "hello world"

    def test_role_context_prefixed_to_prompt(self, test_db):
        with get_connection(test_db["path"]) as conn:
            conn.execute(
                "INSERT INTO roles (name, charter, heuristics) VALUES (?, ?, ?)",
                ("Analyst", "Analyze rigorously", "Be skeptical"),
            )
        args = self._base_args(role="Analyst")
        proc = _FakeProc([])
        popen = mock.MagicMock(return_value=proc)
        with mock.patch.object(tools.subprocess, "Popen", popen):
            _capture_output(tools.cmd_run, args)
        full_prompt = popen.call_args[0][0][-1]
        assert full_prompt.startswith("Role: Analyst")
        assert "Charter: Analyze rigorously" in full_prompt
        assert "Heuristics: Be skeptical" in full_prompt
        assert full_prompt.endswith("hello world")

    def test_unknown_role_leaves_prompt_unprefixed(self, test_db):
        args = self._base_args(role="Ghost")
        proc = _FakeProc([])
        popen = mock.MagicMock(return_value=proc)
        with mock.patch.object(tools.subprocess, "Popen", popen):
            _capture_output(tools.cmd_run, args)
        # No matching role row -> prompt passed through untouched
        assert popen.call_args[0][0][-1] == "hello world"

    def test_session_logging_persists_transcript(self, test_db):
        with get_connection(test_db["path"]) as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, title, date, domain) "
                "VALUES (?, ?, ?, ?)",
                ("S1", "Test Session", "2026-07-13", "engineering"),
            )
        args = self._base_args(session_id="S1")
        proc = _FakeProc(["alpha\n", "beta\n"])
        popen = mock.MagicMock(return_value=proc)
        with mock.patch.object(tools.subprocess, "Popen", popen):
            out = _capture_output(tools.cmd_run, args)
        assert "logged to Engram session 'S1'" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute(
                "SELECT role, content FROM session_transcripts WHERE session_id = ?",
                ("S1",),
            ).fetchone()
        assert row is not None
        assert row["role"] == "Claw"
        assert row["content"] == "alpha\nbeta\n"

    def test_subprocess_failure_reports_error_and_exits(self, test_db):
        args = self._base_args()
        with mock.patch.object(
            tools.subprocess, "Popen", side_effect=OSError("boom")
        ):
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                with pytest.raises(SystemExit) as exc:
                    tools.cmd_run(args)
            finally:
                sys.stdout = old
        assert exc.value.code == 1
        assert "Error executing claw: boom" in buf.getvalue()

    def test_binary_discovered_via_ai_root_fallback(self, test_db):
        args = self._base_args(claw_path=None)
        proc = _FakeProc([])
        popen = mock.MagicMock(return_value=proc)
        with mock.patch.object(tools.config, "claw_path", return_value=None), \
             mock.patch.object(tools.shutil, "which", return_value=None), \
             mock.patch.object(tools.os.path, "exists", return_value=True), \
             mock.patch.object(tools.subprocess, "Popen", popen):
            _capture_output(tools.cmd_run, args)
        # first existing candidate is the release build path
        assert popen.call_args[0][0][0].endswith(
            "claw-code/rust/target/release/claw"
        )

    def test_missing_stdout_reports_runtime_error(self, test_db):
        args = self._base_args()
        proc = _FakeProc([])
        proc.stdout = None
        with mock.patch.object(tools.subprocess, "Popen", return_value=proc):
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                with pytest.raises(SystemExit) as exc:
                    tools.cmd_run(args)
            finally:
                sys.stdout = old
        assert exc.value.code == 1
        assert "subprocess stdout unavailable" in buf.getvalue()

    def test_binary_discovered_via_which_when_no_config(self, test_db):
        args = self._base_args(claw_path=None)
        proc = _FakeProc([])
        popen = mock.MagicMock(return_value=proc)
        with mock.patch.object(tools.config, "claw_path", return_value=None), \
             mock.patch.object(tools.shutil, "which", return_value="/usr/bin/claw"), \
             mock.patch.object(tools.subprocess, "Popen", popen):
            _capture_output(tools.cmd_run, args)
        assert popen.call_args[0][0][0] == "/usr/bin/claw"
