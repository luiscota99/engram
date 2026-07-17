"""Optional append-only JSONL audit log for hybrid `search()` calls.

Set ``ENGRAM_AUDIT_LOG`` to a file path to record each search (query, top hits,
source). Disabled when the variable is unset.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from . import config


def append_search_audit(
    *,
    query: str,
    results: list[dict[str, Any]],
    semantic_status: str,
    source: str,
    item_type: str | None,
    tags: list[str] | None,
    limit: int,
    project_path: str | None,
) -> None:
    path = config.audit_log_path()
    if not path:
        return
    top = [
        {"item_type": r.get("item_type"), "item_id": r.get("item_id"), "title": (r.get("title") or "")[:120]}
        for r in results[: min(5, len(results))]
    ]
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "query": (query or "")[:500],
        "semantic_status": semantic_status,
        "item_type_filter": item_type,
        "tags_filter": tags,
        "limit": limit,
        "project_path": project_path,
        "top_k": top,
        "result_count": len(results),
    }
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


# One rotation generation at ~5MB keeps ROI reads bounded while preserving a
# recent window plus one archived file of history.
AUDIT_ROTATE_BYTES = 5 * 1024 * 1024


def rotate_audit_log_if_needed(path: str | None = None) -> bool:
    """Rotate the audit JSONL to ``<path>.1`` once it exceeds the size cap.

    Append-only logs otherwise grow forever, and summarize_audit_log re-reads
    the whole file per ROI report. Returns True when a rotation happened.
    """
    path = path or config.audit_log_path()
    if not path:
        return False
    try:
        if os.path.getsize(path) < AUDIT_ROTATE_BYTES:
            return False
        os.replace(path, path + ".1")
        return True
    except OSError:
        return False


def append_injection_audit(kind: str, *, tokens_est: int, kept: int) -> None:
    """Record one post-gate injection outcome (the COST side of the ledger).

    ``kind`` is ``recall`` or ``guard``. ``kept=0, tokens_est=0`` records a
    gate suppression — candidates existed but nothing was injected. Without
    these records the ROI report can only see pre-gate search activity, which
    is why its old "hit rate" was a misleading 100%.
    """
    path = config.audit_log_path()
    if not path:
        return
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": f"{kind}_inject",
        "tokens_est": int(tokens_est),
        "kept": int(kept),
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def summarize_audit_log(path: str | None = None) -> dict:
    """Aggregate the search-audit JSONL into ROI-report stats.

    Returns ``{"enabled": bool, "path": str|None, "searches": int,
    "by_source": {src: n}, "with_hit": int, "zero_result": int,
    "hit_rate": float|None, "top_queries": [(query, n)], "first_ts", "last_ts"}``.
    Missing/empty/unreadable log → zeros, never raises.
    """
    if path is None:
        path = config.audit_log_path()
    out: dict[str, Any] = {
        "enabled": bool(path),
        "path": path,
        "searches": 0,
        "by_source": {},
        "with_hit": 0,
        "zero_result": 0,
        "hit_rate": None,
        "top_queries": [],
        "first_ts": None,
        "last_ts": None,
        # Post-gate injection ledger: evals (gate decisions), injected
        # (kept>0), tokens_est_total (context actually added to agents).
        "injection": {
            "recall": {"evals": 0, "injected": 0, "tokens_est_total": 0},
            "guard": {"evals": 0, "injected": 0, "tokens_est_total": 0},
        },
    }
    if not path or not os.path.isfile(path):
        return out

    query_counts: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                src = rec.get("source") or "unknown"
                if src.endswith("_inject"):
                    kind = src[: -len("_inject")]
                    bucket = out["injection"].get(kind)
                    if bucket is not None:
                        bucket["evals"] += 1
                        if (rec.get("kept") or 0) > 0:
                            bucket["injected"] += 1
                            bucket["tokens_est_total"] += int(rec.get("tokens_est") or 0)
                    continue  # injection records are not searches
                out["searches"] += 1
                out["by_source"][src] = out["by_source"].get(src, 0) + 1
                if (rec.get("result_count") or 0) > 0:
                    out["with_hit"] += 1
                else:
                    out["zero_result"] += 1
                q = (rec.get("query") or "").strip()
                if q:
                    query_counts[q] = query_counts.get(q, 0) + 1
                ts = rec.get("ts")
                if ts:
                    if out["first_ts"] is None or ts < out["first_ts"]:
                        out["first_ts"] = ts
                    if out["last_ts"] is None or ts > out["last_ts"]:
                        out["last_ts"] = ts
    except OSError:
        return out

    if out["searches"]:
        out["hit_rate"] = round(out["with_hit"] / out["searches"], 3)
    out["top_queries"] = sorted(query_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return out
