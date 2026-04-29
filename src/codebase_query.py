"""Helpers for matching natural-language questions against codebase knowledge rows."""
from __future__ import annotations

import re
from typing import Any, Sequence

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def codebase_query_tokens(query: str) -> list[str]:
    """Split a free-text question into path/summary-friendly search tokens."""
    if not query or not str(query).strip():
        return []

    seen: dict[str, None] = {}
    out: list[str] = []

    def _keep(tok: str) -> bool:
        if len(tok) >= 4:
            return True
        if len(tok) == 3 and (tok.isupper() or any(c.isdigit() for c in tok)):
            return True
        return False

    for m in _TOKEN_RE.finditer(query):
        raw = m.group(0)
        candidates = {raw}
        if "_" in raw:
            candidates.update(p for p in raw.split("_") if p)
        if "." in raw:
            candidates.update(p for p in raw.split(".") if p)
        for c in candidates:
            c = c.strip("._-")
            if not _keep(c):
                continue
            key = c.lower()
            if key not in seen:
                seen[key] = None
                out.append(c)
    return out


def score_codebase_row(file_path: str, summary: str | None, tokens: Sequence[str]) -> int:
    """How many tokens appear in path or summary (case-insensitive)."""
    if not tokens:
        return 0
    hay = f"{file_path} {summary or ''}".lower()
    return sum(1 for t in tokens if t.lower() in hay)


def fetch_codebase_rows_for_query(
    conn: Any,
    project_id: int,
    query: str | None,
) -> list[Any]:
    """
    Return codebase_knowledge rows for project_id, filtered by query.
    Empty query = all rows. Non-empty query uses token OR-match, ranked by token coverage.
    """
    q = (query or "").strip()
    tokens = codebase_query_tokens(q)
    if not q:
        return list(
            conn.execute(
                "SELECT file_path, summary, exports, dependencies FROM codebase_knowledge "
                "WHERE project_id = ? ORDER BY file_path",
                (project_id,),
            ).fetchall()
        )

    if not tokens:
        # Fallback: single phrase match (legacy behavior)
        like = f"%{q}%"
        return list(
            conn.execute(
                "SELECT file_path, summary, exports, dependencies FROM codebase_knowledge "
                "WHERE project_id = ? AND (file_path LIKE ? OR summary LIKE ?) ORDER BY file_path",
                (project_id, like, like),
            ).fetchall()
        )

    or_clauses = " OR ".join(
        ["(LOWER(file_path) LIKE LOWER(?) OR LOWER(summary) LIKE LOWER(?))"] * len(tokens)
    )
    params: list[Any] = [project_id]
    for t in tokens:
        like = f"%{t}%"
        params.extend((like, like))

    rows = list(
        conn.execute(
            f"SELECT DISTINCT file_path, summary, exports, dependencies FROM codebase_knowledge "
            f"WHERE project_id = ? AND ({or_clauses})",
            tuple(params),
        ).fetchall()
    )

    scored = [(score_codebase_row(r["file_path"], r["summary"], tokens), r) for r in rows]
    scored.sort(key=lambda x: (-x[0], x[1]["file_path"]))
    return [r for _, r in scored]
