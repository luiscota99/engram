#!/usr/bin/env python3
"""
token_benchmark.py — Measure token usage: Traditional vs. Engram-assisted sessions.

Modes:
  --mode traditional   Only run traditional accumulating-history mode
  --mode engram        Only run Engram-assisted session-reset mode
  --mode both          Run both and compare (default)

Token counting:
  Auto-detects Anthropic API key → uses /count_tokens endpoint (free, no generation).
  Falls back to simulation: estimates tokens as len(text) / 4 (GPT-style approximation).

Usage:
  python3 tools/token_benchmark.py
  python3 tools/token_benchmark.py --mode both --turns 10
  python3 tools/token_benchmark.py --api anthropic --output results.json
  ANTHROPIC_API_KEY=sk-... python3 tools/token_benchmark.py

Requirements for real counting:
  pip install anthropic
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

# ── ANSI colors ──────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[1;36m"
GREEN  = "\033[1;32m"
YELLOW = "\033[1;33m"
RED    = "\033[1;31m"
BLUE   = "\033[1;34m"
MAGENTA= "\033[1;35m"


def clr(text, color):
    return f"{color}{text}{RESET}"


# ── Scenario: 10 realistic multi-turn questions ──────────────────────

SCENARIO = [
    {
        "id": 1,
        "label": "FTS5 Setup",
        "user": "How do I set up SQLite FTS5 full-text search in Python? I need to index a table of memory entries and search across title and content fields.",
        "engram_query": "SQLite FTS5 setup Python full-text search",
    },
    {
        "id": 2,
        "label": "Partial Match Bug",
        "user": "My FTS5 search returns no results for partial word matches. For example searching 'migrat' doesn't find 'migration'. How do I fix this?",
        "engram_query": "FTS5 partial word match prefix search",
    },
    {
        "id": 3,
        "label": "Tagging Schema",
        "user": "What's the best SQLite schema design for a many-to-many tagging system? I need to tag items across multiple different tables (mistakes, skills, patterns).",
        "engram_query": "SQLite schema many-to-many tagging polymorphic",
    },
    {
        "id": 4,
        "label": "Vector Search",
        "user": "I want to add semantic vector similarity search to my existing SQLite database. How do I integrate sqlite-vec and store embeddings alongside my existing FTS5 index?",
        "engram_query": "sqlite-vec vector embeddings semantic search hybrid",
    },
    {
        "id": 5,
        "label": "Segfault Debug",
        "user": "The sqlite-vec extension is throwing a segfault when I try to insert a float32 embedding. My embeddings are numpy arrays, could that be the cause?",
        "engram_query": "sqlite-vec segfault float32 numpy embedding error",
    },
    {
        "id": 6,
        "label": "Hybrid Scoring",
        "user": "How do I combine FTS5 keyword search scores and vector cosine similarity scores into a single ranked result list? I want to weight them differently.",
        "engram_query": "hybrid search FTS5 vector scoring weighted ranking",
    },
    {
        "id": 7,
        "label": "Schema Migrations",
        "user": "What's a safe migration strategy for adding new columns and tables to a production SQLite database that users already have installed? I can't lose their data.",
        "engram_query": "SQLite schema migration strategy safe ALTER TABLE",
    },
    {
        "id": 8,
        "label": "MCP Server",
        "user": "How do I expose my SQLite database as an MCP (Model Context Protocol) server so Cursor IDE can call it as a tool? What's the minimal server structure?",
        "engram_query": "MCP server implementation Python tool protocol",
    },
    {
        "id": 9,
        "label": "MCP Hang Debug",
        "user": "My MCP server sometimes hangs indefinitely when Cursor calls it. The process doesn't crash but stops responding. What are the common causes and how do I debug this?",
        "engram_query": "MCP server hang timeout debugging subprocess",
    },
    {
        "id": 10,
        "label": "pytest SQLite",
        "user": "How do I write pytest fixtures for code that uses SQLite? I want each test to use a fresh in-memory database and not touch the real database file.",
        "engram_query": "pytest fixtures SQLite in-memory database testing isolation",
    },
]

SYSTEM_BASE = (
    "You are an expert Python and SQLite engineer. "
    "Answer questions about database design, full-text search, vector similarity, "
    "MCP protocols, and testing. Be concise and practical."
)

# Typical AI response length (chars) — used in simulation mode to estimate output tokens
SIMULATED_RESPONSE_CHARS = 1200


# ── Token counting ───────────────────────────────────────────────────

class TokenCounter:
    """
    Wraps token counting. Uses Anthropic's count_tokens endpoint if available,
    otherwise falls back to len(text)/4 simulation.
    """

    def __init__(self, api="auto"):
        self.mode = "simulation"
        self.client = None
        self.model = "claude-sonnet-4-5"
        self.call_count = 0
        self.total_api_time = 0.0

        if api in ("anthropic", "auto"):
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            if key:
                try:
                    import anthropic
                    self.client = anthropic.Anthropic(api_key=key)
                    self.mode = "anthropic"
                    print(clr("  ✓ Anthropic API detected — using real token counts", GREEN))
                except ImportError:
                    print(clr("  ! anthropic package not installed → pip install anthropic", YELLOW))
                except Exception as e:
                    print(clr(f"  ! Anthropic init failed: {e}", YELLOW))

        if self.mode == "simulation":
            print(clr("  ◉ Simulation mode — estimating tokens as len(text)/4", YELLOW))
            print(clr("    (Set ANTHROPIC_API_KEY + pip install anthropic for real counts)", DIM))

    def count(self, system: str, messages: list[dict]) -> int:
        """Count tokens for a system prompt + messages array."""
        self.call_count += 1

        if self.mode == "anthropic":
            t0 = time.time()
            try:
                resp = self.client.messages.count_tokens(
                    model=self.model,
                    system=system,
                    messages=messages,
                )
                elapsed = time.time() - t0
                self.total_api_time += elapsed
                return resp.input_tokens
            except Exception as e:
                print(clr(f"    [API error: {e} — falling back to simulation]", RED))

        # Simulation fallback
        total_chars = len(system)
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):
                for block in content:
                    total_chars += len(block.get("text", ""))
            else:
                total_chars += len(str(content))
        return max(1, total_chars // 4)

    def estimate_response_tokens(self) -> int:
        """Estimate tokens for a typical AI response in simulation mode."""
        return SIMULATED_RESPONSE_CHARS // 4


# ── Engram retrieval ─────────────────────────────────────────────────

ENGRAM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def engram_search(query: str, limit: int = 5) -> str:
    """
    Run `python3 -m src.cli search` and return formatted context string.
    Returns empty string on failure.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.cli", "search", query, "-n", str(limit)],
            cwd=ENGRAM_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        raw = result.stdout.strip()
        if not raw or "No results found" in raw:
            return ""

        # Strip ANSI codes for clean token counting
        import re
        ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
        clean = ansi_escape.sub('', raw)

        # Take first 1500 chars to keep retrieval overhead realistic
        return clean[:1500]
    except Exception as e:
        return f"[Engram search failed: {e}]"


