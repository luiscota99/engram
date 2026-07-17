#!/usr/bin/env python3
"""Fit the ranking weights against the labeled eval suite — measured, gated, honest.

The ~20 constants in src/ranking.py were hand-tuned ("was 15 — cosmetic").
This harness fits them against labeled queries, with the discipline the July
2026 comparative review distilled from effortmining:

  validate  → instrument gate. Refuses everything downstream until: the label
              set is intact against the bench DB, replayed ranking under
              CURRENT weights exactly reproduces live search ordering for
              every query (parity), and the baseline meets the CI floor.
  fit       → retrieval runs ONCE per query (rank inputs captured); candidate
              weight configs re-rank offline — seeded random search then
              coordinate refinement, TRAIN split only. Constants the eval set
              cannot discriminate (no ordering change across their full
              bounds) are auto-detected, HELD at current values, and listed
              as fit-blindness warnings. Every config lands in an append-only
              results.jsonl.
  analyze   → HOLDOUT verdict. Pre-registered decision rule (constants below,
              fixed before any data): publishable iff the seeded stratified
              bootstrap lower bound of ΔR@5 ≥ −DELTA_R5, point ΔR@5 ≥ 0, and
              point ΔMRR > 0. A no-win is written to the report, not buried.
              Candidate weights carry provenance; only a real-dataset run
              that passed validate can ever stamp proven=true.
  propose   → files an inbox DECISION with the per-constant diff. Applying is
              always the user's move: `engram weights apply`.

Usage (always a scratch DB — the bench itself enforces this):
  ENGRAM_DB_PATH=/tmp/fit.db python benchmarks/fit_ranking.py validate
  ENGRAM_DB_PATH=/tmp/fit.db python benchmarks/fit_ranking.py fit
  ENGRAM_DB_PATH=/tmp/fit.db python benchmarks/fit_ranking.py analyze
  ENGRAM_DB_PATH=/tmp/fit.db python benchmarks/fit_ranking.py propose
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path

if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.grading import expects_abstention, row_matches_expected
from src.ranking_weights import REGISTRY, apply_weights, current_weights

# ── Pre-registered constants (fixed before any data; do not tune to results) ──
SEED = 20260717
TRAIN_FRACTION = 0.7
FIT_BUDGET = 400            # random-search configs
REFINE_SWEEPS = 2           # coordinate-refinement passes over fitted constants
REFINE_POINTS = 5           # grid points per constant per sweep
DELTA_R5 = 0.02             # holdout non-inferiority margin on R@5
BOOTSTRAP_B = 10_000
K_EVAL = 5                  # R@K
BASELINE_R5_FLOOR = 0.90    # must reproduce the CI gate during validate

STATE_DIR = Path(__file__).resolve().parent / "fit"
QUERIES_DEFAULT = str(Path(__file__).resolve().parent / "test_queries.json")


# ── shared plumbing ──────────────────────────────────────────────────


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _embedding_stamp() -> dict:
    return {
        "embed_url": os.environ.get("ENGRAM_EMBED_URL", "default"),
        "embed_model": os.environ.get("ENGRAM_EMBED_MODEL", "default"),
    }


def _load_queries(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("queries", data) if isinstance(data, dict) else data


def _split(queries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Deterministic stratified train/holdout split by category."""
    rng = random.Random(SEED)
    by_cat: dict[str, list[dict]] = {}
    for q in queries:
        by_cat.setdefault(q.get("category", "misc"), []).append(q)
    train, holdout = [], []
    for cat in sorted(by_cat):
        qs = sorted(by_cat[cat], key=lambda q: str(q.get("id", q.get("query"))))
        rng.shuffle(qs)
        cut = max(1, round(len(qs) * TRAIN_FRACTION)) if len(qs) > 1 else 1
        train.extend(qs[:cut])
        holdout.extend(qs[cut:])
    return train, holdout


def _atomic_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ── retrieval once, re-rank offline ──────────────────────────────────


def collect_snapshots(queries: list[dict], db_path: str | None) -> list[dict]:
    """Run live search once per query, capturing rank inputs + live order."""
    from src.search import search

    snaps = []
    for q in queries:
        sink: dict = {}
        live = search(
            q["query"], limit=max(K_EVAL, 10), db_path=db_path,
            skip_audit=True, rank_inputs_sink=sink,
        )
        sink["live_order"] = [(r["item_type"], str(r["item_id"])) for r in live]
        sink["label"] = q
        snaps.append(sink)
    return snaps


