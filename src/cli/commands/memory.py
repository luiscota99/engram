"""Memory commands: search, recent, add, list, suggest, consolidate."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from ... import config
from ...database import (
    check_duplicate_before_add,
    delete_item,
    get_connection,
    get_db_path,
    index_in_fts,
    init_db,
    link_tags,
)
from ...maintenance import find_consolidation_candidates
from ...memory_ops import (
    add_decision,
    create_conversation,
    create_mistake,
    create_pattern,
    create_prompt,
    create_session,
    create_skill,
    create_transcript,
)
from ...search import get_recent, get_stats, search, semantic_search
from ...workflow import (
    WorkflowViolationError,
    check_decision_allowed,
    init_session_state,
    record_role_contribution,
)
from ..fmt import fmt_bold, fmt_dim, fmt_header, fmt_type

logger = logging.getLogger(__name__)


def _cli_dedup_gate(args, content: str, item_type: str, *, name: str | None = None) -> bool:
    """Return True if insert should proceed; False if blocked."""
    if getattr(args, "force", False):
        return True
    dedup = check_duplicate_before_add(content, item_type, name=name)
    if not dedup["duplicates"]:
        return True
    print("Near-duplicate detected — insert blocked. Use --force to add anyway.\n")
    for d in dedup["duplicates"]:
        sim = d.get("similarity", "?")
        kind = d.get("match_kind", "similar")
        print(
            f"  [{d['item_type'].upper()} ID:{d['item_id']}] {d.get('title', '')} "
            f"(similarity: {sim}, {kind})"
        )
    return False


def cmd_search(args):
    query = " ".join(args.query) if args.query else ""
    tag_list = [t.strip() for t in args.tags.split(",")] if args.tags else None
    if args.no_project:
        project_path = None
    elif args.project is not None:
        project_path = os.path.abspath(os.path.expanduser(args.project))
    else:
        project_path = os.getcwd()
    results = search(
        query,
        args.type,
        tag_list,
        args.limit,
        project_path=project_path,
        audit_source="cli",
        include_superseded=getattr(args, "include_superseded", False),
    )
    if not results:
        print("No results found.")
        return

    print(f"Found {len(results)} result(s):\n")
    for r in results:
        badge = "[S]" if r.get("is_semantic") else "[K]"
        print(fmt_header(f"  {badge} [{r['item_type'].upper()}] {r['title']}"))
        if r["snippet"]:
            print(f"    {r['snippet'][:120].replace(chr(10), ' ')}...")
        if r["tags"]:
            print(fmt_dim(f"    tags: {r['tags']}"))
        print("")


def cmd_recent(args):
    results = get_recent(limit=args.n, item_type=args.type)
    if not results:
        print(fmt_dim("No entries yet."))
        return

    print(fmt_header(f"Recent {len(results)} entries:\n"))
    for r in results:
        print(f"  {fmt_type(r['item_type'])} {r['title']}")
        if r["tags"]:
            print(f"    {fmt_dim('tags: ' + r['tags'])}")
    print()


def cmd_add(args):
    init_db()
    kind = args.kind
    dispatch = {
        "mistake": _add_mistake,
        "pattern": _add_pattern,
        "skill": _add_skill,
        "conversation": _add_conversation,
        "prompt": _add_prompt,
        "session": _add_session,
        "transcript": _add_transcript,
        "decision": cmd_add_decision,
    }
    fn = dispatch.get(kind)
    if fn:
        fn(args)
    else:
        print(f"Unknown type: {kind}")
        sys.exit(1)


def _add_mistake(args):
    content = (
        f"{args.context} | {args.mistake} | {args.root_cause or ''} | {args.fix} | {args.prevention or ''}"
    )
    if not _cli_dedup_gate(args, content, "mistake"):
        sys.exit(1)
    with get_connection() as conn:
        mid = create_mistake(
            conn,
            date=args.date,
            context=args.context,
            mistake=args.mistake,
            fix=args.fix,
            root_cause=args.root_cause,
            prevention=args.prevention,
            conversation_id=args.conversation,
            tags=args.tags,
        )
    print(f"✓ Mistake #{mid} logged.")


def _add_pattern(args):
    content = f"{args.symptoms} | {args.root_cause} | {args.fix}"
    if not _cli_dedup_gate(args, content, "pattern", name=args.name):
        sys.exit(1)
    with get_connection() as conn:
        pid = create_pattern(
            conn,
            name=args.name,
            symptoms=args.symptoms,
            root_cause=args.root_cause,
            standard_fix=args.fix,
            tags=args.tags,
        )
    print(f"✓ Pattern #{pid} '{args.name}' logged.")


def _add_skill(args):
    content = f"{args.trigger} | {args.workflow} | {args.pitfalls or ''}"
    if not _cli_dedup_gate(args, content, "skill", name=args.name):
        sys.exit(1)
    with get_connection() as conn:
        sid = create_skill(
            conn,
            name=args.name,
            domain=args.domain,
            trigger=args.trigger,
            workflow=args.workflow,
            pitfalls=args.pitfalls,
            key_files=args.files,
            dependencies=args.dependencies,
            tags=args.tags,
        )
    print(f"✓ Skill #{sid} '{args.name}' logged.")


def _add_conversation(args):
    with get_connection() as conn:
        cid = create_conversation(
            conn,
            conversation_id=args.id,
            title=args.title,
            date=args.date,
            domain=args.domain,
            tasks_completed=args.tasks,
            key_decisions=args.decisions,
            mistakes_summary=args.mistakes,
            skills_extracted=args.skills,
            tags=args.tags,
        )
    print(f"✓ Conversation #{cid} '{args.title}' logged.")


def _add_session(args):
    with get_connection() as conn:
        create_session(
            conn,
            session_id=args.id,
            title=args.title,
            date=args.date,
            domain=args.domain,
            workflow_used=args.workflow_used,
        )
    init_session_state(args.id, workflow_name=args.workflow_used)
    print(f"✓ Session '{args.id}' initialized.")


def _add_transcript(args):
    with get_connection() as conn:
        create_transcript(
            conn,
            session_id=args.session_id,
            role=args.role,
            content=args.content,
        )
    record_role_contribution(args.session_id, args.role)
    print(f"✓ Transcript entry for '{args.role}' added to session '{args.session_id}'.")


def cmd_add_decision(args):
    if not getattr(args, "force_bypass", False):
        try:
            check_decision_allowed(args.session_id)
        except WorkflowViolationError as e:
            print(f"WorkflowViolation: {e}")
            sys.exit(1)
    else:
        logger.warning(
            "Workflow gate bypassed for session %s (--force-bypass)",
            args.session_id,
        )
    with get_connection() as conn:
        add_decision(conn, session_id=args.session_id, decision=args.decision)
    print(f"✓ Decision added to session '{args.session_id}'.")


def _add_prompt(args):
    prompt_text = args.prompt_text or ""
    if args.file:
        with open(args.file, "r") as f:
            prompt_text = f.read()
    with get_connection() as conn:
        pid = create_prompt(
            conn,
            name=args.name,
            role=args.role,
            domain=args.domain,
            description=args.description,
            prompt_text=prompt_text,
            source_path=args.file,
            best_for=args.best_for,
            tags=args.tags,
        )
    print(f"✓ Prompt #{pid} '{args.name}' stored.")


def _batch_tags(conn, item_type, ids):
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT it.item_id, GROUP_CONCAT(t.name, ',') as tags
            FROM item_tags it JOIN tags t ON t.id = it.tag_id
            WHERE it.item_type = ? AND it.item_id IN ({placeholders})
            GROUP BY it.item_id""",
        [item_type] + list(ids),
    ).fetchall()
    return {r["item_id"]: r["tags"].split(",") if r["tags"] else [] for r in rows}


