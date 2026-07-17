"""Mine real-usage queries into labeled benchmark entries — the user labels.

The realistic-benchmark problem: hand-invented queries measure a corpus the
author imagined, and 5 labeled real queries prove nothing (toy-benchmark
false confidence). The audit log records the *actual* query distribution —
every real search, as it happened. This module samples recent audit queries
the label set doesn't cover yet; ``engram bench-label`` shows each with its
current top hits and the user picks the correct answer (or marks abstention,
or skips). Confirmed labels append to ``evals/real_queries.json`` in the
bench's grading format — strongest form: (expected_type, expected_item_id),
plus a title fallback so labels survive re-imports.

The label set grows with real usage; nothing is ever labeled without the
user's explicit choice.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

# Sources that carry genuine user/agent queries. "guard" is excluded — those
# are tool-call payloads (commands, file contents), not retrieval questions.
LABELABLE_SOURCES = ("cli", "mcp", "hook")

MIN_QUERY_WORDS = 3
MAX_QUERY_CHARS = 300


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def load_label_set(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("queries", [])
    return data if isinstance(data, list) else []


def mine_candidates(
    audit_path: str,
    existing: list[dict],
    *,
    limit: int = 5,
) -> list[dict]:
    """Recent unlabeled real queries from the audit log, newest first.

    Filters: labelable sources only, minimum length, no injected/system
    payloads (lines starting with '<'), dedup against the existing label set
    and within the batch (normalized text).
    """
    seen = {_normalize(q.get("query", "")) for q in existing}
    seen.discard("")
    candidates: list[dict] = []
    try:
        with open(audit_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    for line in reversed(lines):
        if len(candidates) >= limit:
            break
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("source") not in LABELABLE_SOURCES:
            continue
        query = (rec.get("query") or "").strip()
        norm = _normalize(query)
        if (
            not norm
            or norm in seen
            or query.lstrip().startswith("<")
            or len(query) > MAX_QUERY_CHARS
            or len(norm.split()) < MIN_QUERY_WORDS
        ):
            continue
        seen.add(norm)
        candidates.append({"query": query, "ts": rec.get("ts"), "source": rec.get("source")})
    return candidates


def _slug(query: str, existing_ids: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")[:40] or "query"
    slug, i = f"mined_{base}", 2
    while slug in existing_ids:
        slug, i = f"mined_{base}_{i}", i + 1
    return slug


def build_label(
    query: str,
    *,
    existing: list[dict],
    item: dict | None = None,
    abstention: bool = False,
) -> dict:
    """One label entry in the bench's grading format.

    ``item`` (a search-result row) → id-grading label with title fallback;
    ``abstention=True`` → the query is expected to retrieve nothing relevant.
    """
    ids = {str(q["id"]) for q in existing if q.get("id")}
    entry: dict = {
        "id": _slug(query, ids),
        "query": query,
        "category": "usage_mined",
        "source": "audit_log",
        "labeled_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    if abstention:
        entry["expect_abstention"] = True
        return entry
    if not item:
        raise ValueError("either item or abstention is required")
    entry["expected_type"] = item.get("item_type")
    entry["expected_item_id"] = int(item["item_id"])
    title = (item.get("title") or "").strip()
    if title:
        entry["expected_title_contains"] = title[:60]
    return entry


def append_labels(path: str, entries: list[dict]) -> int:
    """Append confirmed labels atomically. Returns the new total count."""
    current = load_label_set(path)
    current.extend(entries)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return len(current)