def replay(snapshot: dict) -> list[dict]:
    """Re-run the post-retrieval pipeline (rank → BM25 → pins → filter → trim)
    on captured inputs under whatever weights are currently applied."""
    import copy

    from src.ranking import rank_results, rerank_with_bm25, result_key

    results = rank_results(
        results=copy.deepcopy(snapshot["candidates"]),
        usage_counts=snapshot["usage_counts"],
        last_used_map=snapshot["last_used_map"],
        affinities=snapshot["affinities"],
        query=snapshot["query"],
        stale_rowids=snapshot["stale_rowids"],
        detected_tags=snapshot["detected_tags"],
        rrf_scores=snapshot["rrf_scores"],
        item_dates=snapshot["item_dates"],
        temporal_intent=snapshot["temporal_intent"],
        feedback_map=snapshot["feedback_map"],
        stability_by_key=snapshot["stability_by_key"],
    )
    if snapshot["query"].strip():
        results = rerank_with_bm25(results, snapshot["query"])
    pinned = copy.deepcopy(snapshot["pinned"])
    if pinned:
        ftags = snapshot["filter_tags"]
        if ftags:
            pinned = [
                p for p in pinned
                if all(t.lower() in (p.get("tags") or "").lower() for t in ftags)
            ]
        pinned_keys = {result_key(p) for p in pinned}
        results = [r for r in results if result_key(r) not in pinned_keys]
        results = pinned + results
    if not snapshot["include_superseded"]:
        results = [r for r in results if not str(r.get("title", "")).startswith("[SUPERSEDED]")]
    return results[: snapshot["limit"]]


def _query_score(snapshot: dict) -> tuple[float, float]:
    """(recall@K, reciprocal rank) for one replayed query under current weights."""
    label = snapshot["label"]
    rows = replay(snapshot)[:K_EVAL]
    if expects_abstention(label):
        hit = not any(row_matches_expected(r, label) for r in rows)
        return (1.0 if hit else 0.0, 1.0 if hit else 0.0)
    for rank, r in enumerate(rows, start=1):
        if row_matches_expected(r, label):
            return 1.0, 1.0 / rank
    return 0.0, 0.0


def evaluate(snapshots: list[dict], weights: dict[str, float]) -> tuple[float, float]:
    """(R@K, MRR) over snapshots with *weights* applied — always restored."""
    previous = apply_weights(weights)
    try:
        scores = [_query_score(s) for s in snapshots]
    finally:
        apply_weights(previous)
    n = max(1, len(scores))
    return sum(r for r, _ in scores) / n, sum(m for _, m in scores) / n


# ── subcommands ──────────────────────────────────────────────────────


def cmd_validate(args) -> int:
    db_path = os.environ.get("ENGRAM_DB_PATH")
    if not db_path:
        print("ERROR: set ENGRAM_DB_PATH to a scratch DB (never the real one).")
        return 2
    from benchmarks.engram_retrieval_bench import _ensure_seeded

    _ensure_seeded(db_path)
    queries = _load_queries(args.queries)

    # 1. Label-set integrity: every non-abstention label must be resolvable.
    from src.database import get_item

    broken = []
    for q in queries:
        if expects_abstention(q):
            continue
        if q.get("expected_item_id") is not None and q.get("expected_type"):
            if get_item(q["expected_type"], int(q["expected_item_id"]), db_path=db_path) is None:
                broken.append(q.get("id"))
    if broken:
        print(f"GATE FAIL: {len(broken)} labels point at missing items: {broken[:5]}")
        return 1

    # 2. Replay parity: offline re-rank under current weights must reproduce
    # live ordering exactly, for every query. If not, the fitter would be
    # optimizing a pipeline that isn't the one production runs.
    snaps = collect_snapshots(queries, db_path)
    mismatches = 0
    for s in snaps:
        replay_order = [(r["item_type"], str(r["item_id"])) for r in replay(s)]
        if replay_order != s["live_order"]:
            mismatches += 1
    if mismatches:
        print(f"GATE FAIL: replay != live for {mismatches}/{len(snaps)} queries — refusing to fit.")
        return 1

    # 3. Baseline floor: current weights must reproduce the CI gate.
    r5, mrr = evaluate(snaps, current_weights())
    if r5 < BASELINE_R5_FLOOR:
        print(f"GATE FAIL: baseline R@{K_EVAL}={r5:.3f} below floor {BASELINE_R5_FLOOR} — instrument moved.")
        return 1

    _atomic_write(STATE_DIR / "phase0.json", {
        "gate_passed": True,
        "n_queries": len(queries),
        "baseline_r5": round(r5, 4),
        "baseline_mrr": round(mrr, 4),
        "dataset_hash": _sha256_file(args.queries),
        "db_hash": _sha256_file(db_path),
        "embedding": _embedding_stamp(),
        "seed": SEED,
    })
    print(f"✓ Gate passed: {len(queries)} labels intact, replay==live, baseline R@{K_EVAL}={r5:.3f} MRR={mrr:.3f}")
    return 0