def build_engram_system(query: str) -> str:
    """Build system prompt with Engram-retrieved context for a given query."""
    context = engram_search(query)
    if context:
        return (
            f"{SYSTEM_BASE}\n\n"
            "## Retrieved Memory Context (from Engram)\n"
            f"{context}\n\n"
            "Use the above context where relevant. If it doesn't apply, answer from general knowledge."
        )
    return SYSTEM_BASE


# ── Benchmark runners ────────────────────────────────────────────────

def run_traditional(counter: TokenCounter, turns: int) -> list[dict]:
    """
    Traditional mode: full conversation history accumulates each turn.
    Returns per-turn token counts.
    """
    messages = []
    results = []
    cumulative_input = 0
    cumulative_output = 0

    for turn_data in SCENARIO[:turns]:
        i = turn_data["id"]
        label = turn_data["label"]
        user_msg = turn_data["user"]

        # Add user message
        messages.append({"role": "user", "content": user_msg})

        # Count input tokens (all history so far)
        input_tokens = counter.count(SYSTEM_BASE, messages)

        # Simulate AI response (fixed estimate or previous turn's response)
        output_tokens = counter.estimate_response_tokens()
        cumulative_input += input_tokens
        cumulative_output += output_tokens

        # Add simulated AI response to history (so next turn includes it)
        simulated_response = (
            f"[Simulated response for turn {i}: {label}. "
            f"{'x' * SIMULATED_RESPONSE_CHARS}]"
        )
        messages.append({"role": "assistant", "content": simulated_response})

        results.append({
            "turn": i,
            "label": label,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cumulative_input": cumulative_input,
            "cumulative_output": cumulative_output,
            "cumulative_total": cumulative_input + cumulative_output,
            "history_length": len(messages),
        })

        print(
            f"    Turn {i:>2} [{label:<18}] "
            f"input={clr(f'{input_tokens:>6,}', CYAN)}  "
            f"cumulative={clr(f'{cumulative_input + cumulative_output:>8,}', YELLOW)}"
        )

    return results


