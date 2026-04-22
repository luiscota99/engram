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
- `expected_type`: the item_type the correct result should have
- `expected_title_contains`: substring that must appear in the correct result's title
- `category`: difficulty category
- `notes`: rationale

### Metrics

- **R@k (Recall at k)**: fraction of queries where the correct answer appears in the top-k results
  - R@1 = "first result is correct"
  - R@5 = "correct answer in top 5" (primary metric)
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
```

The benchmark auto-seeds the database with sample data if it is empty, so it works without a real Engram history.

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

_Run these after any significant change to `src/ranking.py`, `src/search.py`, or embedding model to validate improvements._

| Version | Mode | R@1 | R@3 | R@5 | NDCG@5 | Notes |
|---|---|---|---|---|---|---|
| 1.1.0 | hybrid | — | — | — | — | Run `engram_retrieval_bench.py` to populate |

---

## Roadmap

- [ ] Expand to 50–100 queries covering edge cases (multi-hop, temporal, abstention)
- [ ] Add LongMemEval-style "should return no result" (abstention) queries
- [ ] CI integration: run on `main` and comment R@5 delta on PRs
- [ ] Add recall for `conversation` and `prompt` item types
