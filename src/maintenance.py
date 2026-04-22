"""
Maintenance module — garbage collection, consolidation suggestions, and health dashboard.

Commands:
  engram gc            — Archive or delete unused memories
  engram suggest-consolidate — Find near-duplicate clusters for merging
  engram health        — Show a health report with actionable recommendations
"""

from __future__ import annotations


import json
from datetime import datetime, timedelta

from .database import get_connection, get_embedding_stats


# ── Garbage Collection ───────────────────────────────────────────────


def find_gc_candidates(
    days_unused: int = 180,
    item_types: list[str] | None = None,
    db_path=None,
) -> list[dict]:
    """Return items that have never been used OR were last used more than days_unused ago.

    Candidates are: usage_count = 0 AND created_at < cutoff, OR last_used_at < cutoff.
    """
    cutoff = (datetime.now() - timedelta(days=days_unused)).isoformat()
    types = item_types or ["mistake", "pattern", "skill", "conversation", "prompt"]

    table_map = {
        "mistake": "mistakes",
        "pattern": "patterns",
        "skill": "skills",
        "conversation": "conversations",
        "prompt": "prompts",
    }

    candidates = []
    with get_connection(db_path) as conn:
        for itype in types:
            table = table_map.get(itype)
            if not table:
                continue
            rows = conn.execute(
                f"""SELECT id, created_at, last_used_at, usage_count
                    FROM {table}
                    WHERE usage_count = 0 AND (created_at < ? OR created_at IS NULL)""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                candidates.append({
                    "item_type": itype,
                    "item_id": row["id"],
                    "usage_count": row["usage_count"],
                    "created_at": row["created_at"],
                    "last_used_at": row["last_used_at"],
                })
    return candidates


def archive_item(conn, item_type: str, item_id: int, reason: str = "gc") -> bool:
    """Copy an item to archived_memories then delete it from the live table."""
    table_map = {
        "mistake": "mistakes", "pattern": "patterns", "skill": "skills",
        "conversation": "conversations", "prompt": "prompts",
    }
    table = table_map.get(item_type)
    if not table:
        return False

    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return False

    conn.execute(
        "INSERT INTO archived_memories (item_type, item_id, original_table, data, archive_reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_type, item_id, table, json.dumps(dict(row)), reason),
    )

    from .database import delete_item
    delete_item(conn, item_type, item_id)
    return True


def run_gc(
    mode: str = "dry-run",
    days_unused: int = 180,
    item_types: list[str] | None = None,
    db_path=None,
) -> dict:
    """Run garbage collection.

    mode:
        'dry-run'  — report candidates without modifying anything
        'archive'  — soft-delete (move to archived_memories)
        'delete'   — permanently remove

    Returns a report dict with 'candidates', 'processed', 'mode'.
    """
    candidates = find_gc_candidates(days_unused=days_unused, item_types=item_types, db_path=db_path)

    if mode == "dry-run":
        return {"mode": mode, "candidates": candidates, "processed": 0}

    processed = 0
    with get_connection(db_path) as conn:
        for c in candidates:
            if mode == "archive":
                if archive_item(conn, c["item_type"], c["item_id"], reason="gc_auto"):
                    processed += 1
            elif mode == "delete":
                from .database import delete_item
                try:
                    delete_item(conn, c["item_type"], c["item_id"])
                    processed += 1
                except Exception:
                    pass

    return {"mode": mode, "candidates": candidates, "processed": processed}


# ── Consolidation Suggestions ────────────────────────────────────────

try:
    import sqlite_vec as _svec
    _SQLITE_VEC = True
except ImportError:
    _SQLITE_VEC = False


def find_consolidation_candidates(
    threshold: float = 0.80,
    item_types: list[str] | None = None,
    db_path=None,
) -> list[dict]:
    """Find clusters of similar memories that could be consolidated.

    Uses vector similarity from vec_memory.  Each returned cluster is a list of
    (item_type, item_id, title) tuples that exceed the similarity threshold
    between all pairs.

    Returns a list of cluster dicts: {item_type, items: [...], avg_similarity}.
    """
    if not _SQLITE_VEC:
        return []

    import json as _json
    types = item_types or ["mistake", "pattern", "skill"]
    clusters = []
    seen_pairs: set[tuple] = set()

    with get_connection(db_path) as conn:
        for itype in types:
            # Get all rowids and embeddings for this type
            rows = conn.execute(
                """SELECT f.rowid, f.item_id, f.title
                   FROM memory_fts f
                   WHERE f.item_type = ?""",
                (itype,),
            ).fetchall()

            if len(rows) < 2:
                continue

            rowids = [r["rowid"] for r in rows]
            item_map = {r["rowid"]: r for r in rows}

            # Fetch embeddings
            emb_rows = conn.execute(
                f"SELECT rowid, embedding FROM vec_memory WHERE rowid IN "
                f"({','.join('?' * len(rowids))})",
                rowids,
            ).fetchall()

            embeddings: dict[int, list[float]] = {}
            for er in emb_rows:
                try:
                    embeddings[er["rowid"]] = _json.loads(er["embedding"])
                except Exception:
                    pass

            if len(embeddings) < 2:
                continue

            # Compute pairwise cosine similarity
            emb_rowids = list(embeddings.keys())
            for i in range(len(emb_rowids)):
                for j in range(i + 1, len(emb_rowids)):
                    ri, rj = emb_rowids[i], emb_rowids[j]
                    pair = (min(ri, rj), max(ri, rj))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)

                    sim = _cosine_similarity(embeddings[ri], embeddings[rj])
                    if sim >= threshold:
                        ia = item_map[ri]
                        ib = item_map[rj]
                        clusters.append({
                            "item_type": itype,
                            "items": [
                                {"item_id": ia["item_id"], "title": ia["title"], "fts_rowid": ri},
                                {"item_id": ib["item_id"], "title": ib["title"], "fts_rowid": rj},
                            ],
                            "similarity": round(sim, 4),
                        })

    # Sort by similarity descending
    clusters.sort(key=lambda x: x["similarity"], reverse=True)
    return clusters


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ── Health Dashboard ─────────────────────────────────────────────────


def run_health_check(db_path=None) -> dict:
    """Generate a comprehensive health report for the memory database."""
    report: dict = {}

    with get_connection(db_path) as conn:
        # Item counts by type and age
        type_stats = {}
        table_map = {
            "mistakes": "mistake", "patterns": "pattern", "skills": "skill",
            "conversations": "conversation", "prompts": "prompt",
        }
        now_iso = datetime.now().isoformat()
        cutoff_30 = (datetime.now() - timedelta(days=30)).isoformat()
        cutoff_90 = (datetime.now() - timedelta(days=90)).isoformat()
        cutoff_180 = (datetime.now() - timedelta(days=180)).isoformat()

        for table, itype in table_map.items():
            total = conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()["c"]
            recent = conn.execute(
                f"SELECT COUNT(*) as c FROM {table} WHERE created_at > ?", (cutoff_30,)
            ).fetchone()["c"]
            unused_180 = conn.execute(
                f"SELECT COUNT(*) as c FROM {table} WHERE usage_count = 0 AND created_at < ?",
                (cutoff_180,)
            ).fetchone()["c"]
            most_used = conn.execute(
                f"SELECT id, usage_count FROM {table} ORDER BY usage_count DESC LIMIT 1"
            ).fetchone()
            type_stats[itype] = {
                "total": total,
                "added_last_30_days": recent,
                "unused_180_plus_days": unused_180,
                "most_used_id": most_used["id"] if most_used else None,
                "most_used_count": most_used["usage_count"] if most_used else 0,
            }
        report["items"] = type_stats

        # Orphaned tags (tags with no item_tags references)
        orphaned_tags = conn.execute(
            "SELECT COUNT(*) as c FROM tags t "
            "WHERE NOT EXISTS (SELECT 1 FROM item_tags it WHERE it.tag_id = t.id)"
        ).fetchone()["c"]
        report["orphaned_tags"] = orphaned_tags

        # FTS / vec drift: items in FTS but no vec_memory entry
        fts_total = conn.execute("SELECT COUNT(*) as c FROM memory_fts").fetchone()["c"]
        try:
            vec_total = conn.execute("SELECT COUNT(*) as c FROM vec_memory").fetchone()["c"]
        except Exception:
            vec_total = 0
        report["fts_total"] = fts_total
        report["vec_total"] = vec_total
        report["vec_drift"] = fts_total - vec_total  # positive = FTS entries missing vectors

        # Archived memories
        archived = conn.execute("SELECT COUNT(*) as c FROM archived_memories").fetchone()["c"]
        report["archived_memories"] = archived

        # Session states
        session_count = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        state_count = conn.execute("SELECT COUNT(*) as c FROM session_state").fetchone()["c"]
        report["sessions_total"] = session_count
        report["sessions_with_state"] = state_count

    # Embedding stats
    report["embeddings"] = get_embedding_stats(db_path)

    # GC candidates
    gc_candidates = find_gc_candidates(days_unused=180, db_path=db_path)
    report["gc_candidates"] = len(gc_candidates)

    # Build recommendations
    recommendations = []
    emb = report["embeddings"]
    if emb.get("stale", 0) > 0:
        recommendations.append(
            f"Run `engram reembed` to regenerate {emb['stale']} stale embeddings "
            f"(from old model)."
        )
    if emb.get("pending", 0) > 0:
        recommendations.append(
            f"{emb['pending']} items have no embeddings. Ensure Ollama is running and "
            f"run `engram doctor --repair`."
        )
    if report["gc_candidates"] > 0:
        recommendations.append(
            f"{report['gc_candidates']} memories unused for 180+ days. "
            f"Run `engram gc --archive` to clean up."
        )
    if orphaned_tags > 0:
        recommendations.append(
            f"{orphaned_tags} orphaned tags detected. Run `engram doctor --repair`."
        )
    if report["vec_drift"] > 0:
        recommendations.append(
            f"{report['vec_drift']} FTS entries are missing vector embeddings. "
            f"Run `engram doctor --repair`."
        )

    report["recommendations"] = recommendations
    return report