def run_engram(counter: TokenCounter, turns: int) -> list[dict]:
    """
    Engram mode: fresh conversation each turn, Engram context injected.
    Returns per-turn token counts.
    """
    results = []
    cumulative_input = 0
    cumulative_output = 0

    for turn_data in SCENARIO[:turns]:
        i = turn_data["id"]
        label = turn_data["label"]
        user_msg = turn_data["user"]
        engram_query = turn_data["engram_query"]

        # Fresh session: no history
        messages = [{"role": "user", "content": user_msg}]

        # Build system prompt with Engram retrieval
        system = build_engram_system(engram_query)

        # Count input tokens (system + single user message)
        input_tokens = counter.count(system, messages)
        output_tokens = counter.estimate_response_tokens()

        cumulative_input += input_tokens
        cumulative_output += output_tokens

        results.append({
            "turn": i,
            "label": label,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cumulative_input": cumulative_input,
            "cumulative_output": cumulative_output,
            "cumulative_total": cumulative_input + cumulative_output,
            "engram_query": engram_query,
            "system_length": len(system),
        })

        print(
            f"    Turn {i:>2} [{label:<18}] "
            f"input={clr(f'{input_tokens:>6,}', CYAN)}  "
            f"cumulative={clr(f'{cumulative_input + cumulative_output:>8,}', GREEN)}"
        )

    return results


# ── Report rendering ─────────────────────────────────────────────────

def render_report(traditional: list[dict] | None, engram: list[dict] | None, counter_mode: str):
    """Print the comparison table."""
    width = 80
    sep = "─" * width

    print()
    print(clr("╔" + "═" * (width - 2) + "╗", CYAN))
    print(clr("║" + " ENGRAM TOKEN BENCHMARK REPORT".center(width - 2) + "║", CYAN))
    print(clr("╚" + "═" * (width - 2) + "╝", CYAN))
    print()

    counting_label = (
        "Anthropic /count_tokens (exact)"
        if counter_mode == "anthropic"
        else "Simulation — len(text)/4 (estimated)"
    )
    print(f"  {clr('Token counting:', BOLD)} {counting_label}")
    print(f"  {clr('Output tokens:', BOLD)} estimated at {SIMULATED_RESPONSE_CHARS//4} per turn (simulation)")
    print()

    if traditional and engram:
        # Side-by-side comparison
        print(f"  {clr(sep, DIM)}")
        print(
            f"  {'Turn':<5} {'Label':<20} "
            f"{'Trad Input':>12} {'Engram Input':>14} {'Savings':>10} {'Cum Savings':>13}"
        )
        print(f"  {clr(sep, DIM)}")

        for t, e in zip(traditional, engram):
            savings_pct = ((t["input_tokens"] - e["input_tokens"]) / max(t["input_tokens"], 1)) * 100
            cum_savings_pct = (
                (t["cumulative_total"] - e["cumulative_total"]) / max(t["cumulative_total"], 1)
            ) * 100

            savings_str = f"{savings_pct:>+.0f}%"
            cum_str = f"{cum_savings_pct:>+.0f}%"

            savings_color = GREEN if savings_pct > 0 else RED
            cum_color = GREEN if cum_savings_pct > 0 else RED

            print(
                f"  {t['turn']:<5} {t['label']:<20} "
                f"{t['input_tokens']:>12,} {e['input_tokens']:>14,} "
                f"{clr(savings_str, savings_color):>21} "
                f"{clr(cum_str, cum_color):>24}"
            )

        print(f"  {clr(sep, DIM)}")

        trad_total = traditional[-1]["cumulative_total"]
        engram_total = engram[-1]["cumulative_total"]
        total_savings = ((trad_total - engram_total) / max(trad_total, 1)) * 100

        print(
            f"  {'TOTAL':<5} {'':<20} "
            f"{clr(f'{trad_total:>12,}', YELLOW)} {clr(f'{engram_total:>14,}', GREEN)} "
            f"{clr(f'{total_savings:>+.0f}% saved', GREEN if total_savings > 0 else RED):>34}"
        )
        print(f"  {clr(sep, DIM)}")
        print()

        # Summary callout
        if total_savings > 0:
            print(clr(f"  ✓ Engram saves {total_savings:.0f}% total tokens over {len(traditional)} turns", GREEN))
            print(clr(f"  ✓ Traditional grew {trad_total/max(traditional[0]['cumulative_total'],1):.1f}× from turn 1 to {len(traditional)}", YELLOW))
            print(clr(f"  ✓ Engram stayed within {engram[-1]['input_tokens']/max(engram[0]['input_tokens'],1):.1f}× of turn 1 baseline", GREEN))
        else:
            print(clr("  ✗ Traditional used fewer tokens (unexpected — check scenario)", RED))

    elif traditional:
        _render_single(traditional, "Traditional", YELLOW)

    elif engram:
        _render_single(engram, "Engram", GREEN)

    print()


