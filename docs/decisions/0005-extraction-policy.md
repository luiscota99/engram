# ADR-0005: Heuristic vs LLM Extraction Policy

**Date:** 2026-06-06  
**Status:** Accepted  
**Deciders:** Engram core team

---

## Context

Memory quality at ingest determines retrieval ceiling. Mem0 v3 and Mastra use LLM-based ADD extraction at write time (~95% LongMemEval R@5). Engram historically favored **zero-LLM** heuristics in `capture.py` (regex keyword scoring) for offline/air-gapped use, with optional LLM in `auto_extract.py`. The two paths (`memory_suggest_capture` vs `memory_auto_extract`) duplicated logic and confused agents.

## Decision

1. **Default policy:** LLM extraction when Ollama (or configured chat provider) is available; **regex/heuristic fallback only when offline**.
2. **Merge path:** `extract_from_messages` runs LLM first, merges regex candidates, caps at 4 entries, deduplicates by summary hash.
3. **Human-in-the-loop unchanged:** All extracted payloads require user approval before `memory_add` — no autonomous writes.
4. **Engineering tasks:** `suggest_capture` heuristics remain for structured task/outcome capture; results are combined with message extraction in `extract_from_task`.
5. **Eval logging:** Record `llm_used`, `regex_used`, and candidate count for precision/recall measurement against labeled eval sets.

## Consequences

**Positive:**
- Capture quality approaches competitor benchmarks when LLM is available.
- Offline installs still function with regex-only extraction.
- Single mental model: extract → draft → approve → add.

**Negative / Tradeoffs:**
- LLM extraction adds latency and Ollama dependency for best capture quality.
- Regex fallback misses implicit lessons (synonym drift, unstated constraints).
- Extraction precision must be monitored — false positives pollute search.

**Risks:**
- LLM may over-extract boilerplate — mitigate with MAX 2 entries prompt and significance guardrails in `capture.py`.
- Cost/token use on long sessions — truncate to last 6 messages before extract.

## Related Decisions

- ADR-0004: MCP tool taxonomy (`memory_extract` consolidation).
- ADR-0002: Embeddings (separate from chat extraction model).

---

*Accepted 2026-06-06.*
