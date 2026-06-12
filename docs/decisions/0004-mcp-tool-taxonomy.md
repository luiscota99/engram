# ADR-0004: MCP Tool Taxonomy and Namespace Reduction Plan

**Date:** 2026-06-06  
**Status:** Accepted  
**Deciders:** Engram core team

---

## Context

The MCP server exposes **38 flat `memory_*` tools** covering recall, capture, session/committee, maintenance, and codebase indexing. Overlap exists between `memory_add` and `memory_add_mistake|pattern|skill`, and between `memory_suggest_capture` and `memory_auto_extract`. Agents face choice paralysis; tool-list token cost grows with every feature. Competitors organize tools by capability (recall vs capture vs graph) with fewer top-level entries.

## Decision

**Current taxonomy (v1.1, 38 tools)** — grouped by intent:

| Namespace | Count | Tools |
|-----------|------:|-------|
| **recall** | 6 | `memory_search`, `memory_recent`, `memory_read_item`, `memory_find_similar`, `memory_list`, `memory_list_pinned` |
| **capture** | 10 | `memory_add`, `memory_add_mistake`, `memory_add_pattern`, `memory_add_skill`, `memory_add_conversation`, `memory_add_prompt`, `memory_suggest_capture`, `memory_auto_extract`, `memory_record_usage`, `memory_pin` / `memory_unpin` |
| **session** | 9 | `memory_init_session`, `memory_add_transcript`, `memory_add_decision`, `memory_get_role`, `memory_get_session`, `memory_check_workflow_state`, `memory_advance_phase`, `memory_session_review`, `memory_sleep` |
| **maintain** | 13 | `memory_consolidate_skills`, `memory_merge_entries`, `memory_suggest_consolidations`, `memory_invalidate`, `memory_gc`, `memory_embedding_status`, `memory_health`, `memory_stats`, `memory_export_skill`, `memory_sync_skills`, `memory_index_file`, `memory_query_codebase`, `memory_get_stale_files` |

**Reduction plan (v1.2 target, ≤15 top-level tools):**
1. Collapse capture into `memory_add` (type param) — deprecate `memory_add_mistake|pattern|skill`.
2. Merge `memory_suggest_capture` + `memory_auto_extract` → `memory_extract`.
3. Group maintain ops under `memory_maintain` with action enum.
4. Add `memory_kg_query` / `memory_kg_invalidate` for temporal facts (new capability, net −3 after deprecations).
5. Keep deprecated tool names as aliases for one release cycle.

## Consequences

**Positive:**
- Documented taxonomy helps agents pick the right tool on first try.
- Reduction plan cuts MCP `tools/list` payload ~60% while adding KG queries.
- Unified `memory_add` aligns CLI and MCP write surfaces.

**Negative / Tradeoffs:**
- Deprecation cycle requires dual handlers and changelog noise.
- Fewer explicit tools means richer JSON schemas on survivors.
- 38 → 15 is breaking for hard-coded agent prompts referencing old names.

**Risks:**
- Premature deprecation before KG tools ship — phase deprecations only after v1.2 KG MVP.

## Related Decisions

- ADR-0003: Committee workflow (session namespace).
- ADR-0005: Extraction policy (`memory_extract` merge target).

---

*Accepted 2026-06-06.*
