# ADR 0006: The Action Ladder — token efficiency and action correctness as the framework

Date: 2026-07-07
Status: accepted

## Context

Engram began as a memory store. Two pressures pushed it toward being an agent
framework: (1) agents burn thousands of reasoning tokens re-deriving workflows
they have executed many times before, and (2) LLM-derived actions have variance
— the same workflow reasoned twice can produce two different (and differently
wrong) executions. Prior art that compiled agent workflows into code (Voyager
2023, Agent Workflow Memory 2024) drowned in brittle scripts because promotion
to code was not gated on evidence.

## Decision

Engram's framework surface is a single concept: **the Action Ladder**. Every
task has three possible rungs, cheapest first:

1. **Reflex** — an approved, hash-pinned script for a proven workflow, invoked
   as an MCP tool (`reflex_<name>`). ~50 tokens, deterministic.
2. **Recall** — a proven skill/pattern retrieved from memory; the agent follows
   its steps instead of re-deriving them. ~200 tokens of guidance.
3. **Reason** — no prior art; full LLM reasoning, followed by capture so the
   next occurrence lands higher.

`memory_route` / `engram route` answers "which rung?" in one budgeted call
(≤ ~300 tokens), replacing the agent's search → read → decide loop. Known
mistakes matching the task surface as warnings on **every** rung.

Movement on the ladder is earned, never assumed:

- **Down (cheaper):** capture → measured reuse (usage_count, reuse-rate metric)
  → promotion candidate at 5+ uses → human-reviewed script → approval pins the
  script hash.
- **Up (safer):** 2 consecutive reflex failures auto-revoke approval and record
  the failure as a mistake, so the regression itself becomes memory.

## Consequences

- Token efficiency and correctness are the *same mechanism*: cheap rungs are
  only reachable through evidence of correctness, and correctness failures
  automatically re-impose the token cost of reasoning.
- The framework adds no orchestration, planning, or multi-agent machinery.
  Anything that is not "a memory proving its value over time" is out of scope
  (see the scope-creep warning that motivated this design).
- Efficiency claims are measured floors, not estimates (`engram efficiency`):
  we report workflow text not re-read; unmeasurable reasoning savings are not
  claimed.
- Executing promoted scripts is a security surface. Mitigations: human approval
  required, sha256 pinned at approval (edits un-approve), interpreter allowlist,
  params passed as env vars (never interpolated), bounded runtime and output.
