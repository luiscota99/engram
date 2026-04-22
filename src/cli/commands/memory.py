"""Memory commands: search, recent, add, list, suggest, consolidate."""
from __future__ import annotations

import sys

from ...database import (
    delete_item,
    get_connection,
    get_db_path,
    index_in_fts,
    init_db,
    link_tags,
)
from ...maintenance import find_consolidation_candidates
from ...search import get_recent, get_stats, search, semantic_search
from ..fmt import fmt_bold, fmt_dim, fmt_header, fmt_type


def cmd_search(args):
    query = " ".join(args.query) if args.query else ""
    tag_list = [t.strip() for t in args.tags.split(",")] if args.tags else None
    results = search(query, args.type, tag_list, args.limit)
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
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO mistakes (date, context, mistake, root_cause, fix, prevention, conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (args.date, args.context, args.mistake, args.root_cause, args.fix, args.prevention, args.conversation),
        )
        mid = cursor.lastrowid
        tags = args.tags.split(",") if args.tags else []
        link_tags(conn, "mistake", mid, tags)
        content = f"{args.context} | {args.mistake} | {args.root_cause or ''} | {args.fix} | {args.prevention or ''}"
        index_in_fts(conn, "mistake", mid, args.mistake[:80], content, tags)
    print(f"✓ Mistake #{mid} logged.")


def _add_pattern(args):
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO patterns (name, symptoms, root_cause, standard_fix) VALUES (?, ?, ?, ?)",
            (args.name, args.symptoms, args.root_cause, args.fix),
        )
        pid = cursor.lastrowid
        tags = args.tags.split(",") if args.tags else []
        link_tags(conn, "pattern", pid, tags)
        content = f"{args.symptoms} | {args.root_cause} | {args.fix}"
        index_in_fts(conn, "pattern", pid, args.name, content, tags)
    print(f"✓ Pattern #{pid} '{args.name}' logged.")


def _add_skill(args):
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (args.name, args.domain, args.trigger, args.workflow, args.pitfalls, args.files, args.dependencies),
        )
        sid = cursor.lastrowid
        tags = args.tags.split(",") if args.tags else []
        link_tags(conn, "skill", sid, tags)
        content = f"{args.trigger} | {args.workflow} | {args.pitfalls or ''}"
        index_in_fts(conn, "skill", sid, args.name, content, tags)
    print(f"✓ Skill #{sid} '{args.name}' logged.")


def _add_conversation(args):
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO conversations (conversation_id, title, date, domain, tasks_completed,
               key_decisions, mistakes_summary, skills_extracted) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (args.id, args.title, args.date, args.domain, args.tasks, args.decisions, args.mistakes, args.skills),
        )
        cid = cursor.lastrowid
        tags = args.tags.split(",") if args.tags else []
        link_tags(conn, "conversation", cid, tags)
        content = f"{args.tasks or ''} | {args.decisions or ''} | {args.mistakes or ''}"
        index_in_fts(conn, "conversation", cid, args.title, content, tags)
    print(f"✓ Conversation #{cid} '{args.title}' logged.")


def _add_session(args):
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO sessions (session_id, title, date, domain, workflow_used) VALUES (?, ?, ?, ?, ?)",
            (args.id, args.title, args.date, args.domain, args.workflow_used),
        )
        sid = cursor.lastrowid
        content = f"{args.title} | {args.workflow_used or ''}"
        index_in_fts(conn, "session", sid, args.id, content, [])
    print(f"✓ Session '{args.id}' initialized.")


def _add_transcript(args):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO session_transcripts (session_id, role, content) VALUES (?, ?, ?)",
            (args.session_id, args.role, args.content),
        )
    print(f"✓ Transcript entry for '{args.role}' added to session '{args.session_id}'.")


def cmd_add_decision(args):
    with get_connection() as conn:
        conn.execute(
            "UPDATE sessions SET key_decisions = IFNULL(key_decisions, '') || char(10) || ? WHERE session_id = ?",
            (args.decision, args.session_id),
        )
    print(f"✓ Decision added to session '{args.session_id}'.")


def _add_prompt(args):
    prompt_text = args.prompt_text or ""
    if args.file:
        with open(args.file, "r") as f:
            prompt_text = f.read()
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO prompts (name, role, domain, description, prompt_text, source_path, best_for)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (args.name, args.role, args.domain, args.description, prompt_text, args.file, args.best_for),
        )
        pid = cursor.lastrowid
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
        link_tags(conn, "prompt", pid, tags)
        content = f"{args.role} | {args.description} | {args.best_for or ''} | {prompt_text[:500]}"
        index_in_fts(conn, "prompt", pid, args.name, content, tags)
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
    clusters = find_consolidation_candidates(
        threshold=args.threshold,
        item_types=[args.type] if args.type else None,
    )
    if not clusters:
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
    import json

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
