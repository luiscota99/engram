# Engram Rage Audit — June 2026

**Session:** `rage-audit-2026-06-06`  
**Committee:** Sr. Engineer, QA, Architect (parallel agents)  
**Facilitator synthesis:** cross-agent debates resolved, findings persisted to Engram DB  
**Scope:** Engram vs Odysseus, MemPalace, MemGPT (local) + Mem0, Zep, Mastra, cursor-brain (web/competitive)

---

## Executive Summary

Engram has the right **core bet** for coding-agent memory: local-first, SQLite + FTS5 + vector hybrid, MCP-native, no cloud account required. That architecture is defensible.

Everything else is a problem.

The project ships a **committee workflow that is trivially bypassed**, a **38-tool MCP surface** that optimizes write ceremony over read intelligence, **zero standard benchmark scores** while competitors publish LongMemEval/LoCoMo numbers, and **15+ untested core modules** behind a CI pipeline that runs 5 of 20 benchmark queries with no quality gate. CLI and MCP are two different products sharing a database — dedup, workflow gates, and search enums diverge on every surface.

The three agents agree on priority order:

1. **Stop lying with green CI** — gate retrieval quality, test MCP handlers, raise coverage floor  
2. **Unify CLI/MCP through a service layer** — one code path, enforced gates  
3. **Publish honest standard benchmarks** — even if scores are lower than MemPalace  
4. **Fix temporal truth** — stale facts poison agent context worse than missing facts  
5. **Document decisions** — ADRs before the next architectural re-litigation

---

## Sr. Engineer Verdict

### 1. Triple-copy CRUD in CLI/MCP/maintenance

**PROBLEM:** Every memory insert duplicates identical `INSERT` + `link_tags` + `index_in_fts` blocks across `cli/commands/memory.py`, `mcp/handlers.py`, plus `maintenance.py`, `seed.py`, `export.py`.

**WHY IT MATTERS:** Schema or FTS format changes require hunting 6+ copies. CLI and MCP already diverge — CLI pattern uses `args.fix`, MCP uses `standard_fix`; CLI mistake content includes prevention, MCP does not.

**FIX:** Extract `src/memory_ops.py` with typed `create_mistake` / `create_pattern` / … functions. CLI and MCP become thin arg-parse wrappers. Follow MemPalace's `search_memories()` vs `search()` split.

---

### 2. `table_map` copy-paste minefield

**PROBLEM:** Item-type→table mappings are redefined independently in `database.py`, `search.py`, `maintenance.py`, and FTS rebuild specs — each incomplete. `search.py` `table_map` omits `session`, so session `usage_count` / `last_used_at` never feed ranking.

**WHY IT MATTERS:** Silent ranking bugs when types are added. No guard that all indexed types participate in search enrichment.

**FIX:** Single `ITEM_TYPES` registry dataclass (table, fts_title_col, fts_content_expr, pinnable, rank_multiplier). Unit test: registry keys == `memory_fts` distinct `item_types`.

---

### 3. Error handling is printf debugging

**PROBLEM:** MCP handlers return ad-hoc `"Error: ..."` strings. CLI uses `sys.exit(1)` with different messages. `semantic_search` catches bare `Exception` and returns `[]`. `embed_text` returns `None` for all failure modes.

**WHY IT MATTERS:** Agents and humans get false "no results" when Ollama is down. No error codes for programmatic retry.

**FIX:** `EngramError` hierarchy (`EmbeddingUnavailable`, `DuplicateBlocked`, `WorkflowViolation`). MCP returns structured JSON `{code, message, hint}`.

---

### 4. Adding a memory type is a 12-file scavenger hunt

**PROBLEM:** New type requires: `SCHEMA_SQL` table, `rebuild_fts` spec, 4+ `table_map` copies, ranking multipliers, type-inference keywords, CLI dispatch + argparser, MCP handler + schema enum, optional GC map. No plugin hook.

**WHY IT MATTERS:** High friction guarantees half-wired types. Sessions are searchable but not usage-ranked.

**FIX:** Declarative `register_memory_type()` wiring schema migration stub, FTS builder, CLI subcommand, MCP tool fragment — one source file per type.

