# ADR-0001: SQLite + FTS5 + sqlite-vec as Primary Storage Backend

**Date:** 2026-06-06  
**Status:** Accepted  
**Deciders:** Engram core team

---

## Context

Engram needs a local-first memory store that supports lexical full-text search, vector similarity, relational joins (tags, sessions, workflows), and single-file portability. Alternatives include dedicated vector DBs (Chroma), graph stores (Neo4j), and markdown-only persistence (Letta-style). Constraints: zero mandatory cloud dependency, MCP/CLI must share one database, and installs must work on a solo developer laptop without Docker.

## Decision

Use **SQLite** as the sole persistence layer with **FTS5** for lexical retrieval and **sqlite-vec** for 768-dimensional embeddings in `vec_memory`. All memory types (mistakes, patterns, skills, conversations, prompts) live in typed tables indexed into a unified FTS5 virtual table.

## Consequences

**Positive:**
- Single `memory.db` file — easy backup, snapshot, and CI temp DB creation.
- FTS5 + vec in-process eliminates network latency and extra services.
- SQL joins enable tags, usage counts, pins, and session state without a second store.

**Negative / Tradeoffs:**
- No horizontal scaling or connection pooling; team/org mode needs a future backend abstraction.
- sqlite-vec extension must be loaded per connection; vec table rebuilds are expensive.
- Vector + relational growth is O(n) in one file — soft limits (~50k items) apply.

**Risks:**
- SQLite write contention under heavy concurrent MCP writes — mitigate with WAL mode and batched writes.
- Replacing sqlite-vec later requires migration — tracked in ADR-0002.

## Related Decisions

- ADR-0002: 768-dim embedding lock and migration path.

---

*Accepted 2026-06-06.*
