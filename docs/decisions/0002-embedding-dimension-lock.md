# ADR-0002: 768-Dimension Embedding Lock and Migration Path

**Date:** 2026-06-06  
**Status:** Accepted  
**Deciders:** Engram core team

---

## Context

Hybrid search stores embeddings in `vec_memory embedding float[768]`, matching the default Ollama model `nomic-embed-text`. Other popular models (mxbai-embed-large, bge-large) emit 1024-dimensional vectors. Silently skipping incompatible vectors degrades search to lexical-only without a hard failure — the worst outcome for users who change `ENGRAM_EMBED_MODEL`.

## Decision

**Lock the vector index at 768 dimensions** for v1.x. Only embeddings whose length equals `VEC_EMBEDDING_DIMENSION` (768) are written to `vec_memory`. Record the active dimension in `schema_meta` (`vec_dimension=768`). Provide a documented migration path: `engram migrate-embeddings --target-model <768-dim-model>` that DROP/recreates `vec_memory`, re-embeds all FTS rows, and updates `schema_meta`.

## Consequences

**Positive:**
- Fixed schema simplifies sqlite-vec KNN queries and index sizing.
- Default model (`nomic-embed-text`) works out of the box with strong context window (8192 tokens).
- Dimension mismatch is detectable at embed time; `memory_embedding_status` surfaces stale/failed rows.

**Negative / Tradeoffs:**
- Higher-MTEB 1024-dim models are unavailable until schema migration v11+.
- Full re-embed on model change is O(n) and requires Ollama uptime.
- Users who switch models without migrating get lexical-only search until they run migrate.

**Risks:**
- Silent quality regression if embed failures are ignored — mitigate with `memory_health` and startup warnings when model dim ≠ stored dim.

## Related Decisions

- ADR-0001: Storage backend choice (sqlite-vec).

---

*Accepted 2026-06-06.*
