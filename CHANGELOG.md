# Changelog

All notable changes to Engram will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `engram bootstrap --omit-project-integration` and sentinel file `.omit-agent-integration` (project root) to skip Cursor rules + Antigravity `instructions.md` while still initializing the database and MCP when applicable.
- README guidance on explicitly asking for retrieval (Agent Integration).
- Hybrid search merges semantic vs lexical rankings with reciprocal rank fusion (RRF); FTS query tokenization aligns with BM25/ranking tokenizer; tests in `tests/test_rrf.py`.

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
