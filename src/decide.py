"""Typed decisions — the LLM node's ``decide`` interface (hub spec, Laya-ready).

A decision is a judgement, not prose: pick one label (``choice``), place on a
scale (``score``), or answer yes/no (``yesno``). Everything that needs a reason
stays on the ``generate`` side (``llm.py``).

Three rules from the spec:

* **Questions are versioned** (``worth_remembering@v1``). The interface never
  changes; adding a question never breaks an old one.
* **The log is the dataset.** Every decision lands in the ``decisions`` table
  with its input, the answer taken, backend, confidence and latency; the
  outcome signal that arrives later (``record_outcome``) turns it into a label.
  ``export_dataset`` turns the table into the fine-tuning set.
* **Bootstrap on Haiku, promote per question, measured, reversible.** The
  policy file pins a backend per question. ``laya`` (Convai Innovations' local
  decision model, optional extra) joins when it beats Haiku on holdout — and
  joins the *request path* only when ``engram decide bench`` measures it
  under the policy's latency ceiling on this CPU.

Never raises into a caller: a missing backend returns ``None`` and the caller
keeps its pre-decide behaviour (fail open, like the hooks).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import config

logger = logging.getLogger(__name__)

POLICY_PATH_ENV = "ENGRAM_POLICY_PATH"
DECIDE_MODEL_ENV = "ENGRAM_LLM_DECIDE_MODEL"
BACKENDS = ("haiku", "laya")
KINDS = ("choice", "score", "yesno")
MAX_INPUT_CHARS = 4000


# ---------------------------------------------------------------------------
# Question registry (versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    name: str
    version: int
    kind: str
    instructions: str
    options: tuple[str, ...] = ()  # choice: labels; score: ignored
    scale: tuple[int, int] = (0, 1)  # score only (inclusive)
    option_help: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.name}@v{self.version}"


QUESTIONS: dict[str, Question] = {
    q.name: q
    for q in (
        Question(
            "worth_remembering", 1, "yesno",
            "Is this a durable, non-derivable fact worth storing in an engineering memory? "
            "Yes: recurring bugs, standard fixes, workflows, project constraints, strong "
            "preferences, decisions with a reason. No: temporary tasks, one-off questions, "
            "boilerplate, or anything a capable model could re-derive without being told.",
        ),
        Question(
            "test_passes", 1, "yesno",
            "Does the response satisfy the requirement? Judge the substance, not the wording.",
        ),
        Question(
            "is_duplicate", 1, "yesno",
            "Do these two memory entries describe the same underlying fact, mistake or "
            "workflow (so one should be merged into the other)? No if they differ in cause, "
            "fix, or scope.",
        ),
        Question(
            "route_rung", 1, "choice",
            "Which rung of the action ladder fits this task best?",
            options=("reflex", "recall", "reason"),
            option_help={
                "reflex": "an approved deterministic script exists for exactly this workflow",
                "recall": "a proven skill or pattern applies; follow its steps",
                "reason": "no prior art; reason from scratch and capture the outcome",
            },
        ),
    )
}


def get_question(name: str) -> Question:
    try:
        return QUESTIONS[name]
    except KeyError:
        raise KeyError(f"unknown decision question {name!r}; known: {sorted(QUESTIONS)}") from None


# ---------------------------------------------------------------------------
# Policy file
# ---------------------------------------------------------------------------

_DEFAULT_POLICY: dict[str, Any] = {
    "decide": {
        "default_backend": "haiku",
        "backends": {},  # "worth_remembering@v1": "laya"
        "thresholds": {},  # "worth_remembering@v1": 0.6  (yesno: P(true) >= threshold)
        "laya": {
            "model": "multilingual",  # Router alias: english | multilingual | typed-decisions
            "device": None,  # None = auto (cuda → mps → cpu); "cpu" to pin
            "models": None,  # {"multilingual": "/var/lib/engram/laya/v3"} for pinned local weights
            "max_len": None,
            "max_latency_ms": 300,  # request-path ceiling (spec: ~300 ms on the VM's CPU)
        },
    }
}


def policy_path() -> str:
    return os.environ.get(POLICY_PATH_ENV) or os.path.join(config.engram_dir(), "policy.json")


def load_policy() -> dict[str, Any]:
    """Policy with defaults filled in (``{}`` on disk is a valid policy)."""
    data: dict[str, Any] = {}
    try:
        with open(policy_path(), encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    merged = json.loads(json.dumps(_DEFAULT_POLICY))
    dec = data.get("decide")
    if not isinstance(dec, dict):
        dec = {}
    for k, v in dec.items():
        if k == "laya" and isinstance(v, dict):
            merged["decide"]["laya"].update(v)
        else:
            merged["decide"][k] = v
    return merged


def save_policy(policy: dict[str, Any]) -> str:
    path = policy_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(policy, f, indent=2, sort_keys=True)
    return path


def set_backend(question_key: str, backend: str) -> str:
    """Pin ``question@vN`` to a backend (``haiku`` | ``laya``). Reversible: a pointer change."""
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}")
    policy = load_policy()
    policy["decide"]["backends"][question_key] = backend
    return save_policy(policy)


def backend_for(q: Question, policy: dict[str, Any] | None = None) -> str:
    p = (policy or load_policy())["decide"]
    return str(p["backends"].get(q.key) or p["default_backend"] or "haiku")


def threshold_for(q: Question, policy: dict[str, Any] | None = None) -> float:
    p = (policy or load_policy())["decide"]
    try:
        return float(p["thresholds"].get(q.key, 0.5))
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------------------
# Result + parsing helpers
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    id: int | None
    question: str
    version: int
    kind: str
    answer: Any  # bool | str | int
    confidence: float
    backend: str
    model: str
    latency_ms: float
    low_confidence: bool = False

    @property
    def key(self) -> str:
        return f"{self.question}@v{self.version}"


def _input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _coerce_answer(q: Question, raw: Any) -> Any | None:
    """Normalize a backend's answer to the question's type; None when unusable."""
    if q.kind == "yesno":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw >= 0.5)
        m = re.match(r"\s*([a-záéíóúñ0-9]+)", str(raw).strip().lower())
        word = m.group(1) if m else ""
        if word in ("yes", "true", "y", "1", "sí", "si"):
            return True
        if word in ("no", "false", "n", "0"):
            return False
        return None
    if q.kind == "choice":
        s = str(raw).strip().lower()
        for opt in q.options:
            if s == opt.lower():
                return opt
        for opt in q.options:  # tolerate "answer: recall." style text
            if opt.lower() in s:
                return opt
        return None
    if q.kind == "score":
        try:
            n = int(round(float(str(raw).strip().split()[0])))
        except (TypeError, ValueError, IndexError):
            return None
        lo, hi = q.scale
        return max(lo, min(hi, n))
    return None


def _parse_llm_decision(q: Question, raw: str | None) -> tuple[Any, float] | None:
    """``{"answer": ..., "confidence": 0.x}`` preferred; bare answers tolerated."""
    if not raw:
        return None
    from .llm import parse_json_from_llm

    parsed = parse_json_from_llm(raw)
    if isinstance(parsed, dict) and "answer" in parsed:
        ans = _coerce_answer(q, parsed.get("answer"))
        if ans is None:
            return None
        try:
            conf = float(parsed.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        return ans, max(0.0, min(1.0, conf))
    ans = _coerce_answer(q, raw)
    return (ans, 0.5) if ans is not None else None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _format_question_prompt(q: Question) -> str:
    if q.kind == "yesno":
        shape = 'Reply with JSON only: {"answer": true|false, "confidence": <0..1>}.'
    elif q.kind == "choice":
        opts = ", ".join(
            f'"{o}"' + (f" ({q.option_help[o]})" if o in q.option_help else "") for o in q.options
        )
        shape = f'Choose exactly one of: {opts}. Reply with JSON only: {{"answer": "<option>", "confidence": <0..1>}}.'
    else:
        lo, hi = q.scale
        shape = f'Reply with JSON only: {{"answer": <integer {lo}..{hi}>, "confidence": <0..1>}}.'
    return f"{q.instructions}\n{shape} No prose."


def _haiku_decide(q: Question, text: str, *, timeout: int) -> tuple[Any, float, str] | None:
    """Bootstrap backend: the shared LLM layer, task ``decide`` (Haiku under Anthropic)."""
    from .llm import call_chat_completion, is_llm_available, resolve_llm_model

    if not is_llm_available():
        return None
    raw = call_chat_completion(
        [
            {"role": "system", "content": _format_question_prompt(q)},
            {"role": "user", "content": text},
        ],
        task="decide",
        temperature=0.0,
        max_tokens=60,
        timeout=timeout,
    )
    parsed = _parse_llm_decision(q, raw)
    if parsed is None:
        return None
    ans, conf = parsed
    return ans, conf, resolve_llm_model(task="decide")


def laya_installed() -> bool:
    try:
        import laya  # noqa: F401  # pyright: ignore[reportMissingImports]
    except ImportError:
        return False
    return True


_ROUTER: Any = None


def _laya_router(policy: dict[str, Any]) -> Any:
    global _ROUTER
    if _ROUTER is None:
        from laya import Router  # pyright: ignore[reportMissingImports]  # optional extra

        cfg = policy["decide"]["laya"]
        kwargs: dict[str, Any] = {"default": cfg.get("model") or "multilingual"}
        if cfg.get("device"):
            kwargs["device"] = cfg["device"]
        if cfg.get("models"):
            kwargs["models"] = cfg["models"]
        _ROUTER = Router(**kwargs)
    return _ROUTER


def _laya_question(q: Question) -> dict[str, Any]:
    if q.kind == "yesno":
        return {"type": "noul", "instructions": q.instructions}
    if q.kind == "choice":
        return {
            "type": "choice",
            "instructions": q.instructions,
            "criteria": {o: q.option_help.get(o, o) for o in q.options},
        }
    lo, hi = q.scale
    return {"type": "score", "instructions": q.instructions, "criteria": [str(i) for i in range(lo, hi + 1)]}


def _laya_decide(q: Question, text: str, policy: dict[str, Any]) -> tuple[Any, float, str, bool] | None:
    """Local decision model. Reads the documented answer shape defensively — verify
    against the installed version with ``engram decide bench --raw``."""
    cfg = policy["decide"]["laya"]
    router = _laya_router(policy)
    call: dict[str, Any] = {"model": cfg.get("model") or "multilingual"}
    if cfg.get("max_len"):
        call["max_len"] = int(cfg["max_len"])
    result = router.predict(text, {q.name: _laya_question(q)}, **call)
    ans = (result.get("answers") or {}).get(q.name) or {}
    routing = result.get("routing") or {}
    model = str(routing.get("model") or call["model"]) if isinstance(routing, dict) else call["model"]
    low = bool(ans.get("low_confidence"))
    if q.kind == "yesno":
        raw_p = ans.get("noul")
        if raw_p is None:
            return None
        p = float(raw_p)
        thr = threshold_for(q, policy)
        return (p >= thr), max(p, 1.0 - p), model, low
    probs = ans.get("probabilities") or ans.get("probs") or {}
    if q.kind == "choice":
        label = ans.get("choice", ans.get("answer"))
        coerced = _coerce_answer(q, label)
        if coerced is None:
            return None
        conf = ans.get("answer_confidence")
        if conf is None and isinstance(probs, dict):
            conf = probs.get(label)
        return coerced, float(conf or 0.0), model, low
    level = ans.get("score", ans.get("answer"))
    coerced = _coerce_answer(q, level)
    if coerced is None:
        return None
    conf = ans.get("answer_confidence")
    if conf is None and isinstance(probs, dict):
        conf = probs.get(str(level))
    return coerced, float(conf or 0.0), model, low


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


def decide(
    question: str,
    state: str,
    *,
    context: str | None = None,
    session_id: str | None = None,
    backend: str | None = None,
    db_path=None,
    timeout: int = 30,
) -> Decision | None:
    """Answer *question* over *state*; log it; None when no backend can answer.

    ``backend`` forces one (bench/eval); otherwise the policy decides. A ``laya``
    failure falls back to ``haiku`` so a broken local model never silences a job.
    """
    q = get_question(question)
    policy = load_policy()
    chosen = backend or backend_for(q, policy)
    text = state if not context else f"{state}\n\n[context]\n{context}"
    text = text[:MAX_INPUT_CHARS]
    t0 = time.perf_counter()
    outcome: tuple[Any, float, str, bool] | None = None
    used = chosen
    if chosen == "laya":
        if laya_installed():
            try:
                outcome = _laya_decide(q, text, policy)
            except Exception:
                logger.warning("laya backend failed for %s; falling back to haiku", q.key, exc_info=True)
        else:
            logger.warning("policy pins %s to laya but the package is not installed; using haiku", q.key)
        if outcome is None and backend is None:
            used = "haiku"
    if outcome is None and used == "haiku":
        h = _haiku_decide(q, text, timeout=timeout)
        outcome = (h[0], h[1], h[2], False) if h else None
    if outcome is None:
        return None
    ans, conf, model, low = outcome
    latency = (time.perf_counter() - t0) * 1000.0
    d = Decision(None, q.name, q.version, q.kind, ans, float(conf), used, model, latency, low)
    d.id = _log(d, text, session_id=session_id, db_path=db_path)
    return d


def _log(d: Decision, text: str, *, session_id: str | None, db_path=None) -> int | None:
    from .database import get_connection
    from .search_audit import append_decision_audit

    row_id: int | None = None
    try:
        with get_connection(db_path) as conn:
            cur = conn.execute(
                """INSERT INTO decisions
                   (question, version, kind, input_hash, input_text, answer, confidence,
                    backend, model, latency_ms, low_confidence, session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    d.question, d.version, d.kind, _input_hash(text), text,
                    json.dumps(d.answer), d.confidence, d.backend, d.model,
                    round(d.latency_ms, 2), 1 if d.low_confidence else 0, session_id,
                ),
            )
            row_id = int(cur.lastrowid) if cur.lastrowid is not None else None
    except Exception:
        logger.debug("decision log write failed", exc_info=True)
    append_decision_audit(
        question=d.key, backend=d.backend, model=d.model,
        latency_ms=d.latency_ms, confidence=d.confidence, session_id=session_id,
    )
    return row_id