---

### 5. CLI/MCP behavioral parity broken

**PROBLEM:** MCP add paths run `check_duplicate_before_add` + `force` flag; CLI `engram add` has zero dedup. MCP `memory_add_decision` enforces `check_decision_allowed`; CLI `engram add decision` bypasses it entirely. MCP `memory_search` enum excludes `prompt`/`session` though they are indexed.

**WHY IT MATTERS:** Same command via different surfaces produces different DB state and governance. Committee workflow is theater for CLI users.

**FIX:** Shared service layer calls dedup + workflow gates unconditionally. Align tool schema enums with all FTS-indexed types. Add CLI `--force` and workflow-aware decision add.

---

### 6. `search()` opens DB three times per query

**PROBLEM:** `search()` calls `get_connection` separately for lexical query, usage_counts batch, and project affinity — plus `semantic_search` opens another. Affinity failure is swallowed with `logger.exception`.

**WHY IT MATTERS:** WAL lock churn, obscured perf regressions. Contrasts with batch query design elsewhere in the codebase.

**FIX:** Single `with get_connection()` scope for entire `search()`. Pass `conn` through `semantic_search`. Surface affinity errors in `semantic_status`.

---

### 7. Presentation logic duplicated

**PROBLEM:** Embedding stats formatting copy-pasted across `cmd_stats`, `handle_memory_stats`, `handle_memory_embedding_status`, `cmd_health`. MCP `memory_list` does N `get_tags_for_item` queries; CLI uses efficient `_batch_tags`.

**WHY IT MATTERS:** UX strings drift. MCP list is O(n) tag queries.

**FIX:** `src/formatters/stats.py` and `list_items(conn, type)` returning structured rows. MCP/CLI only handle transport formatting.

---

## QA Verdict

### 1. MCP handler coverage crater

**PROBLEM:** `handlers.py` defines 30+ `handle_memory_*` tools. Tests exercise only `handle_memory_llm_status` and session review. Zero direct tests for `memory_search`, `memory_add*`, `memory_merge_entries`, `memory_auto_extract`, workflow tools.

**WHY IT MATTERS:** MCP is the primary integration surface. Green pytest proves nothing about tool contracts. One regression in `handle_memory_search` affects every IDE agent.

**FIX:** Parametrized handler contract tests (happy path + missing arg + DB error) for every `TOOL_HANDLERS` entry. Target ≥80% line coverage on `handlers.py`.

---

### 2. 15+ core modules have zero tests

**PROBLEM:** No test imports: `embeddings.py`, `query_analyzer.py`, `export.py`, `merge.py`, `doctor.py`, `browse.py`, `graph.py`, `backup.py`, `summarize.py`, `auto_extract.py`, `compression.py`, `benchmark.py`, `inject_noise.py`, `migrations.py` (beyond `SCHEMA_VERSION` int).

**WHY IT MATTERS:** These handle migrations, backup/restore, skill export, LLM auto-extract, embeddings — highest-risk paths for data loss and silent retrieval degradation.

**FIX:** One smoke test per module. `test_migrations.py` applies upgrade chain on fixture DB. `test_embeddings.py` with mocked Ollama.

---

### 3. 20-query closed-world benchmark is a toy

**PROBLEM:** `test_queries.json` has exactly 20 queries on seeded DB covering only mistake/pattern/skill. Category `multi_hop` documented but empty. No conversation/prompt/abstention labels. Roadmap TODO for 50–100 queries still open.

**WHY IT MATTERS:** 20 hand-picked queries on synthetic seed cannot detect ranking regressions on real corpora. A 5% lift = 1 query — statistically meaningless.

**FIX:** Expand to ≥100 labeled queries including conversation/prompt/multi_hop/abstention. Wire `inject_noise.py` into CI with R@5 floor. Commit per-category results JSON.

---

### 4. No LongMemEval / LoCoMo / BEAM — cannot compare to field

**PROBLEM:** Engram publishes no scores on standard conversational-memory benchmarks. MemPalace: 96.6% LongMemEval R@5 (500q). Mem0 v3: 94.8% LongMemEval. Mastra: 94.87% LongMemEval. Engram: ~100% R@5 on 20 self-seeded queries — not comparable.

