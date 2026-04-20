"""
Search module — FTS5 full-text search with ranking and filtering.
"""

import json

from .database import get_connection
from .embeddings import embed_text


def semantic_search(query, item_type=None, tags=None, limit=10, db_path=None):
    """
    Search vec_memory using KNN vector search.
    Requires sqlite-vec extension and a running Ollama instance.
    """
    embedding = embed_text(query)
    if not embedding:
        return []

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

            where = ""
            if conditions:
                where = "WHERE " + " AND ".join(conditions)

            sql = f"""
                WITH matches AS (
                    SELECT rowid, distance
                    FROM vec_memory
                    WHERE embedding MATCH ? AND k = ?
                )
                SELECT f.item_type, f.item_id, f.title, f.content as snippet, f.tags, m.distance
                FROM matches m
                JOIN memory_fts f ON m.rowid = f.rowid
                {where}
                ORDER BY m.distance
            """
            rows = conn.execute(sql, [json.dumps(embedding), limit * 2] + params).fetchall()
            results = []
            for row in rows:
                results.append(
                    {
                        "item_type": row["item_type"],
                        "item_id": row["item_id"],
                        "title": row["title"],
                        "snippet": row["snippet"] or "",
                        "tags": row["tags"],
                        "rank": row["distance"],
                        "is_semantic": True,
                    }
                )
            return results[:limit]
        except Exception:
            return []


def search(query, item_type=None, tags=None, limit=20, db_path=None):
    """
    Hybrid Search: Combines FTS5 lexical matching with KNN Semantic Vector matching.
    """
    results = []
    seen = set()

    # 1. Semantic Search (Only if query text exists)
    if query and query.strip():
        semantic_results = semantic_search(query, item_type, tags, limit=limit, db_path=db_path)
        for r in semantic_results:
            key = f"{r['item_type']}-{r['item_id']}"
            if key not in seen:
                seen.add(key)
                results.append(r)

    # 2. Lexical Search
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

        where = ""
        if conditions:
            where = "AND " + " AND ".join(conditions)

        if query and query.strip():
            sql = f"""
                SELECT item_type, item_id, title, content as snippet, tags, rank
                FROM memory_fts
                WHERE memory_fts MATCH ? {where}
                ORDER BY rank
                LIMIT ?
            """
            fts_query = " OR ".join(f'"{term}"' for term in query.strip().split() if term)
            rows = conn.execute(sql, [fts_query] + params + [limit]).fetchall()
        else:
            filter_where = ""
            if conditions:
                filter_where = "WHERE " + " AND ".join(conditions)
            sql = f"""
                SELECT item_type, item_id, title, content as snippet, tags, 0 as rank
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
                results.append(
                    {
                        "item_type": row["item_type"],
                        "item_id": row["item_id"],
                        "title": row["title"],
                        "snippet": row["snippet"] or "",
                        "tags": row["tags"],
                        "rank": row["rank"],
                        "is_semantic": False,
                    }
                )

    # 3. Utility Boost (Apply usage_count)
    table_map = {
        "mistake": "mistakes",
        "pattern": "patterns",
        "skill": "skills",
        "conversation": "conversations",
        "prompt": "prompts",
    }
    with get_connection(db_path) as conn:
        for r in results:
            table = table_map.get(r["item_type"])
            usage_count = 0
            if table:
                u_row = conn.execute(
                    f"SELECT usage_count FROM {table} WHERE id = ?", (r["item_id"],)
                ).fetchone()
                if u_row:
                    usage_count = u_row[0]

            # Base score: semantic matches get 100, FTS matches get 50.
            # Boost: +15 points per successful usage.
            base_score = 100.0 if r.get("is_semantic") else 50.0
            r["utility_score"] = base_score + (usage_count * 15.0)

    # Re-sort by utility score descending
    results.sort(key=lambda x: x.get("utility_score", 0), reverse=True)
    return results[:limit]


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
    """Return counts of each item type and total tags."""
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

        # Total FTS entries
        fts_count = conn.execute("SELECT COUNT(*) as c FROM memory_fts").fetchone()["c"]
        stats["fts_indexed"] = fts_count

        return stats
