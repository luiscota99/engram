# Public Eval Results

Last updated: **2026-07-25** (SOTA roadmap)

## Seeded regression / EEME (`benchmarks/test_queries.json`)

100 labeled engineering queries. CI gates hybrid **R@5 ≥ 0.90**. Also runnable as:

```bash
python benchmarks/eeme_bench.py --fail-under-r5 0.90
```

| Metric | Last known | Target |
|--------|------------|--------|
| **R@5 (aggregate)** | **1.00** | ≥ 0.90 |
| **MRR** | **~0.92** | ≥ 0.80 |

## LongMemEval (retrieval-only, local embedder)

| Metric | Last known | Notes |
|--------|------------|-------|
| **Session R@5** | **0.538** | Full oracle, 940 sessions — see `benchmarks/BENCHMARKS.md` |
| **MRR** | **0.442** | Not comparable to vendor QA accuracy headlines |

## BEAM

Adapter stub: `benchmarks/beam_bench.py` (retrieval-only; provide local labeled slice).

## LoCoMo

Adapter: `benchmarks/locomo_bench.py --queries <file>` (retrieval R@k only).

## Relevance gate gold (`evals/gate_gold.json`)

```bash
python benchmarks/gate_eval.py --fail-under-precision 0.95
```

## Comparison context

| System | Published claim | Comparable to Engram retrieval? |
|--------|-----------------|----------------------------------|
| Engram | R@5 retrieval 0.538 LME oracle | Yes |
| MemPalace | ~96.6% LME R@5 (claim) | Same family if retrieval |
| Mem0 / Zep / Hindsight | Often QA / LLM-judge | **No** — see `docs/COMPARISON.md` |