def _require_gate(args) -> dict:
    try:
        with open(STATE_DIR / "phase0.json", encoding="utf-8") as f:
            phase0 = json.load(f)
    except OSError:
        print("BLOCKED: run `validate` first (no phase0.json).")
        sys.exit(1)
    if not phase0.get("gate_passed"):
        print("BLOCKED: instrument gate not passed.")
        sys.exit(1)
    if phase0.get("dataset_hash") != _sha256_file(args.queries):
        print("BLOCKED: label set changed since validate — re-run `validate`.")
        sys.exit(1)
    return phase0


def _detect_signal(snapshots: list[dict]) -> tuple[list[str], list[str]]:
    """Constants whose full-bounds perturbation changes any top-K ordering.

    The rest get fit-blindness warnings and are HELD — fitting a constant the
    eval can't see is how noise gets laundered into 'tuning'.
    """
    base = current_weights()
    fitted, held = [], []
    for name, (_m, _a, _k, lo, hi) in REGISTRY.items():
        moved = False
        for extreme in (lo, hi):
            probe = dict(base)
            probe[name] = extreme
            previous = apply_weights(probe)
            try:
                for s in snapshots:
                    order = [(r["item_type"], str(r["item_id"])) for r in replay(s)[:K_EVAL]]
                    if order != [tuple(t) for t in s["_base_topk"]]:
                        moved = True
                        break
            finally:
                apply_weights(previous)
            if moved:
                break
        (fitted if moved else held).append(name)
    return fitted, held


def cmd_fit(args) -> int:
    phase0 = _require_gate(args)
    db_path = os.environ.get("ENGRAM_DB_PATH")
    queries = _load_queries(args.queries)
    train, holdout = _split(queries)
    print(f"  Split: {len(train)} train / {len(holdout)} holdout (seed {SEED})")

    snaps = collect_snapshots(train, db_path)
    for s in snaps:  # cache baseline top-K for signal detection
        s["_base_topk"] = [(r["item_type"], str(r["item_id"])) for r in replay(s)[:K_EVAL]]

    fitted_names, held = _detect_signal(snaps)
    print(f"  Fitting {len(fitted_names)} constants; {len(held)} held (no signal in eval set):")
    for h in held:
        print(f"    ~ {h} [fit-blindness warning]")

    base = current_weights()
    rng = random.Random(SEED)
    results_path = STATE_DIR / "results.jsonl"
    results_path.parent.mkdir(parents=True, exist_ok=True)

    def eval_and_log(weights: dict[str, float], origin: str) -> tuple[float, float]:
        r5, mrr = evaluate(snaps, weights)
        with open(results_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "origin": origin, "train_r5": round(r5, 4), "train_mrr": round(mrr, 4),
                "weights": {k: round(v, 4) for k, v in weights.items() if k in fitted_names},
            }, sort_keys=True) + "\n")
        return r5, mrr

    best_w, best = dict(base), eval_and_log(base, "baseline")
    for i in range(FIT_BUDGET):
        cand = dict(base)
        for name in fitted_names:
            _m, _a, _k, lo, hi = REGISTRY[name]
            cand[name] = rng.uniform(lo, hi)
        score = eval_and_log(cand, f"random_{i}")
        if score > best:
            best_w, best = cand, score
    for sweep in range(REFINE_SWEEPS):
        for name in fitted_names:
            _m, _a, _k, lo, hi = REGISTRY[name]
            for j in range(REFINE_POINTS):
                cand = dict(best_w)
                cand[name] = lo + (hi - lo) * j / (REFINE_POINTS - 1)
                score = eval_and_log(cand, f"refine_{sweep}_{name}")
                if score > best:
                    best_w, best = cand, score

    _atomic_write(STATE_DIR / "fit.json", {
        "best_train": {"r5": round(best[0], 4), "mrr": round(best[1], 4)},
        "baseline_train": {"r5": None, "mrr": None},  # first line of results.jsonl
        "best_weights": {k: round(v, 6) for k, v in best_w.items()},
        "fitted": fitted_names,
        "held_no_signal": held,
        "train_ids": [q.get("id") for q in train],
        "holdout_ids": [q.get("id") for q in holdout],
        "phase0": phase0,
    })
    print(f"✓ Fit done: best train R@{K_EVAL}={best[0]:.3f} MRR={best[1]:.3f} → {STATE_DIR/'fit.json'}")
    return 0


