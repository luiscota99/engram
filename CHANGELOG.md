# Changelog

All notable changes to Engram will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `engram bootstrap --omit-project-integration` and sentinel file `.omit-agent-integration` (project root) to skip Cursor rules + Antigravity `instructions.md` while still initializing the database and MCP when applicable.
- README guidance on explicitly asking for retrieval (Agent Integration).
- Hybrid search merges semantic vs lexical rankings with reciprocal rank fusion (RRF); FTS query tokenization aligns with BM25/ranking tokenizer; tests in `tests/test_rrf.py`.
- In-process LRU cache for query embeddings (`src/embeddings.py`, 256 entries) — the Ollama HTTP round-trip dominated search latency (~85% in profiling); repeated queries in a long-lived MCP session now skip it. `clear_embedding_cache()` resets it.
- Schema parity regression test (`tests/test_migrations.py::test_fresh_schema_matches_migrated_schema`) — fails if `SCHEMA_SQL` and the migration chain diverge.

- **Pluggable embedding backends** — `ENGRAM_EMBED_URL` now selects an OpenAI-compatible `/v1/embeddings` endpoint (with `ENGRAM_EMBED_API_KEY`); unset keeps local Ollama; `disabled` turns embeddings off cleanly (short-circuits before any HTTP).
- **Flexible embedding dimensions** — `engram migrate-embeddings --target-model <model>` probes the model's real output dimension and rebuilds `vec_memory` at the new width when it differs (vectors are regenerated from FTS content). Write paths validate against the live `schema_meta.vec_dimension` instead of a hardcoded 768.
- **`src/config.py`** — every environment variable Engram reads, documented and resolved in one module.
- **MCP elicitation (spec 2025-06-18+)** — destructive tools (`memory_gc` non-dry-run, `memory_sleep`) ask the user for confirmation via `elicitation/create` when the client advertises the capability; clients without it keep the previous behavior.
- **Tokens-per-query in benchmarks** — `longmemeval_bench` and `engram_retrieval_bench` report `context_tokens` per query and `avg_context_tokens` in the aggregate (the 2026 memory-benchmark comparison axis alongside accuracy).
- **Claude Code skill** — `engram claude-skill` installs `claude-skills/engram-memory/SKILL.md` into `~/.claude/skills/` so Claude Code searches memory before non-trivial tasks and captures lessons afterward.
- **`engram install`** — one-shot setup: detects Cursor / Claude Code / Antigravity on the machine and wires all global integrations (Cursor MCP config, Claude skill, `~/.gemini/AGENTS.md` snippet); `--all` forces every integration.
- **`engram import-claude-memories`** — imports Claude Code's file-based memories (`~/.claude/**/memory/*.md`) into Engram, idempotent by content hash, so native Claude memories become searchable across every tool.
- **Capture→reuse metric** — `engram health` (and the MCP health tool) now report what share of memories captured 30+ days ago were ever used again, per type and overall, with a recommendation when reuse is low. This is the capture-quality signal.
- **Real LongMemEval retrieval eval** — `longmemeval_bench.py --oracle-file longmemeval_oracle.json` ingests all haystack sessions into one corpus (~950 sessions) and grades session-level Recall@k/MRR against `answer_session_ids`, per question type, with tokens-per-query. First full run (2026-07-06): session-level R@5 0.538 / MRR 0.442 over 940 sessions, 218 ms and 374 tokens per query, fully local; results and caveats published in `benchmarks/BENCHMARKS.md`.
- **Leaner MCP search injection** — `memory_search` default limit 10→5 with rank-aware snippets (500 chars for the top hit, 150 below): ~34% fewer context tokens per search on a real corpus (845→560) while the top hit carries 3× more detail; `estimate_context_tokens` in benchmarks now mirrors this injected format instead of raw row sizes.
- Connection reuse on the search hot path — one connection per search (was 8), plus a `connection_scope` helper for multi-step operations.
- sqlite-vec pinned to >=0.1.9 (proper DELETE/space reclamation in vec0 tables).
- **Batched + deferred embeddings** — `embed_batch` (one HTTP round-trip per 16 docs with per-item fallback), `ENGRAM_DEFER_EMBED=1` write-fast mode (FTS rows land instantly, vectors fill in via batched `engram reembed`), used by the LongMemEval ingest; corpus ingestion is now crash-resumable.
- **Explicit-date ranking boost** — queries mentioning a date ("in May 2023", "2024-03") boost items whose date matches by ISO prefix. A directional heuristic ("first"/"most recent") was benchmarked on LongMemEval and removed (no R@5 effect, slightly negative MRR) — see benchmarks/BENCHMARKS.md.
- **Turn-window chunked ingestion** — `create_conversation_chunked` indexes overlapping turn-windows as sibling rows so buried single-sentence evidence gets its own vector; `--chunked` flag on the oracle benchmark.
- **Reuse-aware capture** — `suggest_capture` consults per-type reuse rates (`get_reuse_rates`) and warns when a suggested type historically never gets retrieved again.

### Fixed

