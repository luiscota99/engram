"""Miscellaneous tool commands: benchmark, simulate, run, browse."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from ... import config
from ...database import get_connection
from ..fmt import fmt_header


def cmd_benchmark(args):
    from ...benchmark import run_benchmark
    run_benchmark()


def cmd_simulate(args):
    from ...token_simulation import run_simulation
    run_simulation(mock=args.mock)


def cmd_browse(args):
    from ...browse import run_browser
    run_browser()


def cmd_retrieval_benchmark(args):
    """Delegate to ``benchmarks/engram_retrieval_bench.py`` (sets ``sys.argv``)."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    from pathlib import Path

    engram_root = Path(__file__).resolve().parent.parent.parent.parent
    script = engram_root / "benchmarks" / "engram_retrieval_bench.py"
    if not script.is_file():
        print(f"Error: {script} not found.")
        sys.exit(1)
    old_argv = sys.argv
    try:
        rest = list(args.bench_args or [])
        sys.argv = [str(script)] + rest
        # SourceFileLoader (concrete) declares exec_module in every typeshed
        # version; the abstract spec.loader does not on 3.12 stubs (pyright
        # reportAttributeAccessIssue — CI 3.12 only, invisible on a 3.9 host).
        loader = SourceFileLoader("engram_retrieval_bench", str(script))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        if spec is None:
            print("Error: could not load retrieval benchmark module.")
            sys.exit(1)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        mod.main()
    finally:
        sys.argv = old_argv


def cmd_hook_recall(args):
    """Auto-recall hook: emit relevant memories as injectable agent context.

    Reads a Claude Code hook payload (JSON) from stdin and prints a
    ``UserPromptSubmit`` additionalContext JSON block, or nothing when there is
    no prompt / no match. ``--prompt`` bypasses stdin (for testing or manual use).
    """
    import json

    from ...hooks import build_recall_context, recall_from_payload

    prompt = getattr(args, "prompt", None)
    if prompt:
        text = " ".join(prompt) if isinstance(prompt, list) else str(prompt)
        ctx = build_recall_context(text)
        if ctx:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": ctx,
                }
            }, ensure_ascii=False))
        return

    stdin_text = ""
    if not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read()
        except Exception:
            stdin_text = ""
    out = recall_from_payload(stdin_text)
    if out:
        print(out)


def cmd_run(args):
    prompt_text = " ".join(args.prompt)
    claw_path = args.claw_path or config.claw_path()

    if not claw_path:
        claw_path = shutil.which("claw")

    if not claw_path:
        ai_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        for rel in ("claw-code/rust/target/release/claw", "claw-code/rust/target/debug/claw"):
            candidate = os.path.join(ai_root, rel)
            if os.path.exists(candidate):
                claw_path = candidate
                break

    if not claw_path:
        print(fmt_header("Error: Claw-Code binary ('claw') not found."))
        print("Build claw-code (cargo build --release) or set CLAW_PATH.")
        sys.exit(1)

    context_prefix = ""
    if args.role:
        with get_connection() as conn:
            row = conn.execute("SELECT charter, heuristics FROM roles WHERE name = ?", (args.role,)).fetchone()
            if row:
                context_prefix = f"Role: {args.role}\nCharter: {row['charter']}\nHeuristics: {row['heuristics']}\n\n"

    full_prompt = context_prefix + prompt_text
    cmd = [claw_path, "--model", args.model, "prompt", full_prompt] if args.model else [claw_path, "prompt", full_prompt]

    print(fmt_header(f"Executing via Claw ({claw_path})...\n"))
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        output_lines = []
        proc_stdout = process.stdout
        if proc_stdout is None:
            raise RuntimeError("subprocess stdout unavailable")
        for line in proc_stdout:
            print(line, end="")
            output_lines.append(line)
        process.wait()
        full_output = "".join(output_lines)

        if args.session_id:
            with get_connection() as conn:
                conn.execute(
                    "INSERT INTO session_transcripts (session_id, role, content) VALUES (?, ?, ?)",
                    (args.session_id, args.role or "Claw", full_output),
                )
            print(f"\n✓ Output logged to Engram session '{args.session_id}'.")
    except Exception as e:
        print(f"\nError executing claw: {e}")
        sys.exit(1)
