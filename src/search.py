"""
Search module — FTS5 full-text search + semantic vector search with
multi-factor ranking via src/ranking.py.
"""

from __future__ import annotations

import json
import logging
import re

from .database import (
    connection_scope,
    get_connection,
    get_or_create_project,
    get_pinned_items,
    get_project_affinities,
)
from .embeddings import embed_text, get_embedding_degradation_reason, is_embedding_host_available
from .item_registry import table_for, usage_ranked_types
from .query_analyzer import detect_query_tags, detect_temporal_intent
from .ranking import (
    optional_cross_encoder_rerank,
    rank_results,
    reciprocal_rank_scores,
    rerank_with_bm25,
    result_key,
)
from .search_audit import append_search_audit

logger = logging.getLogger(__name__)


class SearchResults(list):
    """A list subclass that carries search health metadata."""

    semantic_status: str = "ok"
    semantic_available: bool = True


def _fts_query_terms(query: str) -> list[str]:
    """Tokenize query for FTS5 ``MATCH``: same alphanumeric words as BM25/Ranking (# ``_tokenize``).

    Strips punctuation (e.g. ``migration.`` → ``migration``). If normalization
    yields nothing but the trimmed query looks textual, falls back to
    whitespace split (handles odd Unicode / punctuation-only edge cases).
    """
    q = query.strip()
    if not q:
        return []
    terms = re.findall(r"[a-z0-9]+", q.lower())
    if terms:
        return terms
    if any(ch.isalpha() for ch in q):
        return [t for t in q.split() if t]
    return [t for t in q.split() if t]


def _fts5_tag_phrase(tag: str) -> str:
    """Format *tag* for use in ``... MATCH ?`` on an FTS5 column.

    Hyphenated tags like ``ai-assistant`` or ``n-plus-one`` must be passed as
    a **phrase**; otherwise ``-`` is parsed as the NOT operator and SQLite
    reports errors such as ``no such column: assistant``."""
    t = tag.strip()
    if not t:
        return t
    escaped = t.replace('"', '""')
    return f'"{escaped}"'


def _get_stale_rowids(conn) -> set:
    """Return the set of fts_rowids whose embeddings are stale or failed."""
    try:
        rows = conn.execute(
            "SELECT fts_rowid FROM embedding_status WHERE status IN ('stale', 'failed')"
        ).fetchall()
        return {r["fts_rowid"] for r in rows}
    except Exception as e:
        logger.debug("Stale rowids unavailable (embedding_status): %s", e)
        return set()


