# Engram — User Guide

Engram is persistent engineering memory for AI-assisted development, and a
framework that makes agents cheaper and more correct over time. This guide
covers the concepts; see [`COMMANDS.md`](COMMANDS.md) for the full CLI
reference (auto-generated) and [`decisions/`](decisions/) for the "why" behind
each design (ADRs).

## Install

```bash
git clone https://github.com/luiscota99/engram.git && cd engram
bash scripts/setup.sh
engram install          # wire into Cursor / Claude Code / Antigravity at once
```

One local database at `~/.engram/memory.db` is shared across every project and
tool. Requires a local [Ollama](https://ollama.com) with `nomic-embed-text` for
semantic search (degrades to lexical-only if absent).

## The two things Engram stores

1. **Memory** — what you learned: `mistake` (what went wrong + fix), `pattern`
   (recurring problem → standard solution), `skill` (proven workflow),
   `conversation`, `prompt`, and codebase knowledge. Retrieved by hybrid
   FTS5 (lexical) + sqlite-vec (semantic) search fused with RRF.
2. **Action** — what you can safely do: `reflex` (a proven skill compiled to an
   approved, deterministic script an agent invokes as an MCP tool).

## The Action Ladder

Every task has three rungs, cheapest first. Ask for the cheapest correct one:

```bash
engram route "rollback the failed deploy"
```

| Rung | Cost | What it means |
|------|------|---------------|
| **reflex** | ~50 tok | An approved script exists — invoke `reflex_<name>`, don't re-derive |
| **recall** | ~200 tok | Prior art (skill/pattern) matches — follow it |
| **reason** | 1000s tok | No prior art — reason, then capture so next time is cheaper |

Known mistakes matching the task ride along as pitfall warnings on every rung.
Proven memory (see Validation) is badged `✓validated`.

## Memory lifecycle: capture → reuse → promote

1. **Capture** — `engram add …`, or `engram suggest-capture` to draft from a
   task/outcome. Write-time dedup blocks near-duplicates.
2. **Reuse** — every retrieval that gets used bumps `usage_count`
   (`memory_record_usage`). `engram health` reports the reuse rate — the
   capture-quality signal (retrieved ≠ useful).
3. **Promote** — a skill used 5+ times is a promotion candidate. `engram
   promote <id>` drafts a reflex script (LLM-assisted or template); a human
   reviews and `engram reflex approve <id>` pins its hash.

## Reflexes — proven memory compiled to safe action

- **Two-tier trust.** Approval grants the *capability* (and pins the script
  hash — any edit un-approves it). A **read-only** reflex (`approve
  --read-only`) runs freely; a **mutating** reflex asks the user for an
  elicitation confirmation on each agent invocation.
- **Self-correcting.** Two consecutive failures auto-demote the reflex and
  capture the failure as a mistake. Success rates come from `reflex_runs`
  (`engram efficiency`).
- **Journaled.** A script emits `ENGRAM_CHANGE target=… before=… after=…`;
  every reported mutation lands in `reflex_changes` — revertible by information.
- **Kinds.** `action` (default) vs `monitor` — a monitor firing files an inbox
  alert instead of demoting (it's a finding on the watched system, not a bug).

## Monitors, inbox & scheduling

Agents and monitors **propose**; the user **decides**.

```bash
engram schedule 3 "*/15 * * * *"   # run reflex #3 every 15 min (OS cron)
engram schedule self-check "0 9 * * *"
engram inbox                        # alerts + decision requests, severity-ordered
engram decide 7 --approve --run     # the ONLY proposal→execution path
```

Delivery for high-severity items goes through a user-approved `notify` reflex
(`engram notify-init`; macOS notification by default, swap for any webhook).
Threshold via `ENGRAM_NOTIFY_MIN_SEVERITY`.

## Validation — proof a memory works

Reuse proves a memory gets *retrieved*; validation proves it *works* (ADR 0008).

```bash
engram validate add skill 12 --scenario "How do I avoid writer starvation?" --assert "WAL"
engram validate run <test-id>
```

Only **validated** (fails cold, passes with the memory) counts — a test that
passes without the memory proves nothing.

## Self-maintenance

`engram self-check` (cron it daily) files inbox decisions for: promotion
candidates, flaky reflexes (<70% over 5+ runs), reused-but-unvalidated skills,
placeholder mistakes, pending embeddings, consolidation clusters, and reuse
collapse. `engram doctor --repair` fixes FTS/vector drift and orphans.

## Configuration

Every env var is documented in [`../src/config.py`](../src/config.py). Common:

| Var | Purpose |
|-----|---------|
| `ENGRAM_DB_PATH` | Database path (default `~/.engram/memory.db`) |
| `OLLAMA_HOST` | Embedding host (default `http://localhost:11434`) |
| `ENGRAM_EMBED_URL` | OpenAI-compatible endpoint, or `disabled` for lexical-only |
| `ENGRAM_EMBED_CACHE` | `off` to disable the persistent query-embedding cache |
| `ENGRAM_NOTIFY_MIN_SEVERITY` | Inbox notify threshold (default `high`) |

## Benchmarks

Reproducible numbers in [`../benchmarks/BENCHMARKS.md`](../benchmarks/BENCHMARKS.md):
home-domain R@5 = 1.00; LongMemEval oracle session-level R@5 ≈ 0.54, fully local.
