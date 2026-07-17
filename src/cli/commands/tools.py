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


def cmd_hook_guard(args):
    """PreToolUse guard hook: warn about known mistakes before an action.

    Reads a Claude Code PreToolUse payload (JSON) from stdin and prints a
    hook-output JSON block surfacing relevant mistakes/patterns, or nothing when
    none apply. ``--strict`` asks the user to confirm instead of just warning.
    """
    from ...hooks import guard_from_payload

    stdin_text = ""
    if not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read()
        except Exception:
            stdin_text = ""
    out = guard_from_payload(stdin_text, strict=getattr(args, "strict", False))
    if out:
        print(out)


def cmd_hook_checkpoint(args):
    """Stop hook: upsert a crash-proof session checkpoint after every turn.

    Reads a Claude Code Stop payload (JSON) from stdin and records the last
    user prompt, last assistant reply, and git HEAD for (project, session).
    Prints nothing and never fails the turn it records.
    """
    from ...checkpoint import checkpoint_from_stop_payload

    stdin_text = ""
    if not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read()
        except Exception:
            stdin_text = ""
    checkpoint_from_stop_payload(stdin_text)


def cmd_resume(args):
    """Print "where we left off" for a project from its session checkpoints."""
    from ...checkpoint import build_resume_report

    project = getattr(args, "project", None) or os.getcwd()
    report = build_resume_report(project, limit=getattr(args, "count", 1) or 1)
    if not report:
        print(
            "No checkpoints for this project yet. They appear automatically "
            "once the Stop hook is installed (engram bootstrap)."
        )
        return
    print(report)


def cmd_weights(args):
    """Manage fitted ranking weights: engram weights show|apply|clear.

    apply installs a PROVEN candidate (from benchmarks/fit_ranking.py) into
    the store dir, where ranking loads it at import. Unproven files refuse —
    provenance is the harness's signature, not a formality.
    """
    import json
    import shutil

    from ...ranking_weights import current_weights, persisted_weights_path

    action = getattr(args, "weights_action", None) or "show"
    path = persisted_weights_path()
    if action == "show":
        print(fmt_header("Ranking weights (effective):"))
        for name, val in sorted(current_weights().items()):
            print(f"  {name} = {val:g}")
        print(f"\n  Persisted file: {path} {'(present)' if os.path.exists(path) else '(none — code defaults)'}")
        return
    if action == "clear":
        if os.path.exists(path):
            os.remove(path)
            print(f"✓ Removed {path} — code defaults apply from the next process.")
        else:
            print("No persisted weights to clear.")
        return
    # apply
    src_file = str(getattr(args, "file", "") or "")
    try:
        with open(src_file, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError) as e:
        print(f"Error: cannot read weights file: {e}")
        sys.exit(1)
    if not isinstance(data, dict) or not isinstance(data.get("weights"), dict):
        print("Error: not a weights file (expected {weights: {...}, proven: bool}).")
        sys.exit(1)
    if not data.get("proven"):
        print(
            "Refused: this candidate is not marked proven — it did not pass the "
            "fit harness's holdout decision rule (or was edited by hand)."
        )
        sys.exit(1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copyfile(src_file, path)
    print(f"✓ Installed fitted weights → {path} (applies from the next process; engram weights show to inspect).")


def cmd_bench_label(args):
    """Grow the real-corpus benchmark from real usage: engram bench-label.

    Samples recent unlabeled queries from the audit log, shows each with its
    current top hits, and the user picks the correct answer (1-3), marks
    abstention (a), skips (s), or quits (q). Confirmed labels append to
    evals/real_queries.json. Nothing is labeled without an explicit choice.
    """
    from ... import config
    from ...label_mining import append_labels, build_label, load_label_set, mine_candidates
    from ...search import search

    audit_path = getattr(args, "audit", None) or config.audit_log_path()
    if not audit_path or not os.path.exists(audit_path):
        print("No audit log found — enable with: engram audit on (searches accrue from then).")
        return
    queries_path = getattr(args, "queries", None) or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "evals", "real_queries.json",
    )
    existing = load_label_set(queries_path)
    candidates = mine_candidates(audit_path, existing, limit=getattr(args, "count", 5) or 5)
    if not candidates:
        print("No unlabeled real queries found in the audit log (yet).")
        return
    if not sys.stdin.isatty():
        print(f"{len(candidates)} unlabeled real queries (run interactively to label):")
        for c in candidates:
            print(f"  - {c['query'][:100]}")
        return

    new_labels: list[dict] = []
    for c in candidates:
        print(f"\n{fmt_header('Query:')} {c['query'][:200]}")
        hits = search(c["query"], limit=3, skip_audit=True)
        for i, h in enumerate(hits, 1):
            print(f"  {i}. [{(h.get('item_type') or '?').upper()} #{h.get('item_id')}] {(h.get('title') or '')[:90]}")
        if not hits:
            print("  (no results)")
        try:
            choice = input("Correct answer? [1-3 / a=expects-nothing / s=skip / q=quit] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if choice == "q":
            break
        if choice == "a":
            new_labels.append(build_label(c["query"], existing=existing + new_labels, abstention=True))
        elif choice in ("1", "2", "3") and int(choice) <= len(hits):
            new_labels.append(
                build_label(c["query"], existing=existing + new_labels, item=hits[int(choice) - 1])
            )
        # anything else: skip silently

    if not new_labels:
        print("\nNo labels added.")
        return
    total = append_labels(queries_path, new_labels)
    print(f"\n✓ Added {len(new_labels)} label(s) → {queries_path} ({total} total).")
    print("  Evaluate: cp the DB snapshot first, then run the bench with --queries on it.")


def cmd_guard(args):
    """Scan files (or the staged diff) against known mistakes/patterns.

    Level-4 enforcement for the repo boundary: run from a pre-commit hook so a
    developer sees relevant prior art before committing. Warns by default;
    ``--strict`` exits non-zero if anything matches. Runs against the developer's
    local Engram DB (CI has none, by design).
    """
    from ...hooks import build_guard_warnings

    text_parts: list[str] = []
    if getattr(args, "staged", False):
        try:
            diff = subprocess.run(
                ["git", "diff", "--cached", "--unified=0"],
                capture_output=True, text=True, timeout=15,
            )
            text_parts.append(diff.stdout)
        except Exception as e:
            print(f"Could not read staged diff: {e}")
            return
    for path in getattr(args, "files", None) or []:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                text_parts.append(f"{path}\n{f.read()[:4000]}")
        except OSError:
            continue

    text = "\n".join(p for p in text_parts if p).strip()
    if not text:
        print("engram guard: nothing to scan (pass files or --staged).")
        return

    warnings = build_guard_warnings(text, limit=5)
    if not warnings:
        print("✓ engram guard: no known mistakes/patterns match these changes.")
        return

    print(fmt_header("⚠ engram guard — relevant prior art before you commit:"))
    for w in warnings:
        print(f"  - {w}")
    if getattr(args, "strict", False):
        sys.exit(1)


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