**WHY IT MATTERS:** Users choosing memory systems use published benchmark tables. Without standard evals, Engram cannot defend retrieval quality claims and appears to avoid measurement that might expose gaps.

**FIX:** Add `benchmarks/longmemeval_bench.py` adapter. Publish score honestly even if lower. Commit held-out split. README comparison table with metric definitions (R@5 retrieval vs QA accuracy).

---

### 5. CI benchmark smoke runs 5/20 queries with no gate

**PROBLEM:** `ci.yml` runs `engram_retrieval_bench.py --limit 5` — 25% of query set, no R@5 assertion. `pyproject.toml` sets `fail_under=0`. Semantic leg likely absent without Ollama in CI.

**WHY IT MATTERS:** Named "Retrieval benchmark smoke" step gives false confidence. Hybrid regressions to FTS-only go undetected.

**FIX:** Remove `--limit 5`. Assert R@5≥0.90 in CI bench step. Set `fail_under≥60` and ratchet. Add CI job with mocked embeddings to exercise `vec_memory` path. Fail PR if R@5 drops vs main baseline JSON.

---

### 6. Zero mutation or property testing

**PROBLEM:** No hypothesis, mutmut, or property-based tests. Ranking/grading/RRF rely on hand-picked examples. `grading.py` tests 3 cases.

**WHY IT MATTERS:** Combinatorial input space (query tokens × item types × tag hyphens × embedding availability). Example tests miss empty query, unicode, tie-breaking, score monotonicity.

**FIX:** Hypothesis tests for `grading.row_matches_expected`, `_fts5_tag_phrase`, `reciprocal_rank_scores`. Mutmut on `ranking.py` with ≥70% kill rate target.

---

### 7. Real-world eval is documentation-only

**PROBLEM:** `evals/` contains README + `real_queries.json.example` with 2 placeholder queries. No committed labeled corpus, no CI job, no snapshot pipeline.

**WHY IT MATTERS:** Regression suite measures algorithm on synthetic seed. Realistic fit measurement is entirely optional user homework.

**FIX:** Commit `evals/public_queries.json` (≥30 labels). Add `scripts/validate_eval_labels.py`. Weekly CI job. Document last-known R@5 in `evals/RESULTS.md`.

---

## Architect Verdict

### 1. 768-dim `vec_memory` lock-in

**PROBLEM:** `vec_memory float[768]` hardcoded in schema. Switching to 1024-dim models silently drops vectors while README only warns in prose.

**WHY IT MATTERS:** Users who swap embedding models get lexical-only search with no hard failure — worst outcome for a hybrid memory product.

**FIX:** `schema_meta vec_dimension` column. Migration v11 DROP+recreate `vec_memory` with configurable N. `engram migrate-embeddings --target-model`. Fail loudly at startup when model dimension ≠ stored dimension.

---

### 2. Zero ADR history

**PROBLEM:** No `docs/decisions/`. No record of why SQLite+FTS5+sqlite-vec was chosen over Chroma/Neo4j/Letta markdown.

**WHY IT MATTERS:** A memory system whose core value is cross-session continuity cannot explain its own architectural continuity. Future contributors re-litigate every quarter.

**FIX:** ADR series 0001–0005: storage backend, embedding lock, committee workflow opt-in, MCP tool taxonomy, heuristic-vs-LLM extraction policy.

---

### 3. Committee workflow is theater

**PROBLEM:** `check_decision_allowed()` no-ops when `current_phase is None`. CLI `add decision` bypasses workflow entirely. Agents can log decisions without ever calling `memory_init_session`.

**WHY IT MATTERS:** Committee workflow is Engram's differentiator vs flat-search tools, yet it is trivially bypassed on both MCP and CLI.

**FIX:** Enforce gates on all write paths. Collapse to 2 phases (discuss, decide) by default. `--force-bypass` only with audit log.

---

### 4. MCP tool sprawl without MemPalace capability depth

