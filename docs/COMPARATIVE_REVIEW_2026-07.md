# Comparative review — three "Engram"-adjacent codebases, July 2026

Full-code reads (three parallel agents, every source file) of:
- **nagisanzenin/engram** — evidence-based learning engine (FSRS-4.5 spaced repetition, blind grading, receipts). 7,680-line stdlib Python engine, 214 selftests.
- **nagisanzenin/effortmining** — benchmark-calibrated effort dispatch for Claude Code subagents. 3,633-line stdlib harness, ~450 pre-registered runs.
- **deepseek-ai/Engram** — conditional N-gram memory for LLMs (research). 423-line demo + 33-page paper; no training code or weights.

## Verdicts

### Where they beat us

| Mechanism | Source | Status |
|---|---|---|
| **Per-item forgetting curves (FSRS-4.5)** — stability per memory, grown by proof and shrunk by lapses, vs our one fixed 90-day half-life for everything | nz-engram | **PORTED — schema v25** (`src/stability.py`). Use→good, helped→easy, unhelpful→lapse (never grows s). Conservative: items without events keep the old curve exactly; benchmark verified byte-identical on the seeded DB. |
| **One-scalar refit** — stamp predicted retrievability per event, compare vs observed helped-rate over ≥50 events, clamp a global multiplier [0.5, 1.5]; refuse-with-reason below n | nz-engram | Roadmap (needs event volume first). |
| **Fitting-harness discipline** — instrument gate before spend; append-only raw + derived rewrites; Wilson/Newcombe/bootstrap intervals; pre-registered margins & decision rules; provenance stamping (mock never earns `proven`); guarded single-step refits with human-readable diffs; machine-readable warnings the runtime honors; failures published; whole pipeline selftest-able in mock mode | effortmining | **Adopt as the shape of the planned ranking-weight fitting.** Their weak spot (saturated 9-per-cell binary suites) is exactly our PATTERN #13; our 100+ graded queries avoid it. Keep our train/holdout (stronger than their pre-registration-only). |
| **Gold-set audit for LLM judges** — adversarial case types, answer-stripped whitelist emission, 3 independent runs, QWK + signed leniency + direction-of-error, the consistency-bias paradox gate, contamination guard that dies, verdict with teeth (unvalidated → conservative fallback) | nz-engram | Roadmap: our recall relevance gate is an unaudited judge whose failure direction (injecting noise) is exactly their `graded_up`. Build the gold set before trusting the gate's precision claims. |
| **Honest denominators** — every rate ships with the population it excludes, in the human-readable string, not a nested key | nz-engram | Roadmap for `engram roi`/`stats`: report never-surfaced memories and injections-without-feedback alongside reuse. |
| **Deterministic exact lookup where ranked search is overkill** — the guard hook's true shape is an O(1) normalized-n-gram trigger probe, not a per-action embedding search; plus the NFKC/casefold/whitespace canonicalization chain | ds-engram | Roadmap (biggest latency win: guard currently pays an embedding call per Bash/Edit/Write). SQLite stores real keys, so skip their multi-head prime hashing entirely — that's an artifact of fixed dense tables. |
| **Idempotency keys on externally-generated events** (their `sid` transactions) | nz-engram | Roadmap: hook retries / MCP double-calls can double-record feedback today. |
| **No-network proof by AST selftest** | nz-engram | Roadmap: turns "100% local" from claim into CI invariant (whitelisting the configured Ollama endpoint). |

### Where we're better

- **Data scale and metric quality**: 100+ labeled real queries with graded R@5/MRR vs effortmining's 9–18 binary trials per cell (their suites saturated; most rows carry near-zero signal — their own misclass flags say so).
- **CI that actually gates**: our benchmark threshold runs on every push; theirs is an instruction in docs.
- **A live feedback pipeline**: `retrieval_feedback` accumulates real events; effortmining's equivalent has never fed a refit (`dispatch_consumed: 0` in their shipped state).
- **Transactional storage**: SQLite WAL+transactions supersede the atomic-write/lockfile/quarantine machinery nz-engram had to hand-build (their receipts-idempotency is the one piece we should still take).
- **Retrieve-cheap-then-gate**: DeepSeek's paper independently validates the design we already shipped (relevance gate: inject nothing over noise) — retrieval stays high-coverage, precision lives at the injection decision.
- **DeepSeek repo reality check**: demo-only (attention/MoE are identity lambdas), no training code, no offload implementation; the U-shaped capacity law and Δloss numbers are in-model pretraining results with no external-memory validity. The *strategic* lesson that transfers: on a growing store, the binding constraint is injection precision, not capacity — gate aggressively, delete reluctantly (which is already our stance).

## What shipped from this review

Schema v25: `memory_dynamics` + `src/stability.py` (FSRS-4.5 exact update rules), wired into `record_usage` (good), `memory_feedback` helped (easy) / unhelpful (lapse), and ranking's recency term via a single batched fetch. Items without history are untouched — verified: the 100-query benchmark output is identical to baseline except ~2.5ms/query fetch cost.

## Recommended order for the rest

1. Ranking-weight fitting harness in the effortmining shape (instrument gate → fit on train → interval-based non-inferiority on holdout → guarded provenance-stamped publish via inbox → failures published → mock-mode selftest).
2. Guard fast path: canonicalization chain + trigger-n-gram probe table; hybrid search becomes the fallback rung, not the per-action default.
3. Gold-set audit for the recall relevance gate (before trusting it further).
4. Refit scalar once ≥50 feedback events exist; honest denominators in roi/stats; sid-style idempotency keys; AST no-network selftest.
