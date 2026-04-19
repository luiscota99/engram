"""
Search module — FTS5 full-text search with ranking and filtering.
"""

import json
import urllib.request
from .database import get_connection


from .database import get_connection
from .embeddings import embed_text

def semantic_search(query, limit=10, db_path=None):
    """
    Search vec_memory using KNN vector search.
    Requires sqlite-vec extension and a running Ollama instance.
    """
    embedding = embed_text(query)
    if not embedding:
        return []

    with get_connection(db_path) as conn:
        try:
            # Query vec_memory, join with FTS to get the item details
            # We assume rowid matches between memory_fts and vec_memory
            # Query vec_memory using a CTE to guarantee KNN index utilization before the JOIN
            sql = """
                WITH matches AS (
                    SELECT rowid, distance
                    FROM vec_memory
                    WHERE embedding MATCH ? AND k = ?
                )
                SELECT f.item_type, f.item_id, f.title, f.content as snippet, f.tags, m.distance
                FROM matches m
                JOIN memory_fts f ON m.rowid = f.rowid
                ORDER BY m.distance
            """
            rows = conn.execute(sql, [json.dumps(embedding), limit]).fetchall()
            results = []
            for row in rows:
                results.append({
                    "item_type": row["item_type"],
                    "item_id": row["item_id"],
                    "title": row["title"],
                    "snippet": row["snippet"][:200] if row["snippet"] else "",
                    "tags": row["tags"],
                    "rank": row["distance"], # lower distance is better
                    "is_semantic": True
                })
            return results
        except Exception as e:
            # sqlite-vec might not be loaded
            return []

def search(query, item_type=None, tags=None, limit=20, db_path=None):
    """
    Search across all memory using FTS5 ranked matching.

    Args:
        query: Free-text search query
        item_type: Optional filter ('mistake', 'pattern', 'skill', 'conversation', 'prompt')
        tags: Optional list of tags to filter by
        limit: Max results to return
        db_path: Optional database path override

    Returns:
        List of dicts with item_type, item_id, title, snippet, tags, rank
    """
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
            # FTS5 ranked search
            sql = f"""
                SELECT item_type, item_id, title,
                       snippet(memory_fts, 3, '>>>', '<<<', '...', 48) as snippet,
                       tags, rank
                FROM memory_fts
                WHERE memory_fts MATCH ? {where}
                ORDER BY rank
                LIMIT ?
            """
            # Clean query for FTS5: quote terms to avoid syntax errors
            fts_query = " OR ".join(
                f'"{term}"' for term in query.strip().split() if term
            )
            rows = conn.execute(sql, [fts_query] + params + [limit]).fetchall()
        else:
            # No query text — list with optional filters
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

        results = []
        for row in rows:
            results.append({
                "item_type": row["item_type"],
                "item_id": row["item_id"],
                "title": row["title"],
                "snippet": row["snippet"][:200] if row["snippet"] else "",
                "tags": row["tags"],
                "rank": row["rank"],
            })
        return results


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
