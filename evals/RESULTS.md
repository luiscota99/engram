# Public Eval Results

Last updated: **2026-06-06** (placeholders — run benchmarks to populate)

## Seeded regression (`benchmarks/test_queries.json`)

| Metric | Last known | Target |
|--------|------------|--------|
| **R@5 (aggregate)** | _TBD_ | ≥ 0.85 |
| **MRR** | _TBD_ | ≥ 0.80 |

### R@5 by category

| Category | n | R@5 |
|----------|--:|----:|
| exact_error | 15 | _TBD_ |
| semantic_similar | 19 | _TBD_ |
| tag_filter | 13 | _TBD_ |
| type_inference | 13 | _TBD_ |
| multi_hop | 12 | _TBD_ |
| conversation | 10 | _TBD_ |
| prompt | 8 | _TBD_ |
| abstention | 10 | _TBD_ |

```bash
python benchmarks/engram_retrieval_bench.py --output /tmp/retrieval.json
```

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
| **R@5** | _TBD_ | HF dataset or `--offline` samples |
| **MRR** | _TBD_ | |

```bash
python benchmarks/longmemeval_bench.py --offline --output benchmarks/longmemeval_results.json
```

## Comparison context

| System | LongMemEval R@5 | Source |
|--------|------------------|--------|
| Engram | _TBD_ | This repo |
| MemPalace | 96.6% | Published (500q) |
| Mem0 v3 | 94.8% | Published |
| Mastra | 94.87% | Self-reported |

_Update this table only with reproducible runs; note DB path, commit SHA, and Ollama model._