def record_outcome(decision_id: int, outcome: str, *, source: str = "manual", db_path=None) -> bool:
    """Attach the later signal (``kept`` / ``dismissed`` / ``correct`` / ``wrong`` / …) to a logged decision."""
    from .database import get_connection

    with get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE decisions SET outcome = ?, outcome_source = ?, outcome_at = datetime('now') WHERE id = ?",
            (outcome.strip().lower(), source, int(decision_id)),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Measurement + dataset
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[idx]


def decision_stats(*, question: str | None = None, db_path=None) -> dict[str, Any]:
    """Per question@version: volume, backends, latency p50/p95, outcomes, and
    backend agreement on inputs both answered (the promotion evidence)."""
    from .database import get_connection

    where, params = ("WHERE question = ?", [question]) if question else ("", [])
    with get_connection(db_path) as conn:
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM decisions {where} ORDER BY id", params).fetchall()]
    out: dict[str, Any] = {}
    for r in rows:
        key = f"{r['question']}@v{r['version']}"
        b = out.setdefault(key, {"n": 0, "by_backend": {}, "outcomes": {}, "_lat": {}, "_ans": {}})
        b["n"] += 1
        b["by_backend"][r["backend"]] = b["by_backend"].get(r["backend"], 0) + 1
        b["_lat"].setdefault(r["backend"], []).append(float(r["latency_ms"] or 0))
        if r.get("outcome"):
            b["outcomes"][r["outcome"]] = b["outcomes"].get(r["outcome"], 0) + 1
        b["_ans"].setdefault(r["input_hash"], {})[r["backend"]] = r["answer"]
    for b in out.values():
        b["latency_ms"] = {
            be: {"p50": round(_percentile(v, 50), 1), "p95": round(_percentile(v, 95), 1), "n": len(v)}
            for be, v in b.pop("_lat").items()
        }
        pairs = [a for a in b.pop("_ans").values() if "haiku" in a and "laya" in a]
        b["agreement"] = {
            "pairs": len(pairs),
            "rate": round(sum(1 for a in pairs if a["haiku"] == a["laya"]) / len(pairs), 3) if pairs else None,
        }
    return out


