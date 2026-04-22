# Real-world evaluation (optional)

The **curated** benchmark in [`../benchmarks/`](../benchmarks/) uses a **known seed** and [`test_queries.json`](../benchmarks/test_queries.json) with `(expected_type, expected_item_id)` so CI can measure **R@5 / MRR** on a **closed world**.

This folder is for **separate, labeled** evaluation on **your** memory:

- Copy or export your DB to a **frozen** path (e.g. `~/engram-snapshots/2026-04-22.db`); do **not** commit it if it may contain private text.
- Create `real_queries.json` from [`real_queries.json.example`](real_queries.json.example): for each question, set **`expected_type`** and **`expected_item_id`** that you verified in the DB (or `expected_title_contains` only if you prefer).
- From the **engram** repo root:

  ```bash
  ENGRAM_DB_PATH=/path/to/snapshot.db python benchmarks/engram_retrieval_bench.py \
    --queries evals/real_queries.json --no-seed
  ```

**Interpreting results**

| Setup | What the number means |
|--------|------------------------|
| Seeded + `test_queries.json` | **Regression** — should stay stable in CI. |
| Your snapshot + labeled `real_queries.json` | **Realistic** — how search behaves on your corpus. |
| Unlabeled `memory.db` + seed JSON | **Not comparable** — ground-truth rows may be missing; low R@5 is expected. |

**Privacy:** Keep snapshots and private `real_queries.json` out of version control; commit only the `.example` template.
