# ADR-0003: Committee Workflow — 2-Phase Default and Force-Bypass

**Date:** 2026-06-06  
**Status:** Accepted  
**Deciders:** Engram core team

---

## Context

Engram ships a committee-driven SDLC (Analyst → Researcher → Skeptic → Facilitator → Archivist) as a differentiator vs flat-search tools. The original 5-phase default was heavy for trivial tasks; gates were bypassable on CLI when `current_phase is None`, making the workflow "theater." Agents and users need a lightweight default with an explicit opt-in for full committee mode.

## Decision

1. **Default workflow collapses to 2 phases:** `discuss` (analysis + research + critique combined) and `decide` (decision + archive).
2. **Full 5-phase committee is opt-in only** via explicit user phrases (`@engram committee`, `use committee`) or COMMITTEE mode in Cursor rules.
3. **Enforce workflow gates on all write paths** (CLI and MCP): `memory_add_decision` requires session init and satisfied phase roles unless bypassed.
4. **`--force-bypass`** on CLI (and logged MCP equivalent) skips gates but writes an audit entry to session state — never silent bypass.

## Consequences

**Positive:**
- Trivial fixes no longer incur multi-step committee overhead (LIGHT mode default).
- Decisions logged without session init become impossible by default — workflow has teeth.
- Bypass remains available for emergencies with an audit trail.

**Negative / Tradeoffs:**
- Existing sessions using 5-phase state need migration or manual phase mapping.
- Stricter gates may frustrate scripts that bulk-import decisions — they must use `--force-bypass` with logging.
- MCP and CLI must stay in parity on gate logic (ongoing maintenance).

**Risks:**
- Agents may over-use `--force-bypass` if prompts are unclear — mitigate with facilitator summaries and session review checklist.

## Related Decisions

- ADR-0004: MCP tool taxonomy (session namespace).

---

*Accepted 2026-06-06.*