def _render_single(results: list[dict], label: str, color: str):
    sep = "─" * 60
    print(f"  {clr(label + ' Mode', color)}")
    print(f"  {clr(sep, DIM)}")
    print(f"  {'Turn':<5} {'Label':<20} {'Input':>10} {'Cumulative':>12}")
    print(f"  {clr(sep, DIM)}")
    for r in results:
        print(f"  {r['turn']:<5} {r['label']:<20} {r['input_tokens']:>10,} {r['cumulative_total']:>12,}")
    print(f"  {clr(sep, DIM)}")
    print(f"  Total: {results[-1]['cumulative_total']:,} tokens over {len(results)} turns")


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Benchmark token usage: Traditional chat vs. Engram-assisted sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["traditional", "engram", "both"],
        default="both",
        help="Which mode(s) to benchmark (default: both)",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=10,
        choices=range(1, 11),
        metavar="N",
        help="Number of turns to simulate, 1–10 (default: 10)",
    )
    parser.add_argument(
        "--api",
        choices=["anthropic", "auto"],
        default="auto",
        help="API to use for token counting (default: auto-detect)",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Save raw results to a JSON file",
    )
    args = parser.parse_args()

    print()
    print(clr("  ╔══════════════════════════════════════╗", CYAN))
    print(clr("  ║   ENGRAM TOKEN BENCHMARK v1.0        ║", CYAN))
    print(clr("  ╚══════════════════════════════════════╝", CYAN))
    print()
    print(f"  Mode:  {clr(args.mode, BOLD)}")
    print(f"  Turns: {clr(str(args.turns), BOLD)}")
    print()

    # Initialize counter
    print(clr("  Initializing token counter...", DIM))
    counter = TokenCounter(api=args.api)
    print()

    traditional_results = None
    engram_results = None

    if args.mode in ("traditional", "both"):
        print(clr(f"  ▶ Running TRADITIONAL mode ({args.turns} turns)...", YELLOW))
        traditional_results = run_traditional(counter, args.turns)
        print()

    if args.mode in ("engram", "both"):
        print(clr(f"  ▶ Running ENGRAM mode ({args.turns} turns)...", GREEN))
        engram_results = run_engram(counter, args.turns)
        print()

    render_report(traditional_results, engram_results, counter.mode)

    # API timing summary
    if counter.mode == "anthropic" and counter.total_api_time > 0:
        avg_ms = (counter.total_api_time / counter.call_count) * 1000
        print(
            clr(f"  API stats: {counter.call_count} count requests, "
                f"avg {avg_ms:.0f}ms each", DIM)
        )
        print()

    # Save results
    if args.output:
        payload = {
            "meta": {
                "mode": args.mode,
                "turns": args.turns,
                "counter_mode": counter.mode,
                "model": counter.model if counter.mode == "anthropic" else "simulation",
            },
            "traditional": traditional_results,
            "engram": engram_results,
        }
        with open(args.output, "w") as f:
            json.dump(payload, f, indent=2)
        print(clr(f"  ✓ Results saved to {args.output}", DIM))
        print()


if __name__ == "__main__":
    main()
