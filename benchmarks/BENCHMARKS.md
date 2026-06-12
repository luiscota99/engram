# Engram Retrieval Benchmarks

Measures search quality of Engram's hybrid retrieval (FTS5 + `sqlite-vec` semantic) without requiring external infrastructure or ground-truth conversations.

---

## Methodology

### Query Dataset

`test_queries.json` contains **100** curated queries across eight difficulty categories:

| Category | Description | Expected winner |
|---|---|---|
| `exact_error` | Query uses verbatim terms from the stored entry | FTS5 |
| `semantic_similar` | Query paraphrases the entry (different vocabulary, same concept) | Semantic |
| `tag_filter` | Query names a technology that appears in tags | Tag boost |
| `type_inference` | Query uses type-hint keywords (`how to`, `mistake`, …). IDE cues (`.mdc`, `cursor rules`, …) infer **prompt** before generic `how to` → **skill**. | Type-inference boost |
| `multi_hop` | Correct result requires combining query context with entry metadata | Hybrid |
| `conversation` | Query targets a stored conversation summary | Hybrid / FTS |
| `prompt` | Query targets a stored prompt template | Hybrid / FTS |
| `abstention` | Off-topic query; no seed entry should match confidently | Overlap &lt; `abstention_min_overlap` (default 0.25) |

Each query specifies:
- **`expected_type` + `expected_item_id`** (preferred): stable ground truth for the row in the seed (id from the underlying `mistakes` / `patterns` / `skills` table). The runner matches `item_type` + `item_id` from search results.
- **`expected_title_contains`** (fallback): substring in the **FTS `title` field** when ids are unknown. For mistakes, the title is the first ~80 characters of the mistake text.
- **`abstention_min_overlap`** (abstention only): max fraction of query terms that may appear in a top-k hit's title+snippet before the query is treated as a confident match (default **0.25**). Do not use `utility_score` for abstention — utility bases (50–100+) ignore topical relevance.

### Interpreting two different numbers

| What you run | What R@5 means |
|--------------|----------------|
| **Seeded DB + `test_queries.json` (default)** | **Regression** — algorithm should keep this high; used in CI/smoke. |
| **Your snapshot + hand-labeled queries** | **Realistic** — see [`evals/README.md`](../evals/README.md) and `evals/real_queries.json.example`. |
| **Curated JSON on an arbitrary `memory.db` without the same rows** | **Not comparable** — low R@5 is often **missing ground truth**, not only “noise.” |

### Controlled noise (distractors)

To simulate a larger index **reproducibly** (synthetic distractor rows) without using private data:

```bash
python benchmarks/inject_noise.py --distractors 40
```

This seeds a temp DB, injects generic competing memories, and runs the benchmark. Compare R@1 / MRR before and after `src/ranking.py` changes.

**Fair hybrid stress (lexical + semantic):** `index_in_fts` tries to embed every indexed row into `vec_memory` when Ollama is available. If Ollama is down during injection, distractors can land in `memory_fts` with `embedding_status` **pending** or **failed** and **no** `vec_memory` row—semantic KNN will not see them, so hybrid results look more “lexical-heavy” than a full noise field. For a true dirty haystack on **both** channels, ensure Ollama is running when you run `inject_noise.py`, or run `engram reembed` (or `reembed_stale` via the API) on that `ENGRAM_DB_PATH` until pending items are **ready** and counts match. Lexical-only stress still applies when vectors are missing.

### Failure introspection

```bash
python benchmarks/engram_retrieval_bench.py --failures --failure-detail
```

`--failure-detail` prints `item_type`, `item_id`, and `utility_score` for the top-k hits on failed queries. Use with `--output file.json` to store `failed_query_ids` and per-query metrics; `top_hits_detail` is included when `--failure-detail` is set.

### Metrics

- **R@k (Recall at k)**: fraction of queries where the correct answer appears in the top-k results
  - R@1 = "first result is correct"
  - R@5 = "correct answer in top 5" (primary metric)
