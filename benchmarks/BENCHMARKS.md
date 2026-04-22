# Engram Retrieval Benchmarks

Measures search quality of Engram's hybrid retrieval (FTS5 + `sqlite-vec` semantic) without requiring external infrastructure or ground-truth conversations.

---

## Methodology

### Query Dataset

`test_queries.json` contains 20 curated queries across four difficulty categories:

| Category | Description | Expected winner |
|---|---|---|
| `exact_error` | Query uses verbatim terms from the stored entry | FTS5 |
| `semantic_similar` | Query paraphrases the entry (different vocabulary, same concept) | Semantic |
| `tag_filter` | Query names a technology that appears in tags | Tag boost |
| `type_inference` | Query uses type-hint keywords (`how to`, `debugging`, `mistake`) | Type-inference boost |

Each query specifies:
- `expected_type`: the item_type the correct result should have (informational; grading uses titles)
- `expected_title_contains`: substring that must appear in the **FTS `title` field** of the correct row (for mistakes, that is the first ~80 characters of the mistake text — not a separate display title)
- `category`: difficulty category
- `notes`: rationale

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
# Standard run (hybrid, all 20 queries):
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

**Regression test:** `pytest tests/test_retrieval_benchmark.py` asserts hybrid **R@5 ≥ 0.85** on the full 20-query set (seeded DB).

---

## Interpreting Results

| R@5 range | Interpretation |
|---|---|
| ≥ 0.90 | Excellent — almost always finds the right answer |
| 0.70–0.89 | Good — acceptable for production use |
| 0.50–0.69 | Fair — noticeable gaps; investigate failures |
| < 0.50 | Poor — ranking or retrieval has a structural issue |

Run before and after any ranking changes to validate improvements. A 5% R@5 lift on 20 queries = 1 additional query fixed, so focus on queries with zero overlap across categories.

---

## Baseline Results

_Run after any significant change to `src/ranking.py`, `src/search.py`, `src/query_analyzer.py`, or the embedding model._

Example (hybrid, 20 queries, seeded DB, **local** run — semantic leg may vary slightly if Ollama/model differ):

| Mode | R@1 | R@3 | R@5 | MRR | NDCG@5 |
|------|-----|-----|-----|-----|--------|
| hybrid | ~0.80 | ~1.00 | ~1.00 | ~0.88 | ~0.91 |

### Implementation notes (2026)

- **FTS5 tag filters:** Hyphenated tags (e.g. `ai-assistant`, `n-plus-one`) must be passed as **quoted phrases** in `tags MATCH`, or SQLite treats `-` as NOT and errors (`no such column: assistant`).
- **Auto-detected query tags:** Tags from `query_analyzer` are used for **ranking boosts** only. They are **not** AND’d into `tags MATCH` filters (that previously over-constrained results when many tags fired).

---

## Roadmap

- [ ] Expand to 50–100 queries covering edge cases (multi-hop, temporal, abstention)
- [ ] Add LongMemEval-style "should return no result" (abstention) queries
- [ ] Optional: PR comments with R@5 / MRR delta vs `main`
- [ ] Add recall for `conversation` and `prompt` item types
