---
name: Ranking type-inference tuning
overview: Fix skill-vs-prompt type inference collisions by scoring IDE/rules-file cues as prompt before generic "how to" skill hints, reduce TYPE_MATCH_BOOST 20→15, add unit tests, and run the retrieval benchmark to guard regressions.
todos:
  - id: infer-tier
    content: Add IDE prompt tier + explicit type-check order in infer_type_from_query (ranking.py)
    status: pending
  - id: boost-15
    content: Set TYPE_MATCH_BOOST to 15.0
    status: pending
  - id: tests-ranking
    content: Add tests/test_ranking.py for infer_type_from_query cases
    status: pending
  - id: bench-regress
    content: Run engram_retrieval_bench and fix if R@5 regresses
    status: pending
  - id: docs-benchmarks
    content: Note type_inference IDE-vs-how-to behavior in BENCHMARKS.md
    status: pending
isProject: false
---

# Ranking: prompt vs skill type inference tuning

## Problem

[`infer_type_from_query`](file:///Users/luismiguel/Desktop/AI/engram/src/ranking.py) uses a single ordered keyword map. **`skill` includes `"how to"`** and appears **before** `prompt`, so queries that mention both “how to” and Cursor/IDE rules material (e.g. `.mdc`, cursor rules) infer **`skill`** and receive [`TYPE_MATCH_BOOST`](file:///Users/luismiguel/Desktop/AI/engram/src/ranking.py) (currently **20.0**), drowning a high-quality **prompt** hit (e.g. “Cursor Rules Maker” in a real DB). This is the “type inference collision” Antigravity quantified.

## Approach

### 1. High-precision “IDE / rules file” prompt tier (before skill)

In `infer_type_from_query`, **first** check `query_lower` for **prompt-specific** substrings that indicate Cursor/IDE configuration (narrow list to avoid bare `rules` false positives):

- File/paths: `.mdc`, `.cursorrules`, `.cursor/rules`, `/.cursor/` (as needed)
- Phrases: `cursor rules`, `cursor rule`, `cursorrules` (one word, common in prose)

If any match → return **`"prompt"`** immediately (do not run the generic `"how to"` skill branch).

Optional small addition aligned with Antigravity: **`mdc`** only when clearly path-like — e.g. require `.mdc` **or** regex word boundary `\bmdc\b` — so random tokens do not flip type.

### 2. Broaden normal `prompt` keywords (second pass)

After the IDE tier fails, keep the existing loop but **extend** the `prompt` entry with safe tokens Antigravity mentioned, **without** adding naked `rules` alone (too noisy). Examples: `rules.mdc`, `cursor rules` (may already be covered by tier 1 — avoid duplicate logic; can live only in tier 1).

**Ordering:** Keep **mistake** and **pattern** before **prompt** if their keywords are still high-signal; insert **IDE prompt tier** immediately **before** the **`skill`** block (conceptually: mistake → pattern → **IDE prompt** → skill → conversation → generic prompt). Concretely: implement as “early return for IDE prompt” then the existing dict loop with **`skill` before `prompt`** in the dict, or a small ordered list of `(type, keywords)` so order is explicit in one place.

### 3. Reduce type-match boost

Set **`TYPE_MATCH_BOOST` from `20.0` to `15.0`** in [`ranking.py`](file:///Users/luismiguel/Desktop/AI/engram/src/ranking.py) so inferred type does not overwhelm strong cross-type semantic/lexical scores.

### 4. Tests

Add [`tests/test_ranking.py`](file:///Users/luismiguel/Desktop/AI/engram/tests/test_ranking.py) (or extend an existing test module if you prefer one file) covering `infer_type_from_query`:

| Query (representative) | Expected inferred type |
|------------------------|-------------------------|
| `how to run a database migration safely` | `skill` (regression guard; same intent as [q011](file:///Users/luismiguel/Desktop/AI/engram/benchmarks/test_queries.json)) |
| `how to write cursor rules in .mdc` | `prompt` |
| `debugging steps for a bug stack trace` | `skill` (q013-style) |

Optional fourth case: query with `.cursorrules` only → `prompt`.

### 5. Regression check

Run `python benchmarks/engram_retrieval_bench.py` (or `engram retrieval-benchmark` if that wraps it) on the default seeded suite so **R@5 / per-category** stay stable; fix ordering/keywords if any `type_inference` row drops.

### 6. Docs (light touch)

Update [benchmarks/BENCHMARKS.md](file:///Users/luismiguel/Desktop/AI/engram/benchmarks/BENCHMARKS.md) `type_inference` row with one line: IDE cues (`.mdc`, cursor rules, etc.) bias toward **prompt** before generic `how to` → **skill**.

## Files to touch

- [`engram/src/ranking.py`](file:///Users/luismiguel/Desktop/AI/engram/src/ranking.py) — `TYPE_MATCH_BOOST`, refactor `infer_type_from_query`
- [`engram/tests/test_ranking.py`](file:///Users/luismiguel/Desktop/AI/engram/tests/test_ranking.py) — new tests
- [`engram/benchmarks/BENCHMARKS.md`](file:///Users/luismiguel/Desktop/AI/engram/benchmarks/BENCHMARKS.md) — short methodology note

## Out of scope (unless you want it)

- Adding a new seeded prompt (“Cursor Rules Maker”) + `test_queries.json` entry — useful for CI lock-in of this exact product scenario; can be a follow-up if the seeded DB should model it.
