# Measuring fit and whether Engram helped

This guide answers two questions:

1. **Is Engram a good fit?** — For your memories and how you query, does search surface the right rows?
2. **Did it help on this task?** — Did memory change behavior or avoid a bad path, not just add context?

Default assistant UX can stay **silent** (LIGHT mode). Measurement uses **offline benchmarks**, **optional logs**, and **short self-reports** at session end.

---

## A. Good fit (your corpus, not one chat)

**Fit** means: when useful memory exists, hybrid search finds it with your real phrasing.

| Step | Action |
|------|--------|
| 1 | Freeze a **DB snapshot** (copy your `memory.db` to a dated path; do not commit if it contains private text). |
| 2 | Write **5–20 natural queries** you would run after real use. |
| 3 | For each query, set **`expected_type`** + **`expected_item_id`** (or `expected_title_contains`) — see [evals/README.md](../evals/README.md). |
| 4 | Run `python benchmarks/engram_retrieval_bench.py --queries your.json --no-seed` with `ENGRAM_DB_PATH` pointing at the snapshot. |
| 5 | **Interpret:** Strong **R@5** / **MRR** on *your* labels → good corpus and query fit. Weak scores → improve titles/tags, embeddings (Ollama), or ranking — not necessarily “wrong tool.” |

**Golden-memory smoke test:** Add one unmistakable **skill**, search with a **paraphrase** not in the text. If it is not in the top 5, fix the stack before trusting session-level “help.”

**CI regression:** The seeded [test_queries.json](../benchmarks/test_queries.json) guards **algorithm** quality; it does not replace steps 1–5 for *your* data. See [benchmarks/BENCHMARKS.md](../benchmarks/BENCHMARKS.md).

**Summary:** **R@5 on labeled queries = fit** of search to your data.

---

## B. Helped on this task (session-level)

Causal proof is hard; use a **Session Help Score** in **under 30 seconds** after non-trivial work.

| Score | Meaning |
|-------|--------|
| **0** | No search, or results ignored; memory played no role. |
| **1** | Search ran; relevant hits but **no** change to what you did. |
| **2** | Memory **changed a concrete decision** (file, approach, or avoided a listed mistake). |
| **3** | **Large** effect — avoided a dead end, followed a **skill** end-to-end, or saved substantial rework. |

**Rule of thumb:** **≥ 2** ⇒ “Engram helped this session.”

### Self-report at session end

`memory_session_review` (MCP) and `engram suggest-capture` (CLI) append the same **Engram influence (0–3)** prompt. Treat it as **habit and traceability**, not ground truth — pair with Fit Check metrics.

### CLI log (optional)

```bash
engram session-help --score 2 --note "Followed deploy checklist skill" --task "release-123"
```

Appends one JSON line to `ENGRAM_SESSION_HELP_LOG` (default: `~/.engram/session-help.jsonl`).

### Proxies over time

- Often **≥ 2** but low labeled R@5 → shallow use or luck; tighten corpus.
- High R@5 but rare **≥ 2** → agents may not be **acting** on hits (rules, hooks, prompts).

---

## C. Optional silent instrumentation

### Search audit log

If `ENGRAM_AUDIT_LOG` is set to a file path, each hybrid **`search()`** call (MCP `memory_search`, CLI `engram search`) appends one JSON line: timestamp, query, source (`mcp` / `cli`), `semantic_status`, and top hits (type, id, title prefix). Internal calls (e.g. duplicate check inside `memory_session_review`) skip logging.

Use this for weekly rollups without chat UI.

---

## D. Explicit disclosure (when memory changed behavior)

When you **act on** a retrieved memory, you may add **one short line** (not every search):

| Event | Disclose? | Example |
|-------|-----------|---------|
| Search only, no action | **No** | — |
| Chose approach because of a skill/pattern | **Yes** | `Engram: applied skill "Deploy checklist"` |
| Avoided a path because of a logged mistake | **Yes** | `Engram: avoided flood-fill on alpha mistake` |

### Public vs private (`ENGRAM_DISCLOSURE`)

| Value | Use when | Commits / PRs |
|-------|-----------|----------------|
| `private` (default) | Internal repo; IDs are acceptable | `Engram-Refs: skill:12` OK |
| `public` | OSS or public GitHub | **No numeric IDs.** Titles or neutral slugs only — never `id:12` in public text |

If visibility is unclear, assume **public** (titles only).

---

## E. Quick reference

| Question | Evidence |
|----------|----------|
| Does search find the right row when it exists? | Labeled R@5 / MRR ([benchmarks](../benchmarks/BENCHMARKS.md), [evals](../evals/README.md)) |
| Did memory matter this time? | Session Help Score **≥ 2**, or `session-help` log |
| Did a search happen? | `ENGRAM_AUDIT_LOG` JSONL (optional) |
