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
                out["searches"] += 1
                src = rec.get("source") or "unknown"
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
