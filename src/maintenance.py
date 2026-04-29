"""
Maintenance module — garbage collection, consolidation suggestions, and health dashboard.

Commands:
  engram gc            — Archive or delete unused memories
  engram suggest-consolidate — Find near-duplicate clusters for merging
  engram merge-projects — Point all knowledge from one project row at another (e.g. after rename)
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

    Candidates are:
      - Never used: usage_count = 0 AND (created_at < cutoff OR created_at IS NULL)
      - Stale: last_used_at IS NOT NULL AND last_used_at < cutoff
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
                    WHERE
                        (usage_count = 0 AND (created_at < ? OR created_at IS NULL))
                        OR (last_used_at IS NOT NULL AND last_used_at < ?)""",
                (cutoff, cutoff),
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
    import sqlite_vec  # noqa: F401
    _SQLITE_VEC = True
except ImportError:
    _SQLITE_VEC = False


class _UnionFind:
    """Simple Union-Find (disjoint set) for transitive cluster merging."""

    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        px, py = self.find(x), self.find(y)
        if px != py:
            self.parent[px] = py


def find_consolidation_candidates(
    threshold: float = 0.80,
    item_types: list[str] | None = None,
    db_path=None,
) -> list[dict]:
    """Find transitive clusters of similar memories that could be consolidated.

    Uses vector similarity from vec_memory. Overlapping pairs are merged into
    larger clusters via Union-Find — e.g. if A~B and B~C then {A, B, C} is
    one cluster rather than two separate pairs.

    Returns a list of cluster dicts:
        {item_type, items: [...], avg_similarity, cluster_size}
    sorted by cluster_size (desc) then avg_similarity (desc).
    """
    if not _SQLITE_VEC:
        return []

    import json as _json
    types = item_types or ["mistake", "pattern", "skill"]
    all_clusters = []

    with get_connection(db_path) as conn:
        for itype in types:
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

            # Phase 1: compute pairwise similarities and union matching pairs
            uf = _UnionFind()
            edge_sims: dict[tuple, float] = {}
            emb_rowids = list(embeddings.keys())

            for i in range(len(emb_rowids)):
                for j in range(i + 1, len(emb_rowids)):
                    ri, rj = emb_rowids[i], emb_rowids[j]
                    sim = _cosine_similarity(embeddings[ri], embeddings[rj])
                    if sim >= threshold:
                        uf.union(ri, rj)
                        edge_sims[(min(ri, rj), max(ri, rj))] = sim

            # Phase 2: group rowids by their Union-Find root
            root_to_members: dict = {}
            for rid in emb_rowids:
                root = uf.find(rid)
                root_to_members.setdefault(root, []).append(rid)

            # Phase 3: build cluster dicts for groups with 2+ members
            for members in root_to_members.values():
                if len(members) < 2:
                    continue

                # Collect all pairwise similarities within this cluster
                pair_sims = []
                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        ri, rj = members[i], members[j]
                        key = (min(ri, rj), max(ri, rj))
                        if key in edge_sims:
                            pair_sims.append(edge_sims[key])
                        else:
                            # Members may be transitively connected; compute if missing
                            sim = _cosine_similarity(embeddings[ri], embeddings[rj])
                            pair_sims.append(sim)

                avg_sim = sum(pair_sims) / len(pair_sims) if pair_sims else 0.0
                items = [
                    {
                        "item_id": item_map[rid]["item_id"],
                        "title": item_map[rid]["title"],
                        "fts_rowid": rid,
                    }
                    for rid in members
                ]
                all_clusters.append({
                    "item_type": itype,
                    "items": items,
                    "avg_similarity": round(avg_sim, 4),
                    "cluster_size": len(members),
                })

    all_clusters.sort(key=lambda x: (-x["cluster_size"], -x["avg_similarity"]))
    return all_clusters


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


# ── Merge project rows ──────────────────────────────────────────────


