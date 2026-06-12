# Public Eval Results

Last updated: **2026-06-12**

## Seeded regression (`benchmarks/test_queries.json`)

100 labeled queries across 8 categories (v1.2). CI gates hybrid **R@5 ≥ 0.90** on the full set.

| Metric | Last known | Target |
|--------|------------|--------|
| **R@5 (aggregate)** | **1.00** | ≥ 0.90 |
| **MRR** | **0.90** | ≥ 0.80 |
| **R@1** | 0.82 | — |
| **NDCG@5** | 0.93 | — |

### R@5 by category

| Category | n | R@5 |
|----------|--:|----:|
| exact_error | 15 | 1.00 |
| semantic_similar | 19 | 1.00 |
| tag_filter | 13 | 1.00 |
| type_inference | 13 | 1.00 |
| multi_hop | 12 | 1.00 |
| conversation | 10 | 1.00 |
| prompt | 8 | 1.00 |
| abstention | 10 | 1.00 |

```bash
python benchmarks/engram_retrieval_bench.py --output /tmp/retrieval.json
```

**Abstention grading:** off-topic queries pass when top-k hits have query-term overlap &lt; `abstention_min_overlap` (default 0.25). See `benchmarks/grading.py`.

## Public labeled set (`evals/public_queries.json`)

| Metric | Last known | Notes |
|--------|------------|-------|
| **R@5** | _TBD_ | 30+ held-out queries on seed DB |
| **MRR** | _TBD_ | |

```bash
ENGRAM_DB_PATH=/tmp/eval.db python benchmarks/engram_retrieval_bench.py \
  --queries evals/public_queries.json --output /tmp/public_eval.json
```

## LongMemEval adapter

| Metric | Last known | Notes |
|--------|------------|-------|
| **R@5** | 1.00 | 5-query offline smoke (`--offline`) |
| **MRR** | 1.00 | |

```bash
python benchmarks/longmemeval_bench.py --offline --output benchmarks/longmemeval_results.json
```

## Comparison context

| System | LongMemEval R@5 | Source |
|--------|------------------|--------|
| Engram | _TBD_ (full dataset) | This repo |
| MemPalace | 96.6% | Published (500q) |
| Mem0 v3 | 94.8% | Published |
| Mastra | 94.87% | Self-reported |

_Update this table only with reproducible runs; note DB path, commit SHA, and Ollama model._
