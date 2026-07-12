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
import logging
from datetime import datetime, timedelta

from .database import (
    get_connection,
    get_consolidation_fingerprint,
    get_embedding_stats,
    get_stored_consolidation_fingerprint,
    save_consolidation_fingerprint,
)
from .item_registry import gc_types, table_for

logger = logging.getLogger(__name__)

# ── Safety guardrails for bulk delete/archive operations ─────────────

GC_MAX_REMOVAL_FRACTION = 0.50
GC_MIN_COUNT_FOR_GUARD = 8

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
    types = item_types or list(gc_types())

    candidates = []
    with get_connection(db_path) as conn:
        for itype in types:
            table = table_for(itype)
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


def _count_live_items(item_types: list[str] | None = None, db_path=None) -> int:
    types = item_types or list(gc_types())
    total = 0
    with get_connection(db_path) as conn:
        for itype in types:
            table = table_for(itype)
            if table:
                total += conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()["c"]
    return total


def _gc_would_exceed_guard(candidates: list[dict], item_types: list[str] | None, db_path=None) -> str | None:
    if len(candidates) < GC_MIN_COUNT_FOR_GUARD:
        return None
    total = _count_live_items(item_types=item_types, db_path=db_path)
    if total <= 0:
        return None
    fraction = len(candidates) / total
    if fraction > GC_MAX_REMOVAL_FRACTION:
        pct = int(fraction * 100)
        return (
            f"GC blocked: would affect {len(candidates)}/{total} items ({pct}%) — "
            f"exceeds {int(GC_MAX_REMOVAL_FRACTION * 100)}% safety limit. "
            f"Use dry-run to review or narrow item_types/days_unused."
        )
    return None


def archive_item(conn, item_type: str, item_id: int, reason: str = "gc") -> bool:
    """Copy an item to archived_memories then delete it from the live table."""
    table = table_for(item_type)
    if not table or item_type not in gc_types():
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
        return {"mode": mode, "candidates": candidates, "processed": 0, "blocked": False}

    guard_reason = _gc_would_exceed_guard(candidates, item_types, db_path=db_path)
    if guard_reason:
        return {
            "mode": mode,
            "candidates": candidates,
            "processed": 0,
            "blocked": True,
            "reason": guard_reason,
        }

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
                    logger.warning(
                        "GC delete failed for %s:%s", c["item_type"], c["item_id"], exc_info=True
                    )

    return {"mode": mode, "candidates": candidates, "processed": processed, "blocked": False}


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
    *,
    force_rescan: bool = False,
) -> tuple[list[dict], str | None]:
    """Find transitive clusters of similar memories that could be consolidated.

    Uses vector similarity from vec_memory. Overlapping pairs are merged into
    larger clusters via Union-Find — e.g. if A~B and B~C then {A, B, C} is
    one cluster rather than two separate pairs.

    Returns ``(clusters, skip_reason)``. When fingerprint is unchanged since the
    last scan, returns ``([], "unchanged")`` unless ``force_rescan=True``.
    """
    if not _SQLITE_VEC:
        return [], "sqlite_vec_unavailable"

    types = item_types or ["mistake", "pattern", "skill"]
    fingerprint = get_consolidation_fingerprint(types, db_path=db_path)
    if not force_rescan:
        stored = get_stored_consolidation_fingerprint(db_path=db_path)
        if stored and stored == fingerprint:
            return [], "unchanged"

    import json as _json
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
                    logger.debug("Unparseable embedding for fts_rowid=%s", er["rowid"], exc_info=True)

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
    save_consolidation_fingerprint(fingerprint, db_path=db_path)
    return all_clusters, None


# ── LLM-Driven Consolidation Audit ───────────────────────────────────

_AUDIT_SYSTEM = """You are an engineering knowledge curator. Given near-duplicate memory entries,
decide for each cluster:
- keep_both: distinct facts — do not merge
- merge: same fact in different words — recommend merge (needs approval)
- auto_merge: obvious duplicate — safe to merge automatically

Be CONSERVATIVE: only use auto_merge when entries state the same fact with no unique details.

Return ONLY a JSON array:
[{"cluster_index": 0, "decision": "auto_merge", "reason": "...", "ids": [1, 2]}]
"""


