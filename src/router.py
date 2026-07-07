"""Action-ladder router — one lookup for the cheapest correct way to do a task.

The ladder, cheapest first:

1. **reflex** — an approved, deterministic script exists for this exact
   workflow. Invoke the ``reflex_<name>`` MCP tool (~50 tokens, zero variance).
2. **recall** — a proven skill/pattern matches. Follow its steps instead of
   re-deriving them (~200 tokens of guidance vs thousands of reasoning tokens).
3. **reason** — no prior art. Reason from scratch, then capture the outcome so
   the *next* occurrence lands on a higher rung.

Every recurring action should migrate down this ladder over time; promotion is
earned through measured reuse (see src/reflex.py), and failures demote.

The router's response is budgeted: one tool call, ≤ ~200 tokens out, replacing
the search → read → decide loop an agent would otherwise spend several calls on.
"""
from __future__ import annotations

import logging

from .database import get_connection
from .search import search

logger = logging.getLogger(__name__)

# Utility-score floor below which a match is considered noise rather than
# prior art (semantic base score is 100; weak lexical matches sit under 60).
RECALL_MIN_SCORE = 60.0

_SNIPPET_CHARS = 280


def route_task(task: str, *, db_path=None, project_path=None) -> dict:
    """Return the cheapest correct rung for *task*.

    Result dict: ``{"rung": "reflex"|"recall"|"reason", "text": <agent-facing>,
    "matches": [...], "warnings": [...]}``.
    """
    results = search(
        task,
        limit=5,
        db_path=db_path,
        project_path=project_path,
        skip_audit=False,
        audit_source="route",
    )

    # Map matched skills to approved reflexes
    skill_ids = [int(r["item_id"]) for r in results if r["item_type"] == "skill"]
    reflex_by_skill: dict[int, dict] = {}
    if skill_ids:
        with get_connection(db_path) as conn:
            placeholders = ",".join("?" * len(skill_ids))
            rows = conn.execute(
                f"SELECT skill_id, name, description FROM reflexes "
                f"WHERE approved_at IS NOT NULL AND skill_id IN ({placeholders})",
                skill_ids,
            ).fetchall()
            reflex_by_skill = {r["skill_id"]: dict(r) for r in rows}

    warnings = [
        {
            "item_id": r["item_id"],
            "title": r["title"],
            "snippet": (r.get("snippet") or "")[:_SNIPPET_CHARS],
        }
        for r in results
        if r["item_type"] == "mistake" and r.get("utility_score", 0) >= RECALL_MIN_SCORE
    ][:2]

    # Rung 1: reflex
    for r in results:
        if r["item_type"] == "skill" and int(r["item_id"]) in reflex_by_skill:
            reflex = reflex_by_skill[int(r["item_id"])]
            return {
                "rung": "reflex",
                "reflex": reflex["name"],
                "matches": [{"item_type": "skill", "item_id": r["item_id"], "title": r["title"]}],
                "warnings": warnings,
                "text": _fmt_reflex(reflex, warnings),
            }

    # Rung 2: recall
    recall = [
        r
        for r in results
        if r["item_type"] in ("skill", "pattern") and r.get("utility_score", 0) >= RECALL_MIN_SCORE
    ][:2]
    if recall:
        return {
            "rung": "recall",
            "matches": [
                {"item_type": r["item_type"], "item_id": r["item_id"], "title": r["title"]}
                for r in recall
            ],
            "warnings": warnings,
            "text": _fmt_recall(recall, warnings),
        }

    # Rung 3: reason (with warnings still surfaced — correctness first)
    return {
        "rung": "reason",
        "matches": [],
        "warnings": warnings,
        "text": _fmt_reason(warnings),
    }


def _fmt_warnings(warnings: list[dict]) -> str:
    if not warnings:
        return ""
    lines = ["", "Known pitfalls:"]
    for w in warnings:
        lines.append(f"  ⚠ [MISTAKE {w['item_id']}] {w['title']}")
    return "\n".join(lines)


def _fmt_reflex(reflex: dict, warnings: list[dict]) -> str:
    return (
        f"REFLEX AVAILABLE — do not re-derive this workflow.\n"
        f"Call the MCP tool `reflex_{reflex['name']}` (params as key/value args).\n"
        f"{reflex['description'][:200]}"
        + _fmt_warnings(warnings)
    )


def _fmt_recall(recall: list[dict], warnings: list[dict]) -> str:
    lines = ["PRIOR ART FOUND — follow these steps instead of re-deriving:"]
    for r in recall:
        snippet = (r.get("snippet") or "").replace("\n", " ")[:_SNIPPET_CHARS]
        lines.append(f"  [{r['item_type'].upper()} {r['item_id']}] {r['title']}")
        if snippet:
            lines.append(f"    {snippet}")
    lines.append(
        "Use memory_read_item for full detail; record_usage after applying. "
        "If this workflow keeps recurring, it may earn promotion to a reflex."
    )
    return "\n".join(lines) + _fmt_warnings(warnings)


def _fmt_reason(warnings: list[dict]) -> str:
    return (
        "NO PRIOR ART — reason through this from scratch.\n"
        "Afterward, capture the outcome (memory_add / memory_suggest_capture) so the "
        "next occurrence lands on a cheaper rung."
        + _fmt_warnings(warnings)
    )