- **MRR (Mean Reciprocal Rank)**: average of `1/rank` where `rank` is the 1-based index of the first correct hit (uses the full retrieved list)
- **NDCG@k (Normalized Discounted Cumulative Gain)**: rank-aware quality, penalizes correct answers appearing lower in the list

### Search Modes

| Mode | Description |
|---|---|
| `hybrid` | FTS5 + semantic merged with utility scoring (default) |
| `fts_only` | FTS5 full-text search only |
| `semantic_only` | `sqlite-vec` vector similarity only |

Use `--mode compare` to run all three in one pass and compare.

---

## Running the Benchmark

```bash
# Standard run (hybrid, all 100 queries):
cd /path/to/engram
python benchmarks/engram_retrieval_bench.py

# Compare all three modes:
python benchmarks/engram_retrieval_bench.py --mode compare

# Verbose (see top-5 results per query):
python benchmarks/engram_retrieval_bench.py --verbose

# Show failed queries at R@5:
python benchmarks/engram_retrieval_bench.py --failures

# Save results to JSON:
python benchmarks/engram_retrieval_bench.py --output benchmarks/results_v1.1.json

# Run against a specific DB:
ENGRAM_DB_PATH=/tmp/my.db python benchmarks/engram_retrieval_bench.py

# Same via the engram CLI (pass benchmark args after --):
engram retrieval-benchmark -- --mode compare --output /tmp/out.json
```

The benchmark auto-seeds the database with sample data if it is empty, so it works without a real Engram history.

**Regression test:** CI runs the full 100-query set with `--fail-under-r5 0.90` (hybrid) and `--mode fts_only --fail-under-r5 0.50` (degraded). `pytest tests/test_benchmark_grading.py` covers abstention grading.

---

## Interpreting Results

| R@5 range | Interpretation |
|---|---|
| ≥ 0.90 | Excellent — almost always finds the right answer |
| 0.70–0.89 | Good — acceptable for production use |
| 0.50–0.69 | Fair — noticeable gaps; investigate failures |
| < 0.50 | Poor — ranking or retrieval has a structural issue |

Run before and after any ranking changes to validate improvements. On 100 queries, each 1% R@5 lift ≈ one additional query fixed.

---

## Baseline Results

_Run after any significant change to `src/ranking.py`, `src/search.py`, `src/query_analyzer.py`, or the embedding model._

Recorded baseline (hybrid, 100 queries, seeded DB, Ollama + `nomic-embed-text`, 2026-06-12):

| Mode | R@1 | R@3 | R@5 | MRR | NDCG@5 |
|------|-----|-----|-----|-----|--------|
| hybrid | 0.82 | 0.98 | **1.00** | 0.90 | 0.93 |

See [`results_baseline.json`](results_baseline.json) for per-category breakdown.

### Implementation notes (2026)

- **FTS5 tag filters:** Hyphenated tags (e.g. `ai-assistant`, `n-plus-one`) must be passed as **quoted phrases** in `tags MATCH`, or SQLite treats `-` as NOT and errors (`no such column: assistant`).
- **Auto-detected query tags:** Tags from `query_analyzer` are used for **ranking boosts** only. They are **not** AND’d into `tags MATCH` filters (that previously over-constrained results when many tags fired).

---

## Roadmap

- [x] Expand to 100 queries (multi-hop, conversation, prompt, abstention)
- [x] Abstention queries with overlap-based grading
- [x] CI gates full query set (R@5 ≥ 0.90 hybrid, ≥ 0.50 FTS-only)
- [ ] Optional: PR comments with R@5 / MRR delta vs `results_baseline.json`
- [ ] Full LongMemEval dataset run and published score

## Shared modules

- [`grading.py`](grading.py) — title vs `(type, id)` hit detection for metrics
- [`inject_noise.py`](inject_noise.py) — synthetic distractor stress test