- **Embedding normalization (schema v12)** — Ollama's legacy `/api/embeddings` returns unnormalized vectors while the newer `/api/embed` returns unit vectors; mixing them under euclidean KNN silently partitioned the vector index (queries only matched same-endpoint documents). All vectors are now L2-normalized in code, and migration v12 rescales existing stored vectors in place — no re-embedding needed.
- Fresh databases were missing schema v11 objects (`memory_facts` table, `superseded_by` columns, `codebase_knowledge.file_mtime`) because they were added only in migrations, never in the baseline `SCHEMA_SQL`. `memory_invalidate` with a reason crashed on new installs.
- `engram suggest-consolidate` and `run_sleep` crashed (or reported `clusters_found == 2` on an empty database) — both treated the `(clusters, skip_reason)` tuple from `find_consolidation_candidates` as the cluster list.
- Flaky search performance test: the 500ms budget included a live Ollama embedding call (800ms+ on a cold model load); the query embedding is now stubbed so the test measures the SQL/ranking path it was written to guard.
- `engram doctor --repair` built the orphaned-tags DELETE by string interpolation; now parameterized.
- Silent `except: pass` handlers in `temporal.py`, `maintenance.py`, and `database.py` now log (one of them had already hidden the fresh-install schema bug).

## [1.1.0] - 2026-04-21

### Added
- Unified `memory_add` MCP tool — consolidates `memory_add_mistake`, `memory_add_pattern`, and `memory_add_skill` into a single dispatching tool with explicit `type` field
- `memory_suggest_capture` MCP tool — heuristic-driven auto-drafting of memory entries from task description and outcome; no LLM required
- `engram browse` CLI command — interactive curses-based TUI for browsing and searching memory entries without leaving the terminal
- `engram suggest-capture` CLI command — surface capture suggestions directly from the CLI
- Modular CLI architecture: `src/cli/` package with 7 focused command modules (`memory`, `codebase`, `sync`, `bootstrap`, `maintenance`, `session`, `tools`)
- `src/capture.py` — standalone heuristic capture engine with domain inference, signal detection, and structured draft generation
- `src/browse.py` — curses TUI browser with search, type-filter, and detail view
- Three-mode Cursor rule system: **LIGHT** (default, one search at session start), **FULL** (auto-escalates on complexity signals), **COMMITTEE** (explicit opt-in only via `@engram committee`)
- `SearchResults` list subclass in `src/search.py` to safely carry a `semantic_status` attribute on search result lists
- `phases` and `phase_requirements` columns added to the `workflows` table (schema + migration v8)
- Comprehensive test suite: `tests/test_capture.py` (23 tests), `tests/test_cli_commands.py` (12 tests), `tests/test_workflow.py` (16 tests)
- **Retrieval benchmark hardening (v1.1.0):** `benchmarks/grading.py` with id-based ground truth (`expected_type` + `expected_item_id`) and title fallback; `benchmarks/test_queries.json` aligned to the seed; optional `--failure-detail` and `failed_query_ids` + `top_hits_detail` in JSON on `engram_retrieval_bench.py`
- `evals/` — optional labeled real-DB evaluation (`evals/README.md`, `real_queries.json.example`); not run in CI by default
- `benchmarks/inject_noise.py` — reproducible distractor stress test; interpretation notes in `benchmarks/BENCHMARKS.md`

### Fixed
- MCP schema mismatch: `memory_add_skill` tool definition used `trigger_desc` instead of `trigger` — corrected to match the handler
- `advance_phase` bug: phase advancement previously ignored custom workflows stored in the `workflows` table; now correctly loads phases and role requirements via `_load_workflow_phases`
- `init_session_state` read-before-commit bug: calling `get_session_state` inside the same write transaction made the inserted row invisible; moved the read to after the write connection commits
- `SearchResults` attribute error: `search()` tried to set `.semantic_status` on a plain `list`, causing `AttributeError` when semantic search was skipped — fixed with `SearchResults(list)` subclass
- N+1 query in `get_project_affinities`: replaced per-result queries with a single batched `WHERE (item_type, item_id, project_id) IN (VALUES ...)` query
- Wrong GitHub URL in the bootstrap command
- MCP codebase tools (`memory_index_file`, `memory_query_codebase`, `memory_get_stale_files`) no longer spawn subprocess calls — reimplemented with direct Python database calls

### Changed
- CLI entry point updated from `src.cli_legacy` to `src.cli:main` (the new modular package)
- Adaptive Cursor rule simplified from 122 lines to ~50 lines with clearer mode definitions
- Committee workflow is now strictly opt-in: removed automatic escalation triggers; only activates on explicit `@engram committee` / `use committee` / `committee workflow` phrases
- `cli.py` renamed to `cli_legacy.py` (deprecated, kept for reference only during transition)

## [1.0.0] - 2026-04-19

### Added
- SQLite database with FTS5 full-text search
- CLI tool (`engram`) with commands: search, recent, add, list, link-pattern, stats, init, seed
- MCP server for Cursor IDE and Claude Desktop integration
- 8 MCP tools: memory_search, memory_recent, memory_add_mistake, memory_add_pattern, memory_add_skill, memory_add_conversation, memory_list, memory_stats
- Cursor rule file for automatic agent integration
- Docker support (Dockerfile + docker-compose.yml)
- Install script with shell function setup
- 4 memory types: mistakes, patterns, skills, conversations
- Tag system with cross-cutting labels
- WAL mode for concurrent reads
- Seed module with sample data from real development sessions
