"""Mem0-style extract ops: ADD / UPDATE / DELETE / NOOP — destructive via inbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .database import check_duplicate_before_add
from .embeddings import embed_text
from .inbox import file_item

OpName = Literal["ADD", "UPDATE", "DELETE", "NOOP"]


@dataclass
class ExtractOp:
    op: OpName
    title: str
    summary: str
    item_type: str = "pattern"
    target_type: str | None = None
    target_id: int | None = None
    reason: str = ""


def classify_extract_candidate(
    title: str,
    summary: str,
    *,
    item_type: str = "pattern",
    db_path=None,
) -> ExtractOp:
    """Compare a candidate fact against the store and choose an op.

    Destructive UPDATE/DELETE are proposals only — callers should file inbox
    decisions rather than applying automatically.
    """
    title = (title or "").strip()
    summary = (summary or "").strip()
    if not title and not summary:
        return ExtractOp("NOOP", title or "", summary, item_type=item_type, reason="empty")

    content = f"{title}\n{summary}".strip()
    dedup = check_duplicate_before_add(content, item_type, name=title or None, db_path=db_path)
    if dedup.get("exact_match") and dedup.get("duplicates"):
        d0 = dedup["duplicates"][0]
        return ExtractOp(
            "NOOP",
            title,
            summary,
            item_type=item_type,
            target_type=d0.get("item_type"),
            target_id=int(d0["item_id"]),
            reason="exact_name_match",
        )
    if dedup.get("duplicates"):
        d0 = dedup["duplicates"][0]
        sim = float(d0.get("similarity") or 0)
        # Near-duplicate with high similarity → UPDATE (supersede) proposal
        if sim >= 0.92:
            return ExtractOp(
                "UPDATE",
                title,
                summary,
                item_type=item_type,
                target_type=d0.get("item_type"),
                target_id=int(d0["item_id"]),
                reason=f"near_duplicate:{sim:.3f}",
            )
        if sim >= 0.85 and _contradiction(summary, d0.get("title") or ""):
            return ExtractOp(
                "DELETE",
                title,
                summary,
                item_type=item_type,
                target_type=d0.get("item_type"),
                target_id=int(d0["item_id"]),
                reason=f"contradiction:{sim:.3f}",
            )
    # Embedding path optional — if unavailable, ADD
    vec = embed_text(content)
    if vec is None:
        return ExtractOp("ADD", title, summary, item_type=item_type, reason="no_embed_add")
    return ExtractOp("ADD", title, summary, item_type=item_type, reason="novel")


def _contradiction(a: str, b: str) -> bool:
    a_l, b_l = a.lower(), b.lower()
    markers = (" no longer ", " deprecated ", " instead of ", " moved from ", " replaced ")
    return any(m in a_l for m in markers) or any(m in b_l for m in markers)


def propose_extract_ops(ops: list[ExtractOp], *, db_path=None) -> list[int]:
    """File inbox decisions for UPDATE/DELETE; return decision ids."""
    ids: list[int] = []
    for op in ops:
        if op.op not in ("UPDATE", "DELETE"):
            continue
        body = (
            f"Proposed {op.op} for {op.target_type}#{op.target_id}\n"
            f"Title: {op.title}\nSummary: {op.summary}\nReason: {op.reason}"
        )
        finding = f"extract-op:{op.op}:{op.target_type}:{op.target_id}:{op.title[:40]}"
        item_id = file_item(
            kind="decision",
            title=f"Extract {op.op}: {op.title[:60]}",
            body=body,
            source="extract_ops",
            finding_key=finding,
            severity="warning",
            db_path=db_path,
        )
        if item_id:
            ids.append(int(item_id))
    return ids
