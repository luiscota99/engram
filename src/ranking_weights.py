"""Fittable ranking weights — one registry shared by the fitter and the runtime.

``src/ranking.py``'s constants were set by feel (one comment literally reads
"was 15 — cosmetic"). The fit harness (``benchmarks/fit_ranking.py``)
optimizes them against the labeled eval suites; this module is the single
source of truth for WHICH constants are fittable, their bounds, and how a
fitted set is applied — so the fitter and the runtime loader can never drift.

Runtime adoption is opt-in and provenance-gated: ``engram weights apply``
installs a candidate file into the store dir; on import, ranking loads it
only if ``proven`` is true (set exclusively by the harness when the
instrument gate passed, the dataset was real, and the pre-registered holdout
decision rule held). Mock or hand-edited files never auto-apply.
"""

from __future__ import annotations

import importlib
import json
import logging
import os

logger = logging.getLogger(__name__)

# name → (module, attribute, dict_key_or_None, lo, hi)
# Bounds are the fitter's search space AND the loader's sanity clamp.
# BASE_SCORE_SEMANTIC is deliberately absent: it anchors the score scale.
REGISTRY: dict[str, tuple[str, str, str | None, float, float]] = {
    "base_score_lexical": ("src.ranking", "BASE_SCORE_LEXICAL", None, 20.0, 100.0),
    "recency_floor": ("src.ranking", "RECENCY_FLOOR", None, 0.4, 1.0),
    "recency_span": ("src.ranking", "RECENCY_SPAN", None, 0.0, 0.6),
    "recency_half_life_days": ("src.ranking", "RECENCY_HALF_LIFE_DAYS", None, 15.0, 365.0),
    "usage_boost_weight": ("src.ranking", "USAGE_BOOST_WEIGHT", None, 0.0, 30.0),
    "type_match_boost": ("src.ranking", "TYPE_MATCH_BOOST", None, 0.0, 50.0),
    "tag_match_boost": ("src.ranking", "TAG_MATCH_BOOST", None, 0.0, 50.0),
    "stale_embedding_penalty": ("src.ranking", "STALE_EMBEDDING_PENALTY", None, 0.0, 50.0),
    "bm25_weight": ("src.ranking", "BM25_WEIGHT", None, 0.0, 1.0),
    "rrf_weight": ("src.ranking", "RRF_WEIGHT", None, 0.0, 150.0),
    "affinity_created": ("src.ranking", "AFFINITY_BOOSTS", "created", 0.0, 80.0),
    "affinity_used": ("src.ranking", "AFFINITY_BOOSTS", "used", 0.0, 60.0),
    "affinity_relevant": ("src.ranking", "AFFINITY_BOOSTS", "relevant", 0.0, 40.0),
    "intent_mult_mistake": ("src.ranking", "INTENT_TYPE_MULTIPLIERS", "mistake", 1.0, 1.6),
    "intent_mult_pattern": ("src.ranking", "INTENT_TYPE_MULTIPLIERS", "pattern", 1.0, 1.6),
    "intent_mult_skill": ("src.ranking", "INTENT_TYPE_MULTIPLIERS", "skill", 1.0, 1.6),
    "intent_mult_conversation": ("src.ranking", "INTENT_TYPE_MULTIPLIERS", "conversation", 1.0, 1.6),
    "intent_mult_prompt": ("src.ranking", "INTENT_TYPE_MULTIPLIERS", "prompt", 1.0, 1.6),
    "feedback_helped_weight": ("src.feedback", "HELPED_WEIGHT", None, 0.0, 30.0),
    "feedback_unhelpful_weight": ("src.feedback", "UNHELPFUL_WEIGHT", None, 0.0, 50.0),
}


def current_weights() -> dict[str, float]:
    out = {}
    for name, (mod_name, attr, key, _lo, _hi) in REGISTRY.items():
        mod = importlib.import_module(mod_name)
        val = getattr(mod, attr)
        out[name] = float(val[key] if key is not None else val)
    return out


def apply_weights(weights: dict[str, float]) -> dict[str, float]:
    """Set registry-known weights on their modules; returns the previous
    values (same shape) so a caller can restore. Unknown names are ignored;
    values are clamped to registry bounds."""
    previous = current_weights()
    for name, value in weights.items():
        spec = REGISTRY.get(name)
        if spec is None:
            continue
        mod_name, attr, key, lo, hi = spec
        clamped = max(lo, min(hi, float(value)))
        mod = importlib.import_module(mod_name)
        if key is not None:
            getattr(mod, attr)[key] = clamped
        else:
            setattr(mod, attr, clamped)
    return previous


def persisted_weights_path() -> str:
    explicit = os.environ.get("ENGRAM_RANKING_WEIGHTS")
    if explicit:
        return explicit
    from . import config

    return os.path.join(config.engram_dir(), "ranking_weights.json")


def load_and_apply_persisted() -> bool:
    """Apply an installed, PROVEN weights file at import time.

    Refuses (with a warning) anything unproven — provenance is the harness's
    signature, not a formality. Returns True when weights were applied.
    """
    path = persisted_weights_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return False
    except ValueError:
        logger.warning("ranking weights file at %s is not valid JSON; ignored", path)
        return False
    if not isinstance(data, dict) or not isinstance(data.get("weights"), dict):
        logger.warning("ranking weights file at %s has unexpected shape; ignored", path)
        return False
    if not data.get("proven"):
        logger.warning(
            "ranking weights at %s are not marked proven (mock or hand-edited?); ignored",
            path,
        )
        return False
    apply_weights(data["weights"])
    logger.info("applied fitted ranking weights from %s", path)
    return True