def export_dataset(question: str, path: str, *, only_with_outcome: bool = False, db_path=None) -> int:
    """Write the fine-tuning set for *question* as JSONL: one row per logged decision,
    labelled by the outcome when one exists, else by the answer taken (Haiku bootstrap).

    Row shape: ``{"state", "questions": {name: laya-style spec}, "labels": {name: label},
    "meta": {...}}``. Align with laya's documented eval/train schema before the
    first Kaggle run; the fields are kept flat on purpose so that is a one-line map.
    """
    from .database import get_connection

    q = get_question(question)
    sql = "SELECT * FROM decisions WHERE question = ? AND version = ?"
    if only_with_outcome:
        sql += " AND outcome IS NOT NULL"
    n = 0
    with get_connection(db_path) as conn, open(path, "w", encoding="utf-8") as f:
        for r in conn.execute(sql + " ORDER BY id", (q.name, q.version)).fetchall():
            answer = json.loads(r["answer"])
            label: Any = answer
            if r["outcome"] in ("kept", "correct", "yes", "true"):
                label = True if q.kind == "yesno" else answer
            elif r["outcome"] in ("dismissed", "wrong", "no", "false"):
                label = False if q.kind == "yesno" else None
            if label is None:
                continue
            f.write(json.dumps({
                "state": r["input_text"],
                "questions": {q.name: _laya_question(q)},
                "labels": {q.name: label},
                "meta": {
                    "decision_id": r["id"], "backend": r["backend"], "model": r["model"],
                    "confidence": r["confidence"], "outcome": r["outcome"], "created_at": r["created_at"],
                    "question_version": q.key,
                },
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def bench(question: str, *, limit: int = 50, db_path=None) -> dict[str, Any]:
    """Re-run the last *limit* logged inputs through both backends and report
    latency p50/p95 and agreement — the measured gate for moving a question to
    Laya and for letting Laya into the request path (policy ``max_latency_ms``)."""
    from .database import get_connection

    q = get_question(question)
    with get_connection(db_path) as conn:
        inputs = [
            r["input_text"]
            for r in conn.execute(
                "SELECT input_text FROM decisions WHERE question = ? AND version = ? "
                "GROUP BY input_hash ORDER BY MAX(id) DESC LIMIT ?",
                (q.name, q.version, int(limit)),
            ).fetchall()
        ]
    report: dict[str, Any] = {"question": q.key, "inputs": len(inputs), "laya_installed": laya_installed()}
    lat: dict[str, list[float]] = {"haiku": [], "laya": []}
    agree = total = 0
    policy = load_policy()
    for text in inputs:
        answers: dict[str, Any] = {}
        for be in ("haiku", "laya"):
            if be == "laya" and not laya_installed():
                continue
            d = decide(q.name, text, backend=be, db_path=db_path)
            if d is not None:
                lat[be].append(d.latency_ms)
                answers[be] = d.answer
        if "haiku" in answers and "laya" in answers:
            total += 1
            agree += int(answers["haiku"] == answers["laya"])
    for be, v in lat.items():
        report[be] = {"n": len(v), "p50_ms": round(_percentile(v, 50), 1), "p95_ms": round(_percentile(v, 95), 1)}
    report["agreement"] = {"pairs": total, "rate": round(agree / total, 3) if total else None}
    ceiling = float(policy["decide"]["laya"]["max_latency_ms"] or 300)
    report["laya_request_path_ok"] = bool(lat["laya"]) and _percentile(lat["laya"], 95) <= ceiling
    report["max_latency_ms"] = ceiling
    return report