def _bootstrap_delta_lb(per_query: list[tuple[str, float, float]]) -> float:
    """Seeded stratified bootstrap: 2.5th percentile of mean(ΔR@5)."""
    rng = random.Random(SEED)
    by_cat: dict[str, list[float]] = {}
    for cat, delta, _ in per_query:
        by_cat.setdefault(cat, []).append(delta)
    draws = []
    cats = sorted(by_cat)
    for _ in range(BOOTSTRAP_B):
        total, n = 0.0, 0
        for cat in cats:
            vals = by_cat[cat]
            for _ in vals:
                total += vals[rng.randrange(len(vals))]
                n += 1
        draws.append(total / max(1, n))
    draws.sort()
    return draws[int(0.025 * len(draws))]


def cmd_analyze(args) -> int:
    _require_gate(args)
    db_path = os.environ.get("ENGRAM_DB_PATH")
    with open(STATE_DIR / "fit.json", encoding="utf-8") as f:
        fit = json.load(f)
    queries = _load_queries(args.queries)
    holdout = [q for q in queries if q.get("id") in set(fit["holdout_ids"])]
    if not holdout:
        print("BLOCKED: empty holdout — label set too small to analyze; grow it (engram bench-label).")
        return 1

    snaps = collect_snapshots(holdout, db_path)
    base_w, cand_w = current_weights(), fit["best_weights"]

    per_query = []
    for s in snaps:
        prev = apply_weights(base_w)
        try:
            b_r5, b_mrr = _query_score(s)
        finally:
            apply_weights(prev)
        prev = apply_weights(cand_w)
        try:
            c_r5, c_mrr = _query_score(s)
        finally:
            apply_weights(prev)
        per_query.append((s["label"].get("category", "misc"), c_r5 - b_r5, c_mrr - b_mrr))

    n = len(per_query)
    d_r5 = sum(d for _, d, _ in per_query) / n
    d_mrr = sum(d for _, _, d in per_query) / n
    lb = _bootstrap_delta_lb(per_query)
    publishable = (lb >= -DELTA_R5) and (d_r5 >= 0.0) and (d_mrr > 0.0)

    diff_lines = [
        f"  {name}: {base_w[name]:.3f} -> {cand_w[name]:.3f}"
        for name in fit["fitted"]
        if abs(base_w[name] - cand_w[name]) > 1e-9
    ]
    verdict = "PUBLISHABLE" if publishable else "NO-WIN (kept on the record, not buried)"
    report = [
        f"# Ranking-weight fit report — seed {SEED}",
        f"Holdout: n={n}  ΔR@{K_EVAL}={d_r5:+.4f} (bootstrap lb {lb:+.4f}, margin -{DELTA_R5})  ΔMRR={d_mrr:+.4f}",
        f"Verdict: {verdict}",
        "",
        "Held (no signal in eval set — grow the label set to fit these):",
        *[f"  ~ {h}" for h in fit["held_no_signal"]],
        "",
        "Proposed diff:" if diff_lines else "Proposed diff: (none)",
        *diff_lines,
    ]
    (STATE_DIR / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    _atomic_write(STATE_DIR / "candidate_weights.json", {
        "weights": cand_w,
        "proven": bool(publishable),
        "provenance": {
            "mode": "real",
            "seed": SEED,
            "n_train": len(fit["train_ids"]),
            "n_holdout": n,
            "delta_r5": round(d_r5, 4),
            "delta_r5_bootstrap_lb": round(lb, 4),
            "delta_mrr": round(d_mrr, 4),
            "held_no_signal": fit["held_no_signal"],
            "phase0": fit["phase0"],
        },
    })
    print("\n".join(report))
    return 0


def cmd_propose(args) -> int:
    with open(STATE_DIR / "candidate_weights.json", encoding="utf-8") as f:
        cand = json.load(f)
    if not cand.get("proven"):
        print("Nothing to propose: the candidate did not pass the holdout decision rule.")
        return 1
    from src.inbox import file_item

    prov = cand["provenance"]
    item = file_item(
        kind="decision",
        severity="info",
        title=(
            f"Adopt fitted ranking weights? holdout ΔR@5 {prov['delta_r5']:+.3f} "
            f"(lb {prov['delta_r5_bootstrap_lb']:+.3f}), ΔMRR {prov['delta_mrr']:+.3f}"
        ),
        body=(
            "Fitted against the labeled eval suite (see benchmarks/fit/REPORT.md). "
            "Apply: engram weights apply benchmarks/fit/candidate_weights.json — "
            "reject to keep current constants."
        ),
        finding_key="ranking-weights:candidate",
        source="fit_harness",
    )
    print("✓ Filed inbox decision." if item else "Already proposed (open item exists).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "fit", "analyze", "propose"])
    parser.add_argument("--queries", default=QUERIES_DEFAULT)
    args = parser.parse_args()
    return {
        "validate": cmd_validate, "fit": cmd_fit,
        "analyze": cmd_analyze, "propose": cmd_propose,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