**PROBLEM:** 38 flat `memory_*` tools with overlapping surfaces. MemPalace ships 29 domain-organized tools including `kg_query`, `kg_invalidate`, `kg_timeline`, `traverse_graph` — none of which Engram has.

**WHY IT MATTERS:** Tool count is not capability. Agents drown in undifferentiated tool list while lacking temporal KG, entity traversal, and structured invalidation.

**FIX:** Restructure into 4 namespaces (recall, capture, session, maintain) with ≤15 top-level tools. Add `memory_kg_add/query/invalidate`. Deprecate duplicate `add_*` tools.

---

### 5. SQLite single-file scalability ceiling

**PROBLEM:** All memory in one `~/.engram/memory.db`. No connection pooling, no partition by project. `vec_memory` row count scales O(n) with every mistake logged.

**WHY IT MATTERS:** Fine for solo dev, fatal for team/org memory. Unbounded growth vector.

**FIX:** `StorageBackend` ABC (mirror `mempalace/backends/base.py`). Document soft limits (50k items). Optional Postgres+pgvector or Chroma for team mode. Partition `vec_memory` by `project_id`.

---

### 6. Temporal invalidation absent

**PROBLEM:** No `valid_from`, `valid_to`, `superseded_by`, or invalidation API. When a skill is updated, old content remains fully searchable with equal weight.

**WHY IT MATTERS:** Engram will serve stale API-version advice alongside current fixes, poisoning agent context. This is the #1 recall-quality killer for long-lived engineering memory. Zep Graphiti's core claim is bi-temporal invalidation.

**FIX:** `memory_facts` table with `(subject, predicate, object, valid_from, valid_until, source_item_id)`. Implement `memory_invalidate` and filter search by `as_of=now`. Wire `superseded_by` on skill/pattern UPDATE paths.

---

### 7. No MemoryProvider interface

**PROBLEM:** All storage/search/MCP handlers call `database.py` directly. Odysseus has `MemoryProvider` ABC. MemPalace has `BaseBackend`/`BaseCollection`.

**WHY IT MATTERS:** Cannot compose native SQLite with external Mem0/Zep backends. Cannot swap storage for tests. Every new feature bloats `database.py` past 1500 lines.

**FIX:** `src/providers/base.py` with `MemoryProvider` protocol. Wrap current logic in `NativeMemoryProvider`. Route MCP handlers through provider registry.

---

### 8. Entity linking absent

**PROBLEM:** Tags only. No entities table, no entity-resolution on write, no cross-memory graph. Mem0 v3 extracts entities on ADD. MemPalace has `kg_add`/`kg_query` triples.

**WHY IT MATTERS:** "API mismatch in lzp-pos" and "parameter bug in POS API" are duplicate vector blobs, not the same entity. Search recall degrades with synonym drift.

**FIX:** `entities` + `entity_links` tables. Lightweight NER on `memory_add`. Boost RRF for items sharing `entity_id`. Expose `memory_link_entity` MCP tool.

---

### 9. Heuristic-first extraction vs Mem0 LLM ADD

**PROBLEM:** `capture.py` is explicitly no-LLM — regex only. Mem0 v3 and Mastra use ADD-only LLM extraction at ingest with 94.8%+ LongMemEval.

**WHY IT MATTERS:** Heuristics catch "error/fixed/workflow" keywords but miss implicit lessons. Engram optimizes zero-LLM offline use at the cost of capture quality — the exact metric competitors benchmark on.

**FIX:** LLM extraction default when Ollama available. Regex fallback offline only. Mem0-style ADD prompt. Unify `memory_suggest_capture` and `memory_auto_extract`. Log extraction precision for eval.

---

### 10. No sleep-time consolidation

**PROBLEM:** Letta runs background sleep-time jobs. Engram only consolidates on explicit invocation. Consolidation fingerprint cache skips rescans when unchanged — stale clusters persist indefinitely.

**WHY IT MATTERS:** Memory systems that only consolidate when asked accumulate bloat until someone runs maintenance.

**FIX:** `engram sleep` command (or Cursor hook on session-end): find candidates, LLM merge top clusters, mark superseded invalid, GC archive. Weekly cron default. Expose `memory_sleep` MCP tool.

