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
