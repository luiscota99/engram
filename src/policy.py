"""The policy file — ``~/.engram/policy.json`` (``ENGRAM_POLICY_PATH``).

One file holds every "which backend does this job" rule so a change is a
pointer edit, versioned with the store, never a code change:

* ``generate``: per-job routing for prose calls (extraction, audit, merge,
  summaries, …) — provider, model, mode (realtime | batch), privacy class and
  fallback chain, plus the price table the cost ledger uses. The spec's rule
  that matters: **personal content goes only to providers cleared for it**; a
  provider that is not cleared for the job's privacy class is skipped, and the
  refusal is logged.
* ``decide``: per-question backend pins for typed decisions (see ``decide.py``).

Precedence for generate: an explicit per-task env override
(``ENGRAM_LLM_EXTRACT_MODEL``) still wins over the policy — ops knobs stay
ops knobs — then the policy job entry, then the provider default. Rules
first; a decision model may later choose *within* a job's allowed set, never
outside it.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from . import config

POLICY_PATH_ENV = "ENGRAM_POLICY_PATH"

PRIVACY_CLASSES = ("personal", "public")
MODES = ("realtime", "batch")
PROVIDERS = ("anthropic", "openai", "ollama")

# USD per million tokens: (input, output). Anthropic first-party list prices;
# override or extend under generate.prices_usd_per_mtok in the policy file.
DEFAULT_PRICES: dict[str, list[float]] = {
    "claude-fable-5-1": [10.0, 50.0],
    "claude-fable-5": [10.0, 50.0],
    "claude-opus-5-5": [4.0, 20.0],
    "claude-opus-5": [5.0, 25.0],
    "claude-opus-4-8": [5.0, 25.0],
    "claude-opus-4-7": [5.0, 25.0],
    "claude-opus-4-6": [5.0, 25.0],
    "claude-sonnet-5-5": [2.0, 10.0],
    "claude-sonnet-5": [2.0, 10.0],
    "claude-sonnet-4-6": [3.0, 15.0],
    "claude-haiku-5-5": [0.10, 0.50],
    "claude-haiku-4-5": [1.0, 5.0],
}

DEFAULT_POLICY: dict[str, Any] = {
    "generate": {
        "providers": {
            # privacy: which classes of content may be sent there.
            "anthropic": {"privacy": ["personal", "public"]},
            "ollama": {"privacy": ["personal", "public"]},  # local
            # Any OpenAI-compatible REMOTE endpoint (OpenRouter, DeepSeek, Gemini…).
            # Public only unless you clear it — a localhost base URL is treated
            # as local (personal allowed) automatically.
            "openai": {"privacy": ["public"]},
        },
        "default": {"privacy": "personal", "mode": "realtime", "fallback": []},
        "jobs": {
            "extract": {},
            "audit": {},
            "gc": {},
            "merge": {},
            "decide": {},
            "summarize": {},
            "reflex": {},
            "validate": {},
            "chat": {},
        },
        "prices_usd_per_mtok": {},
    },
    "decide": {
        "default_backend": "haiku",
        "backends": {},
        "thresholds": {},
        "laya": {
            "model": "multilingual",
            "device": None,
            "models": None,
            "max_len": None,
            "max_latency_ms": 300,
        },
    },
}


def policy_path() -> str:
    return os.environ.get(POLICY_PATH_ENV) or os.path.join(config.engram_dir(), "policy.json")


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_policy() -> dict[str, Any]:
    """Policy with defaults filled in (``{}`` or a missing file is a valid policy)."""
    data: dict[str, Any] = {}
    try:
        with open(policy_path(), encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    return _deep_merge(DEFAULT_POLICY, data)


def save_policy(policy: dict[str, Any]) -> str:
    path = policy_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(policy, f, indent=2, sort_keys=True)
    return path


def get_value(policy: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = policy
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_value(dotted: str, value: Any) -> str:
    """Set one dotted key (``generate.jobs.extract.model``) and write the file."""
    if not dotted or dotted.startswith(".") or dotted.endswith("."):
        raise ValueError("key must be a dotted path like generate.jobs.extract.model")
    policy = load_policy()
    cur = policy
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return save_policy(policy)


def prices(policy: dict[str, Any] | None = None) -> dict[str, list[float]]:
    p = policy or load_policy()
    merged = dict(DEFAULT_PRICES)
    for k, v in (p["generate"].get("prices_usd_per_mtok") or {}).items():
        if isinstance(v, (list, tuple)) and len(v) == 2:
            merged[str(k)] = [float(v[0]), float(v[1])]
    return merged


def estimate_cost_usd(model: str, usage: dict[str, Any] | None, policy: dict[str, Any] | None = None) -> float | None:
    """Cost of one call from its token usage; None when the model is unpriced
    (local models are priced 0 explicitly by the caller)."""
    if not usage:
        return None
    table = prices(policy)
    key = next((k for k in table if model == k or model.startswith(k)), None)
    if key is None:
        return None
    inp, out = table[key]
    cost = (float(usage.get("input_tokens") or 0) * inp + float(usage.get("output_tokens") or 0) * out) / 1e6
    # Anthropic prompt caching: reads ~10% of input, writes ~125%.
    cost += float(usage.get("cache_read_input_tokens") or 0) * inp * 0.10 / 1e6
    cost += float(usage.get("cache_creation_input_tokens") or 0) * inp * 1.25 / 1e6
    return round(cost, 6)
