# ADR-0009: SOTA Scope Fence — Engineering Memory + Action Ladder

**Date:** 2026-07-25  
**Status:** Accepted  
**Deciders:** Engram core team

---

## Context

The 2026 agent-memory market is crowded (Mem0, Hindsight, Zep/Graphiti, MemPalace,
Letta, Cognee, OpenMemory, …). Chasing every conversational-personalization
benchmark conflates Engram with cloud vector+graph products and dilutes the
product that already differentiates it.

## Decision

Engram’s state-of-the-art claim is scoped to:

1. **Local-first engineering memory** — typed artifacts (mistake / pattern / skill /
   prompt / conversation), hybrid FTS5 + sqlite-vec, single SQLite file by default.
2. **Action compilation** — Action Ladder (reflex → recall → reason) with
   human-approved reflexes; propose-don’t-decide inbox.
3. **IDE enforcement** — hooks that inject recall/guard/checkpoints without relying
   on the agent to remember to search.
4. **Honest measurement** — retrieval metrics labeled as such; home-domain EEME;
   instrument gates before adopting fitted ranking weights.

Out of scope unless a later ADR revisits:

- Becoming a full agent runtime (Letta-class).
- Requiring Postgres/Neo4j for core features (optional backends only).
- MCP surfaces with dozens of overlapping tools.
- Auto-deletion of memories.
- Winning LongMemEval QA leaderboards by changing the metric definition.

Competitive features we *do* adopt (temporal `as_of`, entities, explain traces,
importers, provider ABC, optional rerank/ONNX) must serve the fence above.

## Consequences

**Positive:** Clear positioning vs Mem0/Hindsight/MemPalace; roadmap rejects
feature sprawl; marketing and benches stay honest.

**Negative / Tradeoffs:** Will not claim #1 on every conversational QA leaderboard;
some enterprise “memory platform” RFPs will prefer Zep/Hindsight.

**Risks:** Scope creep via “just one more Mem0 feature” — gate with this ADR.

## Related

- ADR-0001 storage backend  
- ADR-0004 MCP taxonomy  
- ADR-0006 action ladder  
- [`docs/COMPARISON.md`](../COMPARISON.md)

---

*Accepted 2026-07-25.*