---

## Cross-Agent Debates

| Debate | Positions | Resolution |
|--------|-----------|------------|
| Service layer vs provider ABC | SR: `memory_ops` now. ARCH: `providers/base.py` first. | `memory_ops` immediate fix; StorageBackend ABC phase 2 |
| Committee workflow | ARCH: theater. SR: CLI bypass confirmed. QA: no tests for gates. | **Unanimous:** broken until gates enforced on all write paths |
| MCP tool reduction | ARCH: 38→15 tools. QA: shrink without contract tests = breakage. | Contract tests FIRST, then deprecate duplicate `add_*` in v0.2 |
| Heuristic vs LLM capture | ARCH: Mem0 wins on quality. SR: offline is intentional. | LLM default when Ollama up; regex offline fallback. QA demands precision logging before Mem0 parity claims |
| Backend scalability | ARCH: Chroma/Postgres now. SR+QA: fix bugs before new backends. | Document 50k-item soft limit; backend ABC design-only until benchmark suite exists |

---

## Comparison Matrix

| Dimension | Engram | Odysseus | MemPalace | MemGPT/Letta | Mem0 v3 | Zep/Graphiti | Mastra | cursor-brain |
|-----------|--------|----------|-----------|--------------|---------|--------------|--------|--------------|
| **Storage** | SQLite single file | JSON + ChromaDB | ChromaDB + SQLite KG | Pickle + FAISS / git markdown | Cloud vector + entity store | Neo4j bi-temporal KG | Postgres/LibSQL/Mongo | SQLite single file |
| **Retrieval** | FTS5 + vec RRF + utility | BM25 + vector hybrid | Chroma + BM25 + optional LLM rerank | LLM-managed tiers | Vector + BM25 + entity RRF | Vector + BM25 + graph traversal | 4-layer + observational compression | FTS5 + WASM embeddings |
| **Embeddings** | Ollama local (768-dim locked) | fastembed ONNX or HTTP | Pluggable, dim-checked | OpenAI ada-002 | Cloud managed | Cloud/local | Gemini Flash (OM layer) | HuggingFace WASM |
| **LongMemEval** | **None published** | None | 96.6%+ R@5 (500q) | None published | 94.8% | SOTA claimed (self-reported) | 94.87% | None |
| **LoCoMo** | **None published** | None | Yes | None | 91.6% | Yes | Yes | None |
| **Internal benchmark** | 20 curated queries | None | Extensive suite | Paper only | Published | Self-reported | Published | None |
| **MCP tools** | 38 (flat, overlapping) | 1 (`manage_memory`) | 29 (domain-organized) | N/A | 9 (cloud) | Built-in | Framework SDK | None |
| **Capture** | Heuristic regex (LLM optional) | LLM auto-extract every 4 msgs | Verbatim, no extraction | LLM function calls | LLM ADD-only | Graph ingestion | Observer + Reflector agents | Manual |
| **Temporal invalidation** | **None** | None | KG validity windows | Git history | Partial | **Core feature** | None | None |
| **Entity linking** | Tags only | Category heuristics | KG triples | N/A | Entity store on ADD | Graph edges | N/A | None |
| **Consolidation** | Manual + doctor | LLM audit/tidy | LLM rerank optional | Sleep-time compute | Cloud-managed | Graph invalidation | Observational compression | None |
| **CLI/MCP parity** | **Broken** (dedup, workflow, enums) | REST + MCP aligned | CLI + MCP + hooks | CLI only | SDK + MCP | SDK + MCP | TS SDK only | None |
| **Test coverage** | 18 test files, 15+ modules untested, MCP handlers untested | 400+ tests | Extensive benchmark CI | Minimal local | Cloud-tested | Unknown | Framework tests | None |
| **CI quality gate** | **5/20 queries, fail_under=0** | Unknown | Benchmark-gated | None | Cloud CI | Unknown | CI | None |
| **Privacy** | Fully local | Self-hosted | Local-first | Local/cloud | **Cloud required** | Cloud/self-host | Cloud LLM for OM | Fully local |
| **Scope** | Dedicated memory MCP | Full AI workspace | Dedicated memory MCP | Full agent runtime | Managed memory layer | Temporal KG platform | TS agent framework | Minimal SQLite memory |
| **Provider abstraction** | **None** (direct DB calls) | `MemoryProvider` ABC | `BaseBackend` ABC | Agent-managed | API/SDK | SDK | Framework memory API | None |