def _cluster_snippets(cluster: dict, db_path=None) -> list[dict]:
    """Load id/title/snippet for each item in a consolidation cluster."""
    from .database import get_item

    item_type = cluster["item_type"]
    snippets = []
    for item in cluster["items"]:
        full = get_item(item_type, item["item_id"], db_path=db_path) or {}
        parts = []
        for key in ("name", "mistake", "symptoms", "trigger_desc", "workflow", "fix", "standard_fix"):
            val = full.get(key)
            if val:
                parts.append(str(val)[:200])
        snippet = " | ".join(parts)[:400] if parts else item.get("title", "")
        snippets.append({
            "id": item["item_id"],
            "title": item.get("title") or full.get("name") or full.get("mistake", ""),
            "snippet": snippet,
        })
    return snippets


def llm_audit_clusters(
    clusters: list[dict],
    *,
    db_path=None,
) -> list[dict]:
    """Use an LLM to score consolidation clusters. Returns audit decisions."""
    from .llm import call_chat_completion, is_llm_available, parse_json_from_llm, resolve_llm_model

    if not clusters:
        return []
    if not is_llm_available():
        return []

    payload = []
    for idx, cluster in enumerate(clusters):
        payload.append({
            "cluster_index": idx,
            "item_type": cluster["item_type"],
            "avg_similarity": cluster.get("avg_similarity"),
            "entries": _cluster_snippets(cluster, db_path=db_path),
        })

    user_msg = (
        "Review these near-duplicate clusters and return JSON decisions:\n\n"
        + json.dumps(payload, indent=2)
    )
    raw = call_chat_completion(
        [
            {"role": "system", "content": _AUDIT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        task="audit",
        max_tokens=1200,
    )
    parsed = parse_json_from_llm(raw or "")
    if not isinstance(parsed, list):
        return []

    decisions = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        decision = str(entry.get("decision", "keep_both")).lower()
        if decision not in ("keep_both", "merge", "auto_merge"):
            decision = "keep_both"
        idx = entry.get("cluster_index")
        cluster = clusters[idx] if isinstance(idx, int) and 0 <= idx < len(clusters) else None
        ids = entry.get("ids")
        if not ids and cluster:
            ids = [i["item_id"] for i in cluster["items"]]
        decisions.append({
            "cluster_index": idx,
            "item_type": cluster["item_type"] if cluster else None,
            "decision": decision,
            "reason": entry.get("reason", ""),
            "ids": ids or [],
            "model": resolve_llm_model(task="audit"),
        })
    return decisions


def _insert_merged_item(conn, item_type: str, merged: dict, tags: list[str]) -> int | None:
    """Insert a merged memory row and FTS index. Returns new item id."""
    from .database import index_in_fts, link_tags

    if item_type == "mistake":
        cursor = conn.execute(
            """INSERT INTO mistakes (date, context, mistake, root_cause, fix, prevention, conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                merged.get("date", datetime.now().strftime("%Y-%m-%d")),
                merged.get("context", ""),
                merged.get("mistake", merged.get("name", "Merged mistake")),
                merged.get("root_cause"),
                merged.get("fix", ""),
                merged.get("prevention"),
                merged.get("conversation_id"),
            ),
        )
        mid = cursor.lastrowid
        title = (merged.get("mistake") or "Merged mistake")[:80]
        content = f"{merged.get('context', '')} | {merged.get('mistake', '')} | {merged.get('fix', '')}"
        link_tags(conn, "mistake", mid, tags)
        index_in_fts(conn, "mistake", mid, title, content, tags)
        return mid

    if item_type == "pattern":
        cursor = conn.execute(
            """INSERT INTO patterns (name, symptoms, root_cause, standard_fix)
               VALUES (?, ?, ?, ?)""",
            (
                merged.get("name", "Merged pattern"),
                merged.get("symptoms", ""),
                merged.get("root_cause", ""),
                merged.get("standard_fix", merged.get("fix", "")),
            ),
        )
        pid = cursor.lastrowid
        name = merged.get("name", "Merged pattern")
        content = f"{merged.get('symptoms', '')} | {merged.get('root_cause', '')} | {merged.get('standard_fix', '')}"
        link_tags(conn, "pattern", pid, tags)
        index_in_fts(conn, "pattern", pid, name, content, tags)
        return pid

    if item_type == "skill":
        cursor = conn.execute(
            """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                merged.get("name", "Merged skill"),
                merged.get("domain", "engineering"),
                merged.get("trigger_desc", merged.get("trigger", "")),
                merged.get("workflow", ""),
                merged.get("pitfalls"),
                merged.get("key_files"),
                merged.get("dependencies"),
            ),
        )
        sid = cursor.lastrowid
        name = merged.get("name", "Merged skill")
        content = f"{merged.get('trigger_desc', '')} | {merged.get('workflow', '')}"
        link_tags(conn, "skill", sid, tags)
        index_in_fts(conn, "skill", sid, name, content, tags)
        return sid

    return None


def _apply_auto_merge(
    cluster: dict,
    *,
    db_path=None,
) -> dict:
    """Apply auto_merge for a 2-item cluster. Returns result metadata."""
    from .database import get_item
    from .merge import merge_entries

    item_type = cluster["item_type"]
    items = cluster["items"]
    if len(items) != 2:
        return {"applied": False, "reason": "auto_merge requires exactly 2 items"}

    id_a, id_b = items[0]["item_id"], items[1]["item_id"]
    entry_a = get_item(item_type, id_a, db_path=db_path)
    entry_b = get_item(item_type, id_b, db_path=db_path)
    if not entry_a or not entry_b:
        return {"applied": False, "reason": "one or both items not found"}

    merged = merge_entries(entry_a, entry_b)
    if not merged:
        return {"applied": False, "reason": "LLM merge failed"}

    tags = list({*(entry_a.get("tags") or []), *(entry_b.get("tags") or [])})
    with get_connection(db_path) as conn:
        new_id = _insert_merged_item(conn, item_type, merged, tags)
        if not new_id:
            return {"applied": False, "reason": f"unsupported item_type {item_type}"}
        archive_item(conn, item_type, id_a, reason="llm_auto_merge")
        archive_item(conn, item_type, id_b, reason="llm_auto_merge")

    return {
        "applied": True,
        "item_type": item_type,
        "merged_id": new_id,
        "archived_ids": [id_a, id_b],
    }


def run_llm_consolidation_audit(
    threshold: float = 0.80,
    *,
    dry_run: bool = True,
    db_path=None,
    force_rescan: bool = False,
) -> dict:
    """Run LLM consolidation audit on near-duplicate clusters."""
    from .llm import get_llm_status, is_llm_available

    clusters, skip_reason = find_consolidation_candidates(
        threshold=threshold,
        db_path=db_path,
        force_rescan=force_rescan,
    )
    report: dict = {
        "dry_run": dry_run,
        "llm_available": is_llm_available(),
        "skip_reason": skip_reason,
        "clusters_found": len(clusters),
        "decisions": [],
        "applied": [],
        "blocked": False,
    }
    report["llm_status"] = get_llm_status()

    if skip_reason == "unchanged":
        return report
    if not clusters:
        return report
    if not is_llm_available():
        report["fallback"] = "LLM unavailable — use engram suggest-consolidate for vector-only suggestions"
        report["clusters"] = clusters
        return report

    decisions = llm_audit_clusters(clusters, db_path=db_path)
    report["decisions"] = decisions

    if dry_run:
        report["clusters"] = clusters
        return report

    auto_merges = [d for d in decisions if d.get("decision") == "auto_merge"]
    if len(auto_merges) > len(clusters) * GC_MAX_REMOVAL_FRACTION and len(clusters) >= GC_MIN_COUNT_FOR_GUARD:
        report["blocked"] = True
        report["reason"] = (
            f"Audit blocked: would auto-merge {len(auto_merges)}/{len(clusters)} clusters — "
            f"exceeds {int(GC_MAX_REMOVAL_FRACTION * 100)}% safety limit."
        )
        return report

    for decision in auto_merges:
        idx = decision.get("cluster_index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(clusters):
            continue
        result = _apply_auto_merge(clusters[idx], db_path=db_path)
        report["applied"].append({**decision, **result})

    return report


# ── LLM-Assisted GC Scoring ──────────────────────────────────────────

_GC_SYSTEM = """You are evaluating whether old engineering memories should be kept or discarded.
For each item, return {"item_type": "...", "item_id": N, "decision": "keep"|"discard", "reason": "..."}

Be CONSERVATIVE: keep anything that might still be useful, reference-worthy, or unique.
Only discard entries that are clearly obsolete, trivial, or fully superseded.

Return ONLY a JSON array of decisions."""


def _enrich_gc_candidates(candidates: list[dict], db_path=None) -> list[dict]:
    """Add title/snippet from FTS for GC candidates."""
    enriched = []
    with get_connection(db_path) as conn:
        for c in candidates:
            row = conn.execute(
                """SELECT title, content FROM memory_fts
                   WHERE item_type = ? AND item_id = ? LIMIT 1""",
                (c["item_type"], c["item_id"]),
            ).fetchone()
            enriched.append({
                **c,
                "title": row["title"] if row else "",
                "snippet": (row["content"][:300] if row and row["content"] else ""),
            })
    return enriched


def llm_gc_score_candidates(
    candidates: list[dict],
    *,
    db_path=None,
) -> list[dict]:
    """Ask the LLM to score GC candidates as keep or discard."""
    from .llm import call_chat_completion, is_llm_available, parse_json_from_llm

    if not candidates or not is_llm_available():
        return []

    enriched = _enrich_gc_candidates(candidates, db_path=db_path)
    payload = [
        {
            "item_type": c["item_type"],
            "item_id": c["item_id"],
            "usage_count": c.get("usage_count", 0),
            "created_at": c.get("created_at"),
            "last_used_at": c.get("last_used_at"),
            "title": c.get("title", ""),
            "snippet": c.get("snippet", ""),
        }
        for c in enriched
    ]

    raw = call_chat_completion(
        [
            {"role": "system", "content": _GC_SYSTEM},
            {"role": "user", "content": "Score these GC candidates:\n\n" + json.dumps(payload, indent=2)},
        ],
        task="gc",
        max_tokens=1500,
    )
    parsed = parse_json_from_llm(raw or "")
    if not isinstance(parsed, list):
        return []

    scored = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        decision = str(entry.get("decision", "keep")).lower()
        if decision not in ("keep", "discard"):
            decision = "keep"
        scored.append({
            "item_type": entry.get("item_type"),
            "item_id": entry.get("item_id"),
            "decision": decision,
            "reason": entry.get("reason", ""),
        })
    return scored


def run_llm_gc(
    *,
    dry_run: bool = True,
    days_unused: int = 180,
    item_types: list[str] | None = None,
    db_path=None,
) -> dict:
    """Run GC with optional LLM scoring pass."""
    from .llm import get_llm_status, is_llm_available

    candidates = find_gc_candidates(
        days_unused=days_unused,
        item_types=item_types,
        db_path=db_path,
    )
    report: dict = {
        "dry_run": dry_run,
        "llm_available": is_llm_available(),
        "candidates": candidates,
        "scored": [],
        "to_discard": [],
        "processed": 0,
        "blocked": False,
        "mode": "dry-run" if dry_run else "archive",
    }
    report["llm_status"] = get_llm_status()

    if not candidates:
        return report

    if is_llm_available():
        scored = llm_gc_score_candidates(candidates, db_path=db_path)
        report["scored"] = scored
        discard_keys = {
            (s["item_type"], s["item_id"])
            for s in scored
            if s.get("decision") == "discard" and s.get("item_type") and s.get("item_id") is not None
        }
        to_discard = [c for c in candidates if (c["item_type"], c["item_id"]) in discard_keys]
        report["to_discard"] = to_discard
    else:
        to_discard = candidates
        report["fallback"] = "LLM unavailable — using time-based GC candidates only"

    if dry_run:
        return report

    guard_reason = _gc_would_exceed_guard(to_discard, item_types, db_path=db_path)
    if guard_reason:
        report["blocked"] = True
        report["reason"] = guard_reason
        return report

    processed = 0
    with get_connection(db_path) as conn:
        for c in to_discard:
            if archive_item(conn, c["item_type"], c["item_id"], reason="llm_gc"):
                processed += 1
    report["processed"] = processed
    return report


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


def get_reuse_rates(db_path=None) -> dict:
    """Per-type capture→reuse rates: of items 30+ days old, how many were ever used.

    Returns ``{item_type: {"eligible": n, "reused": n, "rate": float|None}}``.
    The capture-quality signal — low rate means that type of memory is being
    saved but never retrieved again.
    """
    from .item_registry import REGISTRY

    cutoff_30 = (datetime.now() - timedelta(days=30)).isoformat()
    rates: dict = {}
    with get_connection(db_path) as conn:
        for itype, spec in REGISTRY.items():
            if not spec.gc_eligible:
                continue
            eligible = conn.execute(
                f"SELECT COUNT(*) as c FROM {spec.table} WHERE created_at < ?", (cutoff_30,)
            ).fetchone()["c"]
            reused = conn.execute(
                f"SELECT COUNT(*) as c FROM {spec.table} WHERE created_at < ? AND usage_count > 0",
                (cutoff_30,),
            ).fetchone()["c"]
            rates[itype] = {
                "eligible": eligible,
                "reused": reused,
                "rate": round(reused / eligible, 3) if eligible else None,
            }
    return rates


def run_health_check(db_path=None) -> dict:
    """Generate a comprehensive health report for the memory database."""
    report: dict = {}

    with get_connection(db_path) as conn:
        # Item counts by type and age
        type_stats = {}
        from .item_registry import REGISTRY

        cutoff_30 = (datetime.now() - timedelta(days=30)).isoformat()
        cutoff_180 = (datetime.now() - timedelta(days=180)).isoformat()

        reuse_eligible_total = 0
        reuse_reused_total = 0
        for itype, spec in REGISTRY.items():
            if not spec.gc_eligible:
                continue
            table = spec.table
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
            # Capture→reuse: of items old enough to have had a chance (30+ days),
            # how many were ever retrieved-and-used again?
            reuse_eligible = conn.execute(
                f"SELECT COUNT(*) as c FROM {table} WHERE created_at < ?", (cutoff_30,)
            ).fetchone()["c"]
            reuse_reused = conn.execute(
                f"SELECT COUNT(*) as c FROM {table} WHERE created_at < ? AND usage_count > 0",
                (cutoff_30,),
            ).fetchone()["c"]
            reuse_eligible_total += reuse_eligible
            reuse_reused_total += reuse_reused
            type_stats[itype] = {
                "total": total,
                "added_last_30_days": recent,
                "unused_180_plus_days": unused_180,
                "most_used_id": most_used["id"] if most_used else None,
                "most_used_count": most_used["usage_count"] if most_used else 0,
                "reuse_rate_30d_plus": (
                    round(reuse_reused / reuse_eligible, 3) if reuse_eligible else None
                ),
            }
        report["items"] = type_stats
        report["capture_reuse"] = {
            "eligible_30d_plus": reuse_eligible_total,
            "reused": reuse_reused_total,
            "reuse_rate": (
                round(reuse_reused_total / reuse_eligible_total, 3)
                if reuse_eligible_total
                else None
            ),
        }

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
    try:
        from .reflex import get_promotion_candidates

        candidates = get_promotion_candidates(db_path=db_path)
        report["promotion_candidates"] = candidates
        for cand in candidates[:3]:
            recommendations.append(
                f"Skill #{cand['id']} '{cand['name']}' used {cand['usage_count']}x — proven. "
                f"Promote it to a reflex: engram promote {cand['id']}"
            )
    except Exception:
        report["promotion_candidates"] = []

    cr = report["capture_reuse"]
    if cr["eligible_30d_plus"] >= 10 and cr["reuse_rate"] is not None and cr["reuse_rate"] < 0.2:
        recommendations.append(
            f"Only {cr['reuse_rate']:.0%} of memories captured 30+ days ago were ever reused "
            f"({cr['reused']}/{cr['eligible_30d_plus']}). Capture fewer, higher-signal entries "
            f"(`engram suggest-capture`) and prune with `engram gc --archive`."
        )

    report["recommendations"] = recommendations
    return report


def run_sleep(
    *,
    threshold: float = 0.85,
    days_unused: int = 30,
    dry_run: bool = False,
    db_path=None,
) -> dict:
    """Sleep-time consolidation: scan duplicates, archive stale items, invalidate superseded."""
    from .temporal import invalidate_memory

    summary: dict = {
        "clusters_found": 0,
        "items_archived": 0,
        "items_invalidated": 0,
        "dry_run": dry_run,
    }

    clusters, _skip_reason = find_consolidation_candidates(threshold=threshold, db_path=db_path)
    summary["clusters_found"] = len(clusters)

    if not dry_run and clusters:
        for cluster in clusters[:5]:
            items = cluster.get("items", [])
            if len(items) < 2:
                continue
            keeper = items[0]
            for item in items[1:]:
                invalidate_memory(
                    item["item_type"],
                    int(item["item_id"]),
                    superseded_by=int(keeper["item_id"]),
                    reason="sleep-time consolidation",
                    db_path=db_path,
                )
                summary["items_invalidated"] += 1

    gc_candidates = find_gc_candidates(days_unused=days_unused, db_path=db_path)
    if not dry_run and gc_candidates:
        with get_connection(db_path) as conn:
            for cand in gc_candidates[:50]:
                if archive_item(conn, cand["item_type"], cand["item_id"], reason="sleep-gc"):
                    summary["items_archived"] += 1
    else:
        summary["gc_candidates"] = len(gc_candidates)

    return summary


# ── Efficiency report (Action Ladder) ────────────────────────────────


def get_efficiency_report(db_path=None) -> dict:
    """Measured Action-Ladder statistics: how much work runs below the
    reasoning rung, and a conservative floor on tokens avoided.

    Honesty rule: only report what is measurable. The per-run savings floor is
    the token length of the workflow text an agent would otherwise have had to
    read (chars/4) minus the ~50-token reflex call — reasoning tokens saved on
    top of that are real but unmeasurable here, so they are not claimed.
    """
    report: dict = {}
    with get_connection(db_path) as conn:
        try:
            rows = conn.execute(
                """SELECT r.name, r.run_count, r.last_status, r.approved_at,
                          s.workflow, s.trigger_desc
                   FROM reflexes r JOIN skills s ON s.id = r.skill_id"""
            ).fetchall()
        except Exception:
            rows = []

        approved = [dict(r) for r in rows if r["approved_at"]]
        total_runs = sum(r["run_count"] or 0 for r in rows)
        floor_saved = 0
        for r in approved:
            per_run = max(
                0,
                (len(r["workflow"] or "") + len(r["trigger_desc"] or "")) // 4 - 50,
            )
            floor_saved += per_run * (r["run_count"] or 0)

        demotions = conn.execute(
            "SELECT COUNT(*) as c FROM mistakes WHERE mistake LIKE 'Auto-demoted%'"
        ).fetchone()["c"]

    try:
        from .reflex import get_reflex_success_rates

        report["reflex_success"] = get_reflex_success_rates(db_path=db_path)
    except Exception:
        report["reflex_success"] = {}

    report["reflexes_approved"] = len(approved)
    report["reflexes_total"] = len(rows)
    report["reflex_runs"] = total_runs
    report["auto_demotions"] = demotions
    report["tokens_avoided_floor"] = floor_saved
    report["reuse"] = get_reuse_rates(db_path=db_path)
    report["promotion_candidates"] = []
    try:
        from .reflex import get_promotion_candidates

        report["promotion_candidates"] = get_promotion_candidates(db_path=db_path)
    except Exception:
        logger.debug("promotion candidates unavailable", exc_info=True)
    return report


# ── Self-check: Engram monitoring Engram ─────────────────────────────


def run_self_check(db_path=None) -> dict:
    """Daily self-maintenance sweep: files inbox items for findings.

    Idempotent by finding_key — an open item is never re-filed. The system
    proposes its own upkeep as decisions; the human decides, as always.
    """
    from .inbox import file_item
    from .reflex import get_promotion_candidates, get_reflex_success_rates, list_reflexes

    filed: list[str] = []

    def _file(**kw) -> None:
        if file_item(db_path=db_path, source="self_check", **kw) is not None:
            filed.append(kw["finding_key"])

    # 1. Skills that earned reflex-hood
    for cand in get_promotion_candidates(db_path=db_path):
        _file(
            kind="decision",
            severity="info",
            title=f"Promote skill #{cand['id']} '{cand['name']}'? (used {cand['usage_count']}x)",
            body=f"Run: engram promote {cand['id']}  — then review and approve the drafted script.",
            finding_key=f"promote:skill:{cand['id']}",
        )

    # 2. Underperforming reflexes (enough runs to judge)
    rates = get_reflex_success_rates(db_path=db_path)
    names = {r["id"]: r["name"] for r in list_reflexes(db_path=db_path)}
    for rid, st in rates.items():
        if st["runs"] >= 5 and st["rate"] is not None and st["rate"] < 0.7:
            _file(
                kind="decision",
                severity="warning",
                title=f"Reflex '{names.get(rid, rid)}' succeeding only {st['rate']:.0%} ({st['ok']}/{st['runs']}) — fix or demote?",
                body="Inspect: engram reflex list; the script may need repair and re-approval.",
                finding_key=f"reflex-flaky:{rid}",
            )

    # 3. Hygiene: unfilled auto-demotion mistakes, pending embeddings, drift
    with get_connection(db_path) as conn:
        placeholders = conn.execute(
            "SELECT COUNT(*) AS c FROM mistakes WHERE fix LIKE '%(fill in%'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM embedding_status WHERE status IN ('pending','stale')"
        ).fetchone()["c"]
    if placeholders:
        _file(
            kind="alert",
            severity="warning",
            title=f"{placeholders} mistake(s) still carry '(fill in …)' placeholders",
            body="Complete their root_cause/fix so future recall is trustworthy.",
            finding_key="hygiene:placeholders",
        )
    if pending > 5:
        _file(
            kind="alert",
            severity="warning",
            title=f"{pending} memories lack embeddings (semantic recall degraded)",
            body="Run: engram reembed",
            finding_key="hygiene:pending-embeddings",
        )

    # 4. Consolidation clusters (vector-based; skipped when unchanged)
    clusters, _reason = find_consolidation_candidates(db_path=db_path)
    for cl in clusters[:3]:
        ids = ",".join(str(i["item_id"]) for i in cl.get("items", []))
        _file(
            kind="decision",
            severity="info",
            title=f"Consolidate {cl['cluster_size']} near-duplicate {cl['item_type']}s? (ids {ids})",
            body=f"avg similarity {cl.get('avg_similarity')}; engram suggest-consolidate for detail.",
            finding_key=f"consolidate:{cl['item_type']}:{ids}",
        )

    # 4b. Proven-but-unvalidated: reused skills with no test that PROVES they help
    try:
        with get_connection(db_path) as conn:
            unval = conn.execute(
                """SELECT s.id, s.name FROM skills s
                   LEFT JOIN skill_tests t ON t.item_type='skill' AND t.item_id=s.id
                   WHERE s.usage_count >= 5 AND t.id IS NULL AND s.superseded_by IS NULL
                   ORDER BY s.usage_count DESC LIMIT 3""",
            ).fetchall()
        for row in unval:
            _file(
                kind="decision",
                severity="info",
                title=f"Skill #{row['id']} '{row['name']}' is reused but never validated — add a proof test?",
                body="Reuse proves it's retrieved, not that it works. engram validate add skill "
                     f"{row['id']} --scenario ... --assert ...",
                finding_key=f"unvalidated:skill:{row['id']}",
            )
    except Exception:
        logger.debug("unvalidated-skill scan failed", exc_info=True)

    # 5. Capture quality collapse
    cr = get_reuse_rates(db_path=db_path)
    for itype, st in cr.items():
        if st["eligible"] >= 10 and st["rate"] is not None and st["rate"] < 0.2:
            _file(
                kind="alert",
                severity="warning",
                title=f"Reuse rate for {itype}s is {st['rate']:.0%} — capture quality warning",
                body="Capture fewer, higher-signal entries; prune with engram gc.",
                finding_key=f"reuse-low:{itype}",
            )

    return {"filed": filed, "count": len(filed)}