def semantic_search(
    query, item_type=None, tags=None, limit=10, db_path=None, conn=None, embed_timeout=None, timing_sink=None
):
    """Search vec_memory using KNN vector search.

    Requires sqlite_vec extension and a running Ollama instance.
    Returns (results, status) where status is one of:
        "ok"          — semantic search ran and returned results (or a valid empty set)
        "unavailable" — Ollama/embedding failed; caller should fall back to lexical-only
        "degraded"    — vec extension unavailable or query error

    ``embed_timeout`` caps the query-embedding call. Interactive callers (the
    recall/guard hooks, which run on every prompt) pass a small value so a cold
    or slow Ollama can't block the turn — on timeout the query embed returns
    None and the caller falls back to lexical-only, instead of stalling seconds.

    ``timing_sink`` (optional dict) receives ``embed_ms`` and ``vec_search_ms``
    so the caller can record where the semantic path spends its time. This is
    how the ROI ledger separates embedding latency (the measured bottleneck)
    from the vector KNN scan (near-free at current scale) — so a real vector-DB
    limit would show up in data instead of being guessed at.
    """
    import time

    t0 = time.monotonic()
    embedding = embed_text(query, timeout=embed_timeout)
    if timing_sink is not None:
        timing_sink["embed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    if not embedding:
        if not is_embedding_host_available():
            return [], "unavailable"
        reason = get_embedding_degradation_reason()
        if reason:
            logger.debug("semantic_search unavailable: %s", reason)
        return [], "unavailable"

    with connection_scope(conn, db_path) as conn:
        try:
            conditions = []
            params = []
            if item_type:
                conditions.append("f.item_type = ?")
                params.append(item_type)
            if tags:
                for tag in tags:
                    conditions.append("f.tags MATCH ?")
                    params.append(_fts5_tag_phrase(tag))

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
            t_knn = time.monotonic()
            rows = conn.execute(sql, [json.dumps(embedding), limit * 2] + params).fetchall()
            if timing_sink is not None:
                timing_sink["vec_search_ms"] = round((time.monotonic() - t_knn) * 1000, 1)
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
            logger.exception("semantic_search vec_memory query failed")
            return [], "degraded"


def search(
    query,
    item_type=None,
    tags=None,
    limit=10,
    db_path=None,
    project_path=None,
    *,
    skip_audit=False,
    audit_source="search",
    include_superseded=False,
    rank_inputs_sink: dict | None = None,
    embed_timeout=None,
    as_of: str | None = None,
    explain: bool = False,
    token_budget: int | None = None,
):
    """Hybrid Search: FTS5 lexical + KNN semantic, ranked by multi-factor utility score.

    Returns a list of result dicts. Each result has a ``utility_score`` field after ranking.
    The list itself carries a ``semantic_status`` attribute (``"ok"``, ``"unavailable"``, or
    ``"degraded"``) so callers can surface degradation warnings without checking embeddings
    separately.

    ``as_of`` (ISO date): drop items invalidated on or before that date via ``memory_facts``.
    ``explain``: attach ``score_breakdown`` dicts to each result.
    ``token_budget``: soft cap on total injected chars (~4 chars/token) across snippets.
    """
    results: list[dict] = []
    semantic_status = "ok"
    semantic_available = is_embedding_host_available()

    filter_tags = list(tags) if tags else []
    sem_results: list[dict] = []
    lex_results: list[dict] = []
    graph_results: list[dict] = []
    timing: dict = {}

    with get_connection(db_path) as conn:
        detected_tags: list[str] = []
        if query and query.strip():
            detected_tags = detect_query_tags(query, conn=conn)

        if query and query.strip():
            sem_results, semantic_status = semantic_search(
                query, item_type, filter_tags, limit=limit, conn=conn,
                embed_timeout=embed_timeout, timing_sink=timing,
            )
            semantic_available = semantic_status == "ok"

        conditions = []
        params = []
        if item_type:
            conditions.append("item_type = ?")
            params.append(item_type)
        if filter_tags:
            for tag in filter_tags:
                conditions.append("tags MATCH ?")
                params.append(_fts5_tag_phrase(tag))

        where_extra = ("AND " + " AND ".join(conditions)) if conditions else ""

        if query and query.strip():
            qterms = _fts_query_terms(query)
            if not qterms:
                qterms = [query.strip()]
            fts_query = " OR ".join(f'"{term}"' for term in qterms if term)
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
            lex_results.append({
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

        seed_for_graph = (sem_results[:5] + lex_results[:5])
        seen_graph: set[str] = set()
        try:
            for seed in seed_for_graph:
                rel_rows = conn.execute(
                    """SELECT to_type AS other_type, to_id AS other_id, relation
                       FROM memory_relations
                       WHERE from_type = ? AND from_id = ? AND relation != 'not_related'
                       UNION ALL
                       SELECT from_type, from_id, relation
                       FROM memory_relations
                       WHERE to_type = ? AND to_id = ? AND relation != 'not_related'
                       LIMIT 12""",
                    (
                        seed["item_type"],
                        int(seed["item_id"]),
                        seed["item_type"],
                        int(seed["item_id"]),
                    ),
                ).fetchall()
                for rel in rel_rows:
                    ot, oid = rel["other_type"], int(rel["other_id"])
                    gk = f"{ot}-{oid}"
                    if gk in seen_graph:
                        continue
                    seen_graph.add(gk)
                    frow = conn.execute(
                        "SELECT item_type, item_id, title, content as snippet, tags, "
                        "rowid as fts_rowid FROM memory_fts "
                        "WHERE item_type = ? AND item_id = ?",
                        (ot, str(oid)),
                    ).fetchone()
                    if not frow:
                        continue
                    if item_type and frow["item_type"] != item_type:
                        continue
                    graph_results.append({
                        "item_type": frow["item_type"],
                        "item_id": frow["item_id"],
                        "title": frow["title"],
                        "snippet": frow["snippet"] or "",
                        "tags": frow["tags"],
                        "rank": 0,
                        "rowid": frow["fts_rowid"],
                        "is_semantic": False,
                        "via_graph": rel["relation"],
                    })
                    if len(graph_results) >= limit:
                        break
                if len(graph_results) >= limit:
                    break
        except Exception:
            logger.debug("graph-hop channel skipped", exc_info=True)
            graph_results = []

        rrf_scores = reciprocal_rank_scores(sem_results, lex_results)
        if graph_results:
            for rank, r in enumerate(graph_results, start=1):
                key = result_key(r)
                rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (60 + rank)
            if rrf_scores:
                m = max(rrf_scores.values()) or 1.0
                rrf_scores = {k: v / m for k, v in rrf_scores.items()}

        sem_map = {result_key(r): r for r in sem_results}
        lex_map = {result_key(r): r for r in lex_results}
        graph_map = {result_key(r): r for r in graph_results}
        ordered_keys = sorted(rrf_scores.keys(), key=lambda kk: rrf_scores[kk], reverse=True)
        for kk in ordered_keys:
            if kk in sem_map:
                results.append(sem_map[kk])
            elif kk in lex_map:
                results.append(lex_map[kk])
            elif kk in graph_map:
                results.append(graph_map[kk])

        usage_counts = {}
        last_used_map = {}
        item_dates = {}

        for itype in usage_ranked_types():
            table = table_for(itype)
            if not table:
                continue
            date_col = "date" if itype in ("mistake", "conversation", "session") else "created_at"
            ids = [int(r["item_id"]) for r in results if r["item_type"] == itype]
            if ids:
                placeholders = ",".join("?" * len(ids))
                brows = conn.execute(
                    f"SELECT id, usage_count, last_used_at, "
                    f"COALESCE({date_col}, created_at) as item_date "
                    f"FROM {table} WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
                for row in brows:
                    key = (itype, row["id"])
                    usage_counts[key] = row["usage_count"] or 0
                    last_used_map[key] = row["last_used_at"]
                    item_dates[key] = row["item_date"]

        from .feedback import feedback_totals

        candidate_keys = [(r["item_type"], int(r["item_id"])) for r in results]
        try:
            feedback_map = feedback_totals(candidate_keys, conn=conn)
        except Exception:
            logger.exception("feedback totals fetch failed; ranking without feedback")
            feedback_map = {}

        from .stability import stability_map as fetch_stability

        try:
            stability_by_key = fetch_stability(candidate_keys, conn=conn)
        except Exception:
            logger.exception("stability fetch failed; ranking with fixed half-life")
            stability_by_key = {}

        entity_boost_keys: set[tuple[str, int]] = set()
        try:
            from .entities import entity_ids_for_query, item_entity_map

            q_ents = entity_ids_for_query(query or "", conn=conn)
            if q_ents and candidate_keys:
                emap = item_entity_map(candidate_keys, conn=conn)
                for ck, eids in emap.items():
                    if eids & q_ents:
                        entity_boost_keys.add(ck)
        except Exception:
            logger.debug("entity boost skipped", exc_info=True)

        affinities = {}
        if project_path:
            try:
                project = get_or_create_project(project_path, conn=conn)
                affinities = get_project_affinities(results, project["id"], conn=conn)
            except Exception:
                logger.exception("get_or_create_project / project affinity failed")

        pinned = get_pinned_items(item_type=item_type, limit=limit, conn=conn)

        invalidated: set[str] = set()
        try:
            from datetime import date as _date

            from .temporal import invalidated_subjects_as_of

            invalidated = invalidated_subjects_as_of(
                as_of or _date.today().isoformat(), conn=conn
            )
        except Exception:
            invalidated = set()

    temporal_intent = detect_temporal_intent(query or "")
    if rank_inputs_sink is not None:
        import copy

        rank_inputs_sink.update(
            {
                "query": query or "",
                "temporal_intent": temporal_intent,
                "candidates": copy.deepcopy(results),
                "usage_counts": dict(usage_counts),
                "last_used_map": dict(last_used_map),
                "affinities": dict(affinities),
                "stale_rowids": set(stale_rowids),
                "detected_tags": list(detected_tags or []),
                "rrf_scores": dict(rrf_scores) if rrf_scores else None,
                "item_dates": dict(item_dates),
                "feedback_map": dict(feedback_map),
                "stability_by_key": dict(stability_by_key),
                "pinned": copy.deepcopy(pinned),
                "filter_tags": list(filter_tags) if filter_tags else [],
                "include_superseded": include_superseded,
                "limit": limit,
                "as_of": as_of,
            }
        )

    results = rank_results(
        results=results,
        feedback_map=feedback_map,
        stability_by_key=stability_by_key,
        usage_counts=usage_counts,
        last_used_map=last_used_map,
        affinities=affinities,
        query=query or "",
        stale_rowids=stale_rowids,
        detected_tags=detected_tags,
        rrf_scores=rrf_scores if rrf_scores else None,
        item_dates=item_dates,
        temporal_intent=temporal_intent,
    )

    ENTITY_BOOST = 18.0
    for r in results:
        key_row = (r["item_type"], int(r["item_id"]))
        ent = ENTITY_BOOST if key_row in entity_boost_keys else 0.0
        if ent:
            r["utility_score"] = r.get("utility_score", 0.0) + ent
        if explain:
            helped = unhelpful = 0
            if feedback_map and key_row in feedback_map:
                helped, unhelpful = feedback_map[key_row]
            r["score_breakdown"] = {
                "utility": round(float(r.get("utility_score", 0.0)), 3),
                "rrf_normalized": r.get("rrf_normalized", 0.0),
                "is_semantic": bool(r.get("is_semantic")),
                "entity_boost": ent,
                "via_graph": r.get("via_graph"),
                "usage": usage_counts.get(key_row, 0),
                "feedback": {"helped": helped, "unhelpful": unhelpful},
                "affinity": affinities.get(key_row),
            }
    if entity_boost_keys:
        results.sort(key=lambda x: x.get("utility_score", 0.0), reverse=True)

    if query and query.strip():
        results = rerank_with_bm25(results, query)
        results = optional_cross_encoder_rerank(results, query)

    if pinned:
        if filter_tags:
            pinned = [
                p for p in pinned
                if all(t.lower() in (p.get("tags") or "").lower() for t in filter_tags)
            ]
        pinned_keys = {result_key(p) for p in pinned}
        results = [r for r in results if result_key(r) not in pinned_keys]
        results = pinned + results

    if not include_superseded:
        results = [
            r for r in results
            if not str(r.get("title", "")).startswith("[SUPERSEDED]")
        ]

    if invalidated:
        results = [
            r
            for r in results
            if f"{r.get('item_type')}:{r.get('item_id')}" not in invalidated
            and f"{r.get('item_type')}:{int(r['item_id'])}" not in invalidated
        ]

    if token_budget and token_budget > 0:
        budget_chars = int(token_budget) * 4
        used = 0
        trimmed = []
        for r in results:
            snip = r.get("snippet") or ""
            title = r.get("title") or ""
            cost = len(title) + len(snip)
            if used + cost > budget_chars and trimmed:
                r = dict(r)
                r["snippet"] = ""
                cost = len(title)
            if used + cost > budget_chars and trimmed:
                break
            used += cost
            trimmed.append(r)
        results = trimmed

    final = SearchResults(results[:limit])
    final.semantic_status = semantic_status
    final.semantic_available = semantic_available
    if not skip_audit:
        append_search_audit(
            query=query or "",
            results=list(final),
            semantic_status=semantic_status,
            source=audit_source,
            item_type=item_type,
            tags=list(tags) if tags else None,
            limit=limit,
            project_path=project_path,
            embed_ms=timing.get("embed_ms"),
            vec_search_ms=timing.get("vec_search_ms"),
        )
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