def cmd_list(args):
    kind = args.kind
    with get_connection() as conn:
        if kind == "mistakes":
            rows = conn.execute("SELECT id, date, mistake, fix FROM mistakes ORDER BY date DESC").fetchall()
            tags_map = _batch_tags(conn, "mistake", [r["id"] for r in rows])
            print(fmt_header(f"Mistakes ({len(rows)}):\n"))
            for r in rows:
                tags = tags_map.get(r["id"], [])
                print(f"  {fmt_type('mistake')} #{r['id']} [{r['date']}] {r['mistake'][:80]}")
                print(f"    Fix: {fmt_dim(r['fix'][:100])}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        elif kind == "patterns":
            rows = conn.execute("SELECT id, name, symptoms, standard_fix FROM patterns ORDER BY name").fetchall()
            tags_map = _batch_tags(conn, "pattern", [r["id"] for r in rows])
            occ_rows = conn.execute("SELECT pattern_id, COUNT(*) as c FROM pattern_occurrences GROUP BY pattern_id").fetchall()
            occ_map = {r["pattern_id"]: r["c"] for r in occ_rows}
            print(fmt_header(f"Patterns ({len(rows)}):\n"))
            for r in rows:
                tags = tags_map.get(r["id"], [])
                occ = occ_map.get(r["id"], 0)
                print(f"  {fmt_type('pattern')} {fmt_bold(r['name'])} ({occ} occurrence{'s' if occ != 1 else ''})")
                print(f"    Symptoms: {fmt_dim(r['symptoms'][:100])}")
                print(f"    Fix: {fmt_dim(r['standard_fix'][:100])}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        elif kind == "skills":
            rows = conn.execute("SELECT id, name, domain, trigger_desc FROM skills ORDER BY name").fetchall()
            tags_map = _batch_tags(conn, "skill", [r["id"] for r in rows])
            print(fmt_header(f"Skills ({len(rows)}):\n"))
            for r in rows:
                tags = tags_map.get(r["id"], [])
                print(f"  {fmt_type('skill')} {fmt_bold(r['name'])} [{r['domain']}]")
                print(f"    When: {fmt_dim(r['trigger_desc'][:100])}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        elif kind == "conversations":
            rows = conn.execute("SELECT id, conversation_id, title, date, domain FROM conversations ORDER BY date DESC").fetchall()
            tags_map = _batch_tags(conn, "conversation", [r["id"] for r in rows])
            print(fmt_header(f"Conversations ({len(rows)}):\n"))
            for r in rows:
                tags = tags_map.get(r["id"], [])
                print(f"  {fmt_type('conversation')} [{r['date']}] {fmt_bold(r['title'])}")
                print(f"    Domain: {r['domain']} | ID: {fmt_dim(r['conversation_id'][:12] + '...')}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        elif kind == "sessions":
            rows = conn.execute("SELECT id, session_id, title, date, domain, workflow_used FROM sessions ORDER BY date DESC").fetchall()
            print(fmt_header(f"Sessions ({len(rows)}):\n"))
            for r in rows:
                print(f"  {fmt_type('session')} [{r['date']}] {fmt_bold(r['title'])}")
                print(f"    Domain: {r['domain']} | ID: {fmt_dim(r['session_id'][:12] + '...')} | Workflow: {r['workflow_used']}")
                print()

        elif kind == "prompts":
            rows = conn.execute("SELECT id, name, role, domain, best_for FROM prompts ORDER BY name").fetchall()
            tags_map = _batch_tags(conn, "prompt", [r["id"] for r in rows])
            print(fmt_header(f"Prompts ({len(rows)}):\n"))
            for r in rows:
                tags = tags_map.get(r["id"], [])
                print(f"  {fmt_type('prompt')} {fmt_bold(r['name'])} [{r['domain']}]")
                print(f"    Role: {fmt_dim(r['role'][:100])}")
                if r["best_for"]:
                    print(f"    Best for: {fmt_dim(r['best_for'][:100])}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        else:
            print(f"Unknown type: {kind}. Use: mistakes, patterns, skills, conversations, prompts, sessions")
            sys.exit(1)


def cmd_suggest(args):
    query = " ".join(args.query) if args.query else ""
    results = []
    is_semantic = False
    if len(query.split()) > 2:
        sem_results, _ = semantic_search(query, limit=args.limit)
        item_type = getattr(args, "type", "prompt")
        results = [r for r in sem_results if r["item_type"] == item_type]
        if results:
            is_semantic = True
    if not results:
        results = search(query, item_type=getattr(args, "type", "prompt"), limit=args.limit)
    if not results:
        print(fmt_dim(f"No matching {getattr(args, 'type', 'prompt')}s found."))
        return
    search_type = "Semantic" if is_semantic else "Lexical"
    print(fmt_header(f"Suggested {getattr(args, 'type', 'prompt')}s ({search_type}) for: {query}\n"))
    for r in results:
        print(f"  {fmt_type('prompt')} {fmt_bold(r['title'])}")
        if r["snippet"]:
            snippet = r["snippet"].replace("\n", " ")[:150]
            print(f"    {fmt_dim(snippet)}")
        if r["tags"]:
            print(f"    {fmt_dim('tags: ' + r['tags'])}")
        print()


def cmd_stats(args):
    stats = get_stats()
    emb = stats.get("embeddings", {})
    print(fmt_header("Engram Stats\n"))
    print(f"  Mistakes:      {stats['mistakes']}")
    print(f"  Patterns:      {stats['patterns']}")
    print(f"  Skills:        {stats['skills']}")
    print(f"  Conversations: {stats['conversations']}")
    print(f"  Prompts:       {stats['prompts']}")
    print(f"  Tags:          {stats['tags']}")
    print(f"  FTS indexed:   {stats['fts_indexed']}")
    if emb:
        total = emb.get("total", 0)
        model = emb.get("model", "unknown")
        print(f"\n  Embedding Status (model: {fmt_dim(model)}):")
        if total > 0:
            def pct(n): return f"{100*n/total:.1f}%"
            print(f"    Ready:   {emb['ready']:4d} ({pct(emb['ready'])})")
            if emb.get("stale"):
                print(f"    Stale:   {emb['stale']:4d} ({pct(emb['stale'])})  ← run `engram reembed`")
            if emb.get("pending"):
                print(f"    Pending: {emb['pending']:4d} ({pct(emb['pending'])})")
            if emb.get("failed"):
                print(f"    Failed:  {emb['failed']:4d} ({pct(emb['failed'])})")
        else:
            print(fmt_dim("    No embeddings tracked yet."))
    print(f"\n  DB path: {fmt_dim(get_db_path())}")


def cmd_link_pattern(args):
    with get_connection() as conn:
        pattern = conn.execute("SELECT id FROM patterns WHERE name = ?", (args.name,)).fetchone()
        if not pattern:
            print(f"Pattern '{args.name}' not found.")
            sys.exit(1)
        conn.execute(
            "INSERT INTO pattern_occurrences (pattern_id, conversation_id, date, notes) VALUES (?, ?, ?, ?)",
            (pattern["id"], args.conversation, args.date, args.notes),
        )
    print(f"✓ Linked pattern '{args.name}' to conversation.")


def cmd_consolidate(args):
    ids = [int(i.strip()) for i in args.delete_ids.split(",") if i.strip()]
    if not ids:
        print("Error: --delete-ids requires at least one ID.")
        sys.exit(1)
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (args.name, args.domain, args.trigger, args.workflow, args.pitfalls, args.key_files, args.deps),
        )
        sid = cursor.lastrowid
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        link_tags(conn, "skill", sid, tags)
        content = f"{args.trigger} | {args.workflow} | {args.pitfalls or ''}"
        index_in_fts(conn, "skill", sid, args.name, content, tags)
        for old_id in ids:
            delete_item(conn, "skill", old_id)
    print(f"✓ Consolidated {len(ids)} skills into new Master Skill #{sid}.")


def cmd_suggest_consolidate(args):
    clusters, skip_reason = find_consolidation_candidates(
        threshold=args.threshold,
        item_types=[args.type] if args.type else None,
    )
    if not clusters:
        if skip_reason == "unchanged":
            print(fmt_dim("Memory unchanged since last scan — no new candidates."))
        else:
            print(fmt_dim("No consolidation candidates found at this similarity threshold."))
        return
    total = sum(c["cluster_size"] for c in clusters[:args.limit])
    print(fmt_header(f"Consolidation Candidates (similarity ≥ {args.threshold}):\n"))
    for i, cluster in enumerate(clusters[:args.limit], 1):
        size = cluster["cluster_size"]
        avg_sim = cluster["avg_similarity"]
        size_label = f"{size} items" if size > 2 else "pair"
        print(f"  Cluster {i} — {fmt_type(cluster['item_type'])}  {size_label}  (avg similarity: {avg_sim})")
        ids = []
        for item in cluster["items"]:
            print(f"    ID:{item['item_id']}  {item['title']}")
            ids.append(str(item["item_id"]))
        print(fmt_dim(f"    → engram consolidate --delete-ids {','.join(ids)} --name \"...\" --domain \"...\" --trigger \"...\" --workflow \"...\""))
        print()
    print(fmt_dim(f"Total: {len(clusters[:args.limit])} cluster(s) covering {total} items."))


def cmd_suggest_capture(args):
    from ...capture import format_capture_suggestion, suggest_capture

    files = [f.strip() for f in args.files.split(",")] if args.files else []
    suggestion = suggest_capture(
        task_description=args.task,
        outcome=args.outcome,
        errors_encountered=args.errors or "",
        files_changed=files,
    )
    if getattr(args, "json", False):
        def _default(o):
            if isinstance(o, (set, frozenset)):
                return list(o)
            raise TypeError

        print(json.dumps(suggestion, indent=2, default=_default))
    else:
        print(format_capture_suggestion(suggestion))


def cmd_session_help(args):
    """Append one JSON line with Session Help Score (0–3) for local rollup."""
    score = args.score
    if score < 0 or score > 3:
        print("Error: --score must be between 0 and 3.", file=sys.stderr)
        sys.exit(1)
    log_path = config.session_help_log_path()
    parent = os.path.dirname(os.path.abspath(log_path))
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "note": (args.note or "")[:2000],
        "task": (args.task or "")[:500],
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"Error writing session-help log: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Logged Session Help Score {score} → {log_path}")