def merge_projects(
    from_project_id: int,
    to_project_id: int,
    *,
    dry_run: bool = True,
    db_path=None,
) -> dict:
    """Reassign codebase knowledge, file graph, and item links from one project to another.

    Use when the same repo was indexed under two paths or names (e.g. rename ``tcg-pos`` → ``lzp-pos``).
    Rows that would violate a uniqueness constraint on the target project are dropped from the source.

    Does **not** rewrite paths inside memory item bodies or FTS rows — only ``projects``-scoped tables.
    """
    if from_project_id == to_project_id:
        raise ValueError("from_project_id and to_project_id must differ")

    summary: dict = {
        "dry_run": dry_run,
        "from_project_id": from_project_id,
        "to_project_id": to_project_id,
        "from_name": None,
        "from_path": None,
        "to_name": None,
        "to_path": None,
        "codebase_overlap_removed": 0,
        "codebase_reassigned": 0,
        "relationships_overlap_removed": 0,
        "relationships_reassigned": 0,
        "item_projects_overlap_removed": 0,
        "item_projects_reassigned": 0,
        "source_project_deleted": False,
    }

    with get_connection(db_path) as conn:
        fr = conn.execute(
            "SELECT id, name, path FROM projects WHERE id = ?", (from_project_id,)
        ).fetchone()
        to = conn.execute(
            "SELECT id, name, path FROM projects WHERE id = ?", (to_project_id,)
        ).fetchone()
        if not fr or not to:
            raise ValueError("Both project IDs must exist in the database")

        summary["from_name"] = fr["name"]
        summary["from_path"] = fr["path"]
        summary["to_name"] = to["name"]
        summary["to_path"] = to["path"]

        ck_overlap = conn.execute(
            """SELECT COUNT(*) AS c FROM codebase_knowledge a
               WHERE a.project_id = ? AND EXISTS (
                 SELECT 1 FROM codebase_knowledge b
                 WHERE b.project_id = ? AND b.file_path = a.file_path
               )""",
            (from_project_id, to_project_id),
        ).fetchone()["c"]
        ck_src = conn.execute(
            "SELECT COUNT(*) AS c FROM codebase_knowledge WHERE project_id = ?",
            (from_project_id,),
        ).fetchone()["c"]

        rel_overlap = conn.execute(
            """SELECT COUNT(*) AS c FROM file_relationships a
               WHERE a.project_id = ? AND EXISTS (
                 SELECT 1 FROM file_relationships b
                 WHERE b.project_id = ?
                   AND b.source_file = a.source_file
                   AND b.target_file = a.target_file
                   AND b.relationship_type = a.relationship_type
               )""",
            (from_project_id, to_project_id),
        ).fetchone()["c"]
        rel_src = conn.execute(
            "SELECT COUNT(*) AS c FROM file_relationships WHERE project_id = ?",
            (from_project_id,),
        ).fetchone()["c"]

        ip_overlap = conn.execute(
            """SELECT COUNT(*) AS c FROM item_projects a
               WHERE a.project_id = ? AND EXISTS (
                 SELECT 1 FROM item_projects b
                 WHERE b.project_id = ?
                   AND b.item_type = a.item_type AND b.item_id = a.item_id
               )""",
            (from_project_id, to_project_id),
        ).fetchone()["c"]
        ip_src = conn.execute(
            "SELECT COUNT(*) AS c FROM item_projects WHERE project_id = ?",
            (from_project_id,),
        ).fetchone()["c"]

        summary["codebase_overlap_removed"] = ck_overlap
        summary["codebase_reassigned"] = ck_src - ck_overlap
        summary["relationships_overlap_removed"] = rel_overlap
        summary["relationships_reassigned"] = rel_src - rel_overlap
        summary["item_projects_overlap_removed"] = ip_overlap
        summary["item_projects_reassigned"] = ip_src - ip_overlap

        if dry_run:
            return summary

        conn.execute(
            """DELETE FROM codebase_knowledge WHERE project_id = ? AND EXISTS (
                 SELECT 1 FROM codebase_knowledge b
                 WHERE b.project_id = ? AND b.file_path = codebase_knowledge.file_path
               )""",
            (from_project_id, to_project_id),
        )
        conn.execute(
            "UPDATE codebase_knowledge SET project_id = ? WHERE project_id = ?",
            (to_project_id, from_project_id),
        )
        conn.execute(
            """DELETE FROM file_relationships WHERE project_id = ? AND EXISTS (
                 SELECT 1 FROM file_relationships b
                 WHERE b.project_id = ?
                   AND b.source_file = file_relationships.source_file
                   AND b.target_file = file_relationships.target_file
                   AND b.relationship_type = file_relationships.relationship_type
               )""",
            (from_project_id, to_project_id),
        )
        conn.execute(
            "UPDATE file_relationships SET project_id = ? WHERE project_id = ?",
            (to_project_id, from_project_id),
        )
        conn.execute(
            """DELETE FROM item_projects WHERE project_id = ? AND EXISTS (
                 SELECT 1 FROM item_projects b
                 WHERE b.project_id = ?
                   AND b.item_type = item_projects.item_type
                   AND b.item_id = item_projects.item_id
               )""",
            (from_project_id, to_project_id),
        )
        conn.execute(
            "UPDATE item_projects SET project_id = ? WHERE project_id = ?",
            (to_project_id, from_project_id),
        )
        conn.execute("DELETE FROM projects WHERE id = ?", (from_project_id,))
        summary["source_project_deleted"] = True

    return summary


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
        cutoff_30 = (datetime.now() - timedelta(days=30)).isoformat()
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
