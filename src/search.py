"""
Search module — FTS5 full-text search + semantic vector search with
multi-factor ranking via src/ranking.py.
"""

from __future__ import annotations

import json

from .database import get_connection, get_or_create_project, get_project_affinities
from .embeddings import embed_text
from .ranking import rank_results


def _get_stale_rowids(conn) -> set:
    """Return the set of fts_rowids whose embeddings are stale or failed."""
    try:
        rows = conn.execute(
            "SELECT fts_rowid FROM embedding_status WHERE status IN ('stale', 'failed')"
        ).fetchall()
        return {r["fts_rowid"] for r in rows}
    except Exception:
        return set()


def semantic_search(query, item_type=None, tags=None, limit=10, db_path=None):
    """Search vec_memory using KNN vector search.

    Requires sqlite_vec extension and a running Ollama instance.
    Returns (results, status) where status is one of:
        "ok"          — semantic search ran and returned results (or a valid empty set)
        "unavailable" — Ollama/embedding failed; caller should fall back to lexical-only
        "degraded"    — vec extension unavailable or query error
    """
    embedding = embed_text(query)
    if not embedding:
        return [], "unavailable"

    with get_connection(db_path) as conn:
        try:
            conditions = []
            params = []
            if item_type:
                conditions.append("f.item_type = ?")
                params.append(item_type)
            if tags:
                for tag in tags:
                    conditions.append("f.tags MATCH ?")
                    params.append(tag.strip())

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            sql = f"""
                WITH matches AS (
                    SELECT rowid, distance
                    FROM vec_memory
                    WHERE embedding MATCH ? AND k = ?
                )
                SELECT f.item_type, f.item_id, f.title, f.content as snippet,
                       f.tags, m.rowid as fts_rowid, m.distance
                FROM matches m
                JOIN memory_fts f ON m.rowid = f.rowid
                {where}
                ORDER BY m.distance
            """
            rows = conn.execute(sql, [json.dumps(embedding), limit * 2] + params).fetchall()
            results = []
            for row in rows:
                results.append({
                    "item_type": row["item_type"],
                    "item_id": row["item_id"],
                    "title": row["title"],
                    "snippet": row["snippet"] or "",
                    "tags": row["tags"],
                    "rank": row["distance"],
                    "rowid": row["fts_rowid"],
                    "is_semantic": True,
                })
            return results[:limit], "ok"
        except Exception:
            return [], "degraded"


def search(query, item_type=None, tags=None, limit=20, project_path=None, db_path=None):
    """Hybrid Search: FTS5 lexical + KNN semantic, ranked by multi-factor utility score.

    Returns a list of result dicts. Each result has a ``utility_score`` field after ranking.
    The list itself carries a ``semantic_status`` attribute (``"ok"``, ``"unavailable"``, or
    ``"degraded"``) so callers can surface degradation warnings without checking embeddings
    separately.
    """
    results = []
    seen = set()
    semantic_status = "ok"

    # 1. Semantic Search
    if query and query.strip():
        sem_results, semantic_status = semantic_search(query, item_type, tags, limit=limit, db_path=db_path)
        for r in sem_results:
            key = f"{r['item_type']}-{r['item_id']}"
            if key not in seen:
                seen.add(key)
                results.append(r)

    # 2. Lexical FTS5 Search
    with get_connection(db_path) as conn:
        conditions = []
        params = []
        if item_type:
            conditions.append("item_type = ?")
            params.append(item_type)
        if tags:
            for tag in tags:
                conditions.append("tags MATCH ?")
                params.append(tag.strip())

        where_extra = ("AND " + " AND ".join(conditions)) if conditions else ""

        if query and query.strip():
            fts_query = " OR ".join(f'"{term}"' for term in query.strip().split() if term)
            sql = f"""
                SELECT item_type, item_id, title, content as snippet, tags, rank,
                       rowid as fts_rowid
                FROM memory_fts
                WHERE memory_fts MATCH ? {where_extra}
                ORDER BY rank
                LIMIT ?
            """
            rows = conn.execute(sql, [fts_query] + params + [limit]).fetchall()
        else:
            filter_where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            sql = f"""
                SELECT item_type, item_id, title, content as snippet, tags, 0 as rank,
                       rowid as fts_rowid
                FROM memory_fts
                {filter_where}
                ORDER BY rowid DESC
                LIMIT ?
            """
            rows = conn.execute(sql, params + [limit]).fetchall()

        for row in rows:
            key = f"{row['item_type']}-{row['item_id']}"
            if key not in seen:
                seen.add(key)
                results.append({
                    "item_type": row["item_type"],
                    "item_id": row["item_id"],
                    "title": row["title"],
                    "snippet": row["snippet"] or "",
                    "tags": row["tags"],
                    "rank": row["rank"],
                    "rowid": row["fts_rowid"],
                    "is_semantic": False,
                })

        stale_rowids = _get_stale_rowids(conn)

    # 3. Batch-fetch usage_count and last_used_at per type (avoids N+1 queries)
    table_map = {
        "mistake": "mistakes",
        "pattern": "patterns",
        "skill": "skills",
        "conversation": "conversations",
        "prompt": "prompts",
    }
    usage_counts = {}
    last_used_map = {}

    with get_connection(db_path) as conn:
        for itype, table in table_map.items():
            ids = [int(r["item_id"]) for r in results if r["item_type"] == itype]
            if ids:
                placeholders = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"SELECT id, usage_count, last_used_at FROM {table} WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
                for row in rows:
                    key = (itype, row["id"])
                    usage_counts[key] = row["usage_count"] or 0
                    last_used_map[key] = row["last_used_at"]

    # 4. Project affinity
    affinities = {}
    if project_path:
        try:
            project = get_or_create_project(project_path, db_path=db_path)
            affinities = get_project_affinities(results, project["id"], db_path=db_path)
        except Exception:
            pass

    # 5. Rank using multi-factor scoring
    results = rank_results(
        results=results,
        usage_counts=usage_counts,
        last_used_map=last_used_map,
        affinities=affinities,
        query=query or "",
        stale_rowids=stale_rowids,
    )

    final = results[:limit]
    # Attach semantic status so MCP / CLI callers can surface degradation without a
    # separate health check.  We use a plain attribute on the list object.
    final.semantic_status = semantic_status  # type: ignore[attr-defined]
    return final


def get_recent(limit=10, item_type=None, db_path=None):
    """Get the most recent entries across all types."""
    with get_connection(db_path) as conn:
        type_filter = ""
        params = []
        if item_type:
            type_filter = "WHERE item_type = ?"
            params.append(item_type)

        sql = f"""
            SELECT item_type, item_id, title, tags
            FROM memory_fts
            {type_filter}
            ORDER BY rowid DESC
            LIMIT ?
        """
        rows = conn.execute(sql, params + [limit]).fetchall()
        return [dict(r) for r in rows]


def get_stats(db_path=None):
    """Return counts of each item type, total tags, and embedding health."""
    from .database import get_embedding_stats

    with get_connection(db_path) as conn:
        stats = {}
        for table, label in [
            ("mistakes", "mistakes"),
            ("patterns", "patterns"),
            ("skills", "skills"),
            ("conversations", "conversations"),
            ("prompts", "prompts"),
            ("tags", "tags"),
        ]:
            count = conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()["c"]
            stats[label] = count

        fts_count = conn.execute("SELECT COUNT(*) as c FROM memory_fts").fetchone()["c"]
        stats["fts_indexed"] = fts_count

    stats["embeddings"] = get_embedding_stats(db_path)
    return stats