---

## Engram's Defensible Strengths (only what survives scrutiny)

These are real. They are not enough to offset the problems above, but they are genuine differentiators:

1. **Fully offline, no cloud account** — Mem0, Mastra OM, Zep cloud all require external services. Engram runs on a laptop with Ollama. cursor-brain matches this but lacks lifecycle hooks and benchmarks.

2. **FTS5-weighted hybrid for code** — Exact symbol/API name matching (`getConnection`, `EngramError`) is lexical, not semantic. Engram's FTS5 leg is the correct primary channel for engineering memory. Competitors that are vector-heavy miss identifier recall.

3. **Typed memory schema** — mistakes/patterns/skills/decisions are structured artifacts, not raw chat blobs. MemPalace stores verbatim text; Odysseus stores flat user facts. Engram's schema maps to how engineers actually learn.

4. **Zero LLM cost on capture (when heuristic path used)** — No extraction API call per message. Speed advantage for high-frequency capture, if capture quality is acceptable.

5. **Cursor/Antigravity lifecycle integration** — Bootstrap, hooks, skill sync, adaptive engagement modes. No competitor matches this IDE-native depth except DevContext (unmaintained, cloud-dependent).

---

## Top 10 Prioritized Action Items

Ranked by **severity first**, then effort.

| # | Severity | Action | Owner | Effort |
|---|----------|--------|-------|--------|
| 1 | **Critical** | Fix CI: remove `--limit 5`, assert R@5≥0.90, set `fail_under≥60`, mock embeddings in CI | QA | 1–2 days |
| 2 | **Critical** | Enforce workflow + dedup gates on CLI write paths (stop committee theater) | SR + ARCH | 2–3 days |
| 3 | **Critical** | Extract `src/memory_ops.py` service layer; eliminate CLI/MCP CRUD duplication | SR | 3–5 days |
| 4 | **High** | Parametrized MCP handler contract tests for all 30+ tools | QA | 3–5 days |
| 5 | **High** | Add temporal invalidation: `valid_until` + `memory_invalidate` + `as_of` search filter | ARCH | 1 week |
| 6 | **High** | Publish LongMemEval adapter benchmark — honest score even if lower than MemPalace | QA | 1 week |
| 7 | **High** | Single `ITEM_TYPES` registry; fix session usage ranking bug | SR | 2–3 days |
| 8 | **Medium** | ADR series 0001–0005 in `docs/decisions/` | ARCH | 2 days |
| 9 | **Medium** | Expand benchmark to ≥100 queries with multi_hop/abstention/conversation types | QA | 1 week |
| 10 | **Medium** | `engram migrate-embeddings` + loud dimension mismatch at startup | ARCH | 3–5 days |

---

## Engram Search Receipt

**Session ID:** `rage-audit-2026-06-06`

**Decisions persisted:** 24 (7 SR + 7 QA + 10 ARCH)

**Patterns persisted:** 6
- `SR-AUDIT: Dual-surface drift`
- `SR-AUDIT: table_map whack-a-mole`
- `QA-AUDIT: Green CI, broken retrieval`
- `QA-AUDIT: Toy benchmark false confidence`
- `QA-AUDIT: MCP surface untested`
- `ARCH-AUDIT: Monolithic database.py god-module`

**Mistakes persisted:** 1
- Session usage not ranked (`search.py` `table_map` missing `session`)

**Transcripts:** 6 (1 bootstrap + 5 cross-agent debates)

**Retrieve findings:**
```bash
engram get-session --id rage-audit-2026-06-06
engram search engram-audit --no-project -n 50
```

---

*Generated by Engram Rage Audit Committee, 2026-06-06. Findings persisted to Engram DB before this document was written.*
