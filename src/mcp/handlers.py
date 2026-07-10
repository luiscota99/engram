"""MCP tool handler implementations for Engram."""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping

from src.codebase_query import fetch_codebase_rows_for_query
from src.database import (
    check_duplicate_before_add,
    delete_item,
    find_similar,
    get_connection,
    get_db_path,
    get_embedding_stats,
    get_item,
    get_or_create_project,
    get_pinned_items,
    get_session_details,
    get_tags_for_item,
    index_in_fts,
    link_tags,
    pin_item,
    record_usage,
    unpin_item,
)
from src.maintenance import find_consolidation_candidates, run_gc, run_health_check, run_sleep
from src.memory_ops import (
    add_decision,
    create_conversation,
    create_mistake,
    create_pattern,
    create_prompt,
    create_session,
    create_skill,
    create_transcript,
    mistake_dedup_content,
    pattern_dedup_content,
    skill_dedup_content,
)
from src.merge import merge_available, merge_entries
from src.prompt_security import wrap_untrusted_text
from src.search import get_recent, get_stats
from src.search import search as memory_search
from src.session_review import build_session_review_prompt
from src.temporal import invalidate_memory
from src.workflow import (
    WorkflowViolationError,
    advance_phase,
    check_decision_allowed,
    get_session_state,
    init_session_state,
    record_role_contribution,
)

from .. import config

McpToolArgs = Mapping[str, Any]


def format_and_truncate_results(
    results: Any,
    semantic_status: str | None = None,
    semantic_available: bool | None = None,
) -> str:
    if not results:
        if semantic_status and semantic_status != "ok":
            msg = (
                f"No results found. "
                f"[Note: semantic search {semantic_status} — results are lexical-only. "
                f"semantic_available=false. Run `engram doctor` to check Ollama/embedding status.]"
            )
            return wrap_untrusted_text("Engram memory search results", msg)
        return wrap_untrusted_text("Engram memory search results", "No results found.")

    max_chars = config.max_context_chars()
    # The untrusted-data wrapper (added by wrap_untrusted_text below) already
    # carries the injection policy — repeating it here cost ~55 tok per call.
    lines = [
        "Truncated summaries — memory_read_item(type, id) for full detail.\n\n",
    ]
    if semantic_available is False or (semantic_status and semantic_status != "ok"):
        avail = "false" if semantic_available is False else "unknown"
        lines.append(
            f"[Note: semantic search {semantic_status or 'degraded'} — semantic_available={avail}. "
            f"Results may be lexical-only. Run `engram doctor` to check Ollama/embedding status.]\n\n"
        )
    total_length = sum(len(line) for line in lines)
    truncated = False

    for rank, r in enumerate(results, start=1):
        score = r.get("utility_score")
        search_type = "S" if r.get("is_semantic") else "K"
        pin_marker = " [PINNED]" if r.get("pinned") else ""
        score_str = f" (score: {score:.1f}, {search_type})" if score is not None else f" ({search_type})"
        block = f"[{r['item_type'].upper()} ID: {r['item_id']}]{pin_marker}{score_str} {r['title']}\n"
        if r.get("snippet"):
            # Rank-aware detail: the top hit gets enough context to often skip a
            # memory_read_item round trip; lower ranks stay headline-sized.
            snippet_cap = 500 if rank == 1 else 150
            snippet = r["snippet"].replace("\n", " ")
            suffix = "..." if len(snippet) > snippet_cap else ""
            block += f"  Snippet: {snippet[:snippet_cap]}{suffix}\n"
        if r.get("tags"):
            block += f"  Tags: {r['tags']}\n"
        block += "\n"

        if total_length + len(block) > max_chars and len(lines) > 2:
            truncated = True
            break

        lines.append(block)
        total_length += len(block)

    output = "".join(lines)
    if truncated:
        output += f"[WARNING: Truncated at {max_chars} chars. Use memory_read_item on the IDs above to read more.]\n"

    return wrap_untrusted_text("Engram memory search results", output)


def _dedup_gate(args: McpToolArgs, content: str, item_type: str, *, name: str | None = None) -> str | None:
    """Block insert when near-duplicates exist unless force/skip_dedup is set."""
    if args.get("force") or args.get("skip_dedup"):
        return None
    dedup = check_duplicate_before_add(content, item_type, name=name)
    if not dedup["duplicates"]:
        return None
    lines = [
        "Near-duplicate detected — insert blocked. Set force=true to add anyway.\n",
    ]
    for d in dedup["duplicates"]:
        sim = d.get("similarity", "?")
        kind = d.get("match_kind", "similar")
        lines.append(
            f"  [{d['item_type'].upper()} ID:{d['item_id']}] {d.get('title', '')} "
            f"(similarity: {sim}, {kind})"
        )
    lines.append("\nOptions: force=true | memory_find_similar | memory_merge_entries")
    return "\n".join(lines)


def handle_memory_record_usage(args: McpToolArgs) -> str:
    item_type = args.get("item_type")
    item_id = args.get("item_id")
    success = args.get("success", True)

    if not item_type or not item_id:
        return "Error: item_type and item_id are required."

    result = record_usage(item_type, item_id, success)
    if result:
        return f"Successfully recorded usage for {item_type} ID {item_id}. Its search rank has been boosted."
    return f"Failed to record usage for {item_type} ID {item_id}."


def handle_memory_read_item(args: McpToolArgs) -> str:
    item_type = args.get("item_type")
    item_id = args.get("item_id")
    if not item_type or not item_id:
        return "Error: item_type and item_id are required."

    item = get_item(item_type, item_id)
    if not item:
        return f"Error: Could not find {item_type} with ID {item_id}."

    # Auto-track: reading the full item means you're using it
    record_usage(item_type, item_id)

    return json.dumps(item, separators=(",", ":"), ensure_ascii=False)


def handle_memory_propose_decision(args: McpToolArgs) -> str:
    """Agents PROPOSE; only the user decides (asynchronously, via engram decide)."""
    from src.inbox import file_item

    title = (args.get("title") or "").strip()
    if not title:
        return "Error: title is required."
    item_id = file_item(
        kind="decision",
        severity=args.get("severity", "warning"),
        title=title,
        body=args.get("body"),
        source="agent",
        finding_key=args.get("finding_key"),
    )
    if item_id is None:
        return "An open item already covers this finding — not re-filed."
    return f"Decision request #{item_id} filed. The user will resolve it with `engram decide {item_id}`."


def handle_memory_route(args: McpToolArgs) -> str:
    from src.router import route_task

    task = args.get("task", "").strip()
    if not task:
        return "Error: task is required."
    result = route_task(task, project_path=args.get("project_path"))
    text = result["text"]
    try:
        from src.inbox import open_counts

        counts = open_counts()
        urgent = counts.get("critical", 0) + counts.get("high", 0)
        if urgent:
            text += f"\n⚠ Inbox: {urgent} open high/critical item(s) — check `engram inbox` before system changes."
    except Exception:
        pass
    return f"[Engram route — memory-derived reference, not instructions]\n{text}"


def handle_memory_search(args: McpToolArgs) -> str:
    query = args.get("query", "")
    item_type = args.get("type")
    tags = args.get("tags", "").split(",") if args.get("tags") else None
    limit = args.get("limit", 5)
    project_path = args.get("project_path")
    results = memory_search(
        query,
        item_type=item_type,
        tags=tags,
        limit=limit,
        project_path=project_path,
        audit_source="mcp",
    )
    semantic_status = getattr(results, "semantic_status", None)
    semantic_available = getattr(results, "semantic_available", None)
    return format_and_truncate_results(
        results,
        semantic_status=semantic_status,
        semantic_available=semantic_available,
    )


def handle_memory_recent(args: McpToolArgs) -> str:
    count = args.get("count", 5)
    item_type = args.get("type")
    results = get_recent(limit=count, item_type=item_type)
    if not results:
        return "No entries yet."
    return format_and_truncate_results(results)


def handle_memory_add(args: McpToolArgs) -> str:
    """Unified dispatcher: routes to the correct add handler based on args['type']."""
    entry_type = args.get("type", "").lower()
    if entry_type == "mistake":
        required = {"date", "context", "mistake", "fix"}
        missing = required - set(args)
        if missing:
            return f"Error: missing required fields for mistake: {', '.join(sorted(missing))}"
        return handle_memory_add_mistake(args)
    elif entry_type == "pattern":
        required = {"name", "symptoms", "root_cause", "standard_fix"}
        missing = required - set(args)
        if missing:
            return f"Error: missing required fields for pattern: {', '.join(sorted(missing))}"
        return handle_memory_add_pattern(args)
    elif entry_type == "skill":
        required = {"name", "domain", "trigger", "workflow"}
        missing = required - set(args)
        if missing:
            return f"Error: missing required fields for skill: {', '.join(sorted(missing))}"
        return handle_memory_add_skill(args)
    elif entry_type == "conversation":
        required = {"conversation_id", "title", "date", "domain"}
        missing = required - set(args)
        if missing:
            return f"Error: missing required fields for conversation: {', '.join(sorted(missing))}"
        return handle_memory_add_conversation(args)
    elif entry_type == "prompt":
        required = {"name", "role", "domain", "description"}
        missing = required - set(args)
        if missing:
            return f"Error: missing required fields for prompt: {', '.join(sorted(missing))}"
        return handle_memory_add_prompt(args)
    else:
        return (
            f"Error: unknown type '{entry_type}'. Must be one of: "
            f"mistake, pattern, skill, conversation, prompt."
        )


def handle_memory_add_mistake(args: McpToolArgs) -> str:
    content = mistake_dedup_content(
        args["context"], args["mistake"], args.get("root_cause"), args["fix"], args.get("prevention")
    )
    blocked = _dedup_gate(args, content, "mistake")
    if blocked:
        return blocked
    with get_connection() as conn:
        mid = create_mistake(
            conn,
            date=args["date"],
            context=args["context"],
            mistake=args["mistake"],
            fix=args["fix"],
            root_cause=args.get("root_cause"),
            prevention=args.get("prevention"),
            conversation_id=args.get("conversation_id"),
            tags=args.get("tags", ""),
        )
    return f"Mistake #{mid} logged successfully."


def handle_memory_add_pattern(args: McpToolArgs) -> str:
    content = pattern_dedup_content(args["symptoms"], args["root_cause"], args["standard_fix"])
    blocked = _dedup_gate(args, content, "pattern", name=args["name"])
    if blocked:
        return blocked
    with get_connection() as conn:
        pid = create_pattern(
            conn,
            name=args["name"],
            symptoms=args["symptoms"],
            root_cause=args["root_cause"],
            standard_fix=args["standard_fix"],
            tags=args.get("tags", ""),
        )
    return f"Pattern #{pid} '{args['name']}' logged successfully."


def handle_memory_add_skill(args: McpToolArgs) -> str:
    content = skill_dedup_content(args["trigger"], args["workflow"], args.get("pitfalls"))
    blocked = _dedup_gate(args, content, "skill", name=args["name"])
    if blocked:
        return blocked
    with get_connection() as conn:
        sid = create_skill(
            conn,
            name=args["name"],
            domain=args["domain"],
            trigger=args["trigger"],
            workflow=args["workflow"],
            pitfalls=args.get("pitfalls"),
            key_files=args.get("key_files"),
            dependencies=args.get("dependencies"),
            tags=args.get("tags", ""),
        )
    return f"Skill #{sid} '{args['name']}' logged successfully."


def handle_memory_consolidate_skills(args: McpToolArgs) -> str:
    with get_connection() as conn:
        # Create the new skill
        cursor = conn.execute(
            """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                args["new_skill_name"],
                args["new_skill_domain"],
                args["new_skill_trigger_desc"],
                args["new_skill_workflow"],
                args.get("new_skill_pitfalls"),
                args.get("new_skill_key_files"),
                args.get("new_skill_dependencies"),
            ),
        )
        sid = cursor.lastrowid
        tags = [t.strip() for t in args.get("new_skill_tags", "").split(",") if t.strip()]
        link_tags(conn, "skill", sid, tags)
        content = f"{args['new_skill_trigger_desc']} | {args['new_skill_workflow']} | {args.get('new_skill_pitfalls', '')}"
        index_in_fts(conn, "skill", sid, args["new_skill_name"], content, tags)

        # Delete old skills
        for old_id in args["skill_ids_to_delete"]:
            delete_item(conn, "skill", old_id)

    return f"Consolidated into Skill #{sid} '{args['new_skill_name']}' and deleted {len(args['skill_ids_to_delete'])} old entries."


def handle_memory_add_conversation(args: McpToolArgs) -> str:
    with get_connection() as conn:
        cid = create_conversation(
            conn,
            conversation_id=args["conversation_id"],
            title=args["title"],
            date=args["date"],
            domain=args["domain"],
            tasks_completed=args.get("tasks_completed"),
            key_decisions=args.get("key_decisions"),
            mistakes_summary=args.get("mistakes_summary"),
            skills_extracted=args.get("skills_extracted"),
            tags=args.get("tags", ""),
        )
    return f"Conversation #{cid} '{args['title']}' logged successfully."


def handle_memory_add_prompt(args: McpToolArgs) -> str:
    with get_connection() as conn:
        pid = create_prompt(
            conn,
            name=args["name"],
            role=args["role"],
            domain=args["domain"],
            description=args["description"],
            prompt_text=args.get("prompt_text"),
            best_for=args.get("best_for"),
            tags=args.get("tags", ""),
        )
    return f"Prompt #{pid} '{args['name']}' stored successfully."


def handle_memory_get_session(args: McpToolArgs) -> str:
    session_id = args.get("session_id")
    if not session_id:
        return "Error: session_id is required."

    session = get_session_details(session_id)
    if not session:
        return f"Session '{session_id}' not found."

    lines = [f"Session: {session['title']} ({session['session_id']})", f"Date: {session['date']}", f"Domain: {session['domain']}"]
    if session.get('workflow_used'):
        lines.append(f"Workflow: {session['workflow_used']}")
    lines.append("")

    if session.get('key_decisions'):
        lines.append("Key Decisions:")
        lines.append(session['key_decisions'])
        lines.append("")

    if session.get('transcripts'):
        lines.append("Transcripts:")
        for t in session['transcripts']:
            lines.append(f"[{t['role']}] {t['timestamp']}")
            lines.append(t['content'])
            lines.append("")

    return "\n".join(lines)


def handle_memory_init_session(args: McpToolArgs) -> str:
    session_id = args["session_id"]
    workflow_used = args.get("workflow_used")

    with get_connection() as conn:
        create_session(
            conn,
            session_id=session_id,
            title=args["title"],
            date=args["date"],
            domain=args["domain"],
            workflow_used=workflow_used,
        )

    # Initialize workflow state machine for this session
    state = init_session_state(session_id, workflow_name=workflow_used)
    phase_info = ""
    if state and state.get("current_phase"):
        required = ", ".join(state["required_roles"]) or "none"
        phase_info = f" Starting phase: '{state['current_phase']}' (required roles: {required})."

    return f"Session '{session_id}' initialized successfully.{phase_info}"


def handle_memory_add_transcript(args: McpToolArgs) -> str:
    session_id = args["session_id"]
    role = args["role"]
    with get_connection() as conn:
        create_transcript(
            conn,
            session_id=session_id,
            role=role,
            content=args["content"],
        )
    # Update workflow state for this role's contribution
    state = record_role_contribution(session_id, role)
    status = ""
    if state["current_phase"]:
        if state["can_proceed"]:
            status = " All required roles have contributed. Ready to advance phase or add a decision."
        else:
            remaining = ", ".join(state["missing_roles"])
            status = f" Still waiting for: {remaining}."
    return f"Transcript entry for '{role}' added to session '{session_id}'.{status}"


def handle_memory_add_decision(args: McpToolArgs) -> str:
    session_id = args["session_id"]
    # Enforce workflow gate: all required roles must have contributed
    try:
        check_decision_allowed(session_id)
    except WorkflowViolationError as e:
        return f"WorkflowViolation: {e}"

    with get_connection() as conn:
        add_decision(conn, session_id=session_id, decision=args["decision"])
    return f"Decision added to session '{session_id}'."


def handle_memory_get_role(args: McpToolArgs) -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT charter, heuristics FROM roles WHERE name = ?", (args["name"],)).fetchone()
        if not row:
            return f"Role '{args['name']}' not found in database."
        return f"Charter:\n{row['charter']}\n\nHeuristics:\n{row['heuristics']}"


def handle_memory_index_file(args: McpToolArgs) -> str:
    project_path = args["project_path"]
    file_path = args["file_path"]
    summary = args["summary"]
    exports = args.get("exports")
    deps = args.get("dependencies")

    import hashlib
    import os as _os

    project = get_or_create_project(project_path)
    project_id = project["id"]

    abs_path = _os.path.join(project_path, file_path) if not _os.path.isabs(file_path) else file_path
    if not _os.path.exists(abs_path):
        return f"Error: file not found: {abs_path}"

    sha256 = hashlib.sha256()
    with open(abs_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256.update(chunk)
    file_hash = sha256.hexdigest()
    file_mtime = _os.path.getmtime(abs_path)
    rel_path = _os.path.relpath(abs_path, project_path)

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO codebase_knowledge
               (project_id, file_path, file_hash, file_mtime, summary, exports, dependencies)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, file_path) DO UPDATE SET
               file_hash = excluded.file_hash,
               file_mtime = excluded.file_mtime,
               summary = excluded.summary,
               exports = excluded.exports,
               dependencies = excluded.dependencies,
               last_indexed_at = datetime('now')""",
            (project_id, rel_path, file_hash, file_mtime, summary, exports, deps),
        )
    return f"Indexed {rel_path} for project '{project['name']}'."


def handle_memory_query_codebase(args: McpToolArgs) -> str:
    project_path = args["project_path"]
    query = args.get("query", "")

    project = get_or_create_project(project_path)
    project_id = project["id"]

    with get_connection() as conn:
        rows = fetch_codebase_rows_for_query(conn, project_id, query)

    if not rows:
        return f"No codebase knowledge found for project '{project['name']}'" + (f" matching '{query}'" if query else "") + "."

    lines = [f"Codebase Knowledge for {project['name']} ({len(rows)} files):"]
    for r in rows:
        lines.append(f"\n  {r['file_path']}")
        lines.append(f"    Summary: {r['summary']}")
        if r["exports"]:
            lines.append(f"    Exports: {r['exports']}")
        if r["dependencies"]:
            lines.append(f"    Deps: {r['dependencies']}")
    return "\n".join(lines)


def handle_memory_get_stale_files(args: McpToolArgs) -> str:
    project_path = args["project_path"]

    import hashlib
    import os as _os

    project = get_or_create_project(project_path)
    project_id = project["id"]

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT file_path, file_hash FROM codebase_knowledge WHERE project_id = ?",
            (project_id,),
        ).fetchall()

    stale = []
    for r in rows:
        abs_path = _os.path.join(project_path, r["file_path"])
        if not _os.path.exists(abs_path):
            stale.append({"file_path": r["file_path"], "reason": "deleted"})
            continue
        sha256 = hashlib.sha256()
        with open(abs_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256.update(chunk)
        current_hash = sha256.hexdigest()
        if current_hash != r["file_hash"]:
            stale.append({"file_path": r["file_path"], "reason": "modified", "old_hash": r["file_hash"], "new_hash": current_hash})

    if not stale:
        return "All indexed files are up to date."
    return json.dumps(stale, indent=2)


def handle_memory_list(args: McpToolArgs) -> str:
    item_type = args["type"]
    with get_connection() as conn:
        if item_type == "mistakes":
            rows = conn.execute(
                "SELECT id, date, mistake, fix FROM mistakes ORDER BY date DESC"
            ).fetchall()
            lines = [f"Mistakes ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "mistake", r["id"])
                lines.append(f"  #{r['id']} [{r['date']}] {r['mistake'][:80]}")
                lines.append(f"    Fix: {r['fix'][:100]}")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

        elif item_type == "patterns":
            rows = conn.execute(
                "SELECT id, name, symptoms, standard_fix FROM patterns ORDER BY name"
            ).fetchall()
            lines = [f"Patterns ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "pattern", r["id"])
                lines.append(f"  {r['name']}")
                lines.append(f"    Symptoms: {r['symptoms'][:100]}")
                lines.append(f"    Fix: {r['standard_fix'][:100]}")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

        elif item_type == "skills":
            rows = conn.execute(
                "SELECT id, name, domain, trigger_desc FROM skills ORDER BY name"
            ).fetchall()
            lines = [f"Skills ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "skill", r["id"])
                lines.append(f"  {r['name']} [{r['domain']}]")
                lines.append(f"    When: {r['trigger_desc'][:100]}")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

        elif item_type == "conversations":
            rows = conn.execute(
                "SELECT id, conversation_id, title, date, domain FROM conversations ORDER BY date DESC"
            ).fetchall()
            lines = [f"Conversations ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "conversation", r["id"])
                lines.append(f"  [{r['date']}] {r['title']}")
                lines.append(f"    Domain: {r['domain']} | ID: {r['conversation_id'][:12]}...")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

        elif item_type == "prompts":
            rows = conn.execute(
                "SELECT id, name, role, domain, best_for FROM prompts ORDER BY name"
            ).fetchall()
            lines = [f"Prompts ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "prompt", r["id"])
                lines.append(f"  {r['name']} [{r['domain']}]")
                lines.append(f"    Role: {r['role'][:100]}")
                if r["best_for"]:
                    lines.append(f"    Best for: {r['best_for'][:100]}")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

    return f"Unknown type: {item_type}"


def handle_memory_stats(args: McpToolArgs) -> str:
    stats = get_stats()
    lines = [
        "Engram Memory Stats:",
        f"  Mistakes:      {stats['mistakes']}",
        f"  Patterns:      {stats['patterns']}",
        f"  Skills:        {stats['skills']}",
        f"  Conversations: {stats['conversations']}",
        f"  Prompts:       {stats.get('prompts', 0)}",
        f"  Tags:          {stats['tags']}",
        f"  FTS indexed:   {stats['fts_indexed']}",
    ]
    emb = stats.get("embeddings", {})
    if emb:
        total = emb.get("total", 0)
        model = emb.get("model", "unknown")
        lines.append(f"\n  Embedding Status (model: {model}):")
        if total > 0:
            def pct(n):
                return f"{100 * n / total:.1f}%"
            lines.append(f"    Ready:   {emb.get('ready', 0):4d} ({pct(emb.get('ready', 0))})")
            if emb.get("stale"):
                lines.append(f"    Stale:   {emb['stale']:4d} ({pct(emb['stale'])})  <- run `engram reembed`")
            if emb.get("pending"):
                lines.append(f"    Pending: {emb['pending']:4d} ({pct(emb['pending'])})")
            if emb.get("failed"):
                lines.append(f"    Failed:  {emb['failed']:4d} ({pct(emb['failed'])})")
        else:
            lines.append("    No embeddings tracked yet.")

    lines.append(f"\n  DB path: {get_db_path()}")

    return "\n".join(lines)


def handle_memory_session_review(args: McpToolArgs) -> str:
    """Return a structured reflection prompt for end-of-session learning."""
    return build_session_review_prompt(
        conversation_id=args.get("conversation_id", "unknown"),
        project_path=args.get("project_path"),
        tasks_completed=args.get("tasks_completed", ""),
        bugs_fixed=args.get("bugs_fixed", ""),
        new_patterns_noticed=args.get("new_patterns_noticed", ""),
        workflows_used=args.get("workflows_used", ""),
    )


def handle_memory_check_workflow_state(args: McpToolArgs) -> str:
    session_id = args.get("session_id")
    if not session_id:
        return "Error: session_id is required."
    state = get_session_state(session_id)
    if not state["current_phase"]:
        return f"No workflow state found for session '{session_id}'. Call memory_init_session first."
    lines = [
        f"Session: {session_id}",
        f"Current phase: {state['current_phase']}",
        f"Required roles: {', '.join(state['required_roles']) or 'none'}",
        f"Completed roles: {', '.join(state['completed_roles']) or 'none'}",
        f"Can proceed: {'Yes' if state['can_proceed'] else 'No'}",
    ]
    if state["missing_roles"]:
        lines.append(f"⚠ Missing roles: {', '.join(state['missing_roles'])}")
    return "\n".join(lines)


def handle_memory_advance_phase(args: McpToolArgs) -> str:
    session_id = args.get("session_id")
    if not session_id:
        return "Error: session_id is required."
    try:
        state = advance_phase(session_id)
        return (
            f"Advanced to phase '{state['current_phase']}'.\n"
            f"Required roles for this phase: {', '.join(state['required_roles']) or 'none'}"
        )
    except WorkflowViolationError as e:
        return f"Workflow violation: {e}"


def handle_memory_find_similar(args: McpToolArgs) -> str:
    content = args.get("content", "")
    item_type = args.get("item_type")
    threshold = float(args.get("threshold", 0.85))
    if not content:
        return "Error: content is required."
    results = find_similar(content, item_type=item_type, threshold=threshold)
    if not results:
        return "No similar entries found above the threshold. Safe to add as new entry."
    lines = [f"Found {len(results)} similar entries (threshold: {threshold}):\n"]
    for r in results:
        lines.append(f"  [{r['item_type'].upper()} ID:{r['item_id']}] {r['title']} (similarity: {r['similarity']})")
        if r.get("snippet"):
            lines.append(f"    {r['snippet'][:120]}...")
    lines.append("\nOptions: skip | add_anyway | replace (delete old + add new) | merge (call memory_merge_entries)")
    return "\n".join(lines)


def handle_memory_merge_entries(args: McpToolArgs) -> str:
    item_type_a = args.get("item_type_a")
    item_id_a = args.get("item_id_a")
    item_type_b = args.get("item_type_b")
    item_id_b = args.get("item_id_b")

    if not all([item_type_a, item_id_a, item_type_b, item_id_b]):
        return "Error: item_type_a, item_id_a, item_type_b, item_id_b are all required."

    if not merge_available():
        return "Error: Ollama is not available. Cannot perform LLM-assisted merge."

    entry_a = get_item(item_type_a, item_id_a)
    entry_b = get_item(item_type_b, item_id_b)

    if not entry_a:
        return f"Error: {item_type_a} ID {item_id_a} not found."
    if not entry_b:
        return f"Error: {item_type_b} ID {item_id_b} not found."

    merged = merge_entries(entry_a, entry_b)
    if not merged:
        return "Merge failed: LLM did not return valid JSON. Try again or merge manually."

    return (
        f"Merged entry draft (present to user for approval before storing):\n\n"
        f"```json\n{json.dumps(merged, indent=2)}\n```\n\n"
        f"After approval:\n"
        f"1. Use the appropriate memory_add_* tool to store the merged entry\n"
        f"2. Delete old entries: memory_read_item was called — you can now delete IDs "
        f"{item_id_a} and {item_id_b} from the core tables via consolidate tool"
    )


def handle_memory_embedding_status(args: McpToolArgs) -> str:
    stats = get_embedding_stats()
    total = stats.get("total", 0)
    model = stats.get("model", "unknown")
    lines = [f"Embedding Status (model: {model}):"]
    if total > 0:
        def pct(n): return f"{100*n/total:.1f}%"
        lines.append(f"  Ready:   {stats['ready']:4d} ({pct(stats['ready'])})")
        if stats["stale"]:
            lines.append(f"  Stale:   {stats['stale']:4d} ({pct(stats['stale'])})  ← run engram reembed")
        if stats["pending"]:
            lines.append(f"  Pending: {stats['pending']:4d} ({pct(stats['pending'])})")
        if stats["failed"]:
            lines.append(f"  Failed:  {stats['failed']:4d} ({pct(stats['failed'])})")
    else:
        lines.append("  No embeddings tracked yet.")
    return "\n".join(lines)


def handle_memory_health(args: McpToolArgs) -> str:
    report = run_health_check()
    lines = ["Memory Health Report\n"]
    for itype, stats in report.get("items", {}).items():
        if stats["total"] > 0:
            lines.append(f"  {itype}: {stats['total']} total, {stats['unused_180_plus_days']} GC candidates")
    emb = report.get("embeddings", {})
    if emb:
        lines.append(f"\nEmbeddings ({emb.get('model','?')}): ready={emb.get('ready',0)}, stale={emb.get('stale',0)}, pending={emb.get('pending',0)}")
    lines.append(f"\nFTS: {report.get('fts_total',0)}, Vec: {report.get('vec_total',0)}, Drift: {report.get('vec_drift',0)}")
    lines.append(f"Orphaned tags: {report.get('orphaned_tags',0)}, GC candidates: {report.get('gc_candidates',0)}")
    recs = report.get("recommendations", [])
    if recs:
        lines.append("\nRecommendations:")
        for rec in recs:
            lines.append(f"  • {rec}")
    return "\n".join(lines)


def handle_memory_suggest_consolidations(args: McpToolArgs) -> str:
    threshold = float(args.get("threshold", 0.80))
    item_type = args.get("item_type")
    force_rescan = bool(args.get("force_rescan", False))
    clusters, skip_reason = find_consolidation_candidates(
        threshold=threshold,
        item_types=[item_type] if item_type else None,
        force_rescan=force_rescan,
    )
    if skip_reason == "unchanged":
        return (
            "No changes since last consolidation scan (fingerprint unchanged). "
            "Set force_rescan=true to rescan."
        )
    if not clusters:
        return f"No consolidation candidates found at similarity threshold {threshold}."
    lines = [f"Found {len(clusters)} consolidation candidate(s) (threshold: {threshold}):\n"]
    for i, cluster in enumerate(clusters[:20], 1):
        lines.append(
            f"Cluster {i} — {cluster['item_type']} "
            f"(avg similarity: {cluster['avg_similarity']}, size: {cluster['cluster_size']})"
        )
        for item in cluster["items"]:
            lines.append(f"  ID:{item['item_id']}  {item['title']}")
    return "\n".join(lines)


def _confirm_destructive(description: str) -> str | None:
    """Gate a destructive operation behind MCP elicitation when the client supports it.

    Returns an abort message if the user declined, or None to proceed. Clients
    without elicitation get the pre-gate behavior (proceed).
    """
    from .protocol import elicit_confirmation

    if elicit_confirmation(description) is False:
        return "Cancelled by user — no changes were made."
    return None


def handle_memory_gc(args: McpToolArgs) -> str:
    mode = args.get("mode", "dry-run")
    days = int(args.get("days_unused", 180))
    if mode != "dry-run":
        aborted = _confirm_destructive(
            f"Engram GC is about to {mode} memory items unused for {days}+ days. Proceed?"
        )
        if aborted:
            return aborted
    result = run_gc(mode=mode, days_unused=days)
    if result.get("blocked"):
        return f"GC blocked: {result.get('reason', 'safety guardrail triggered')}"
    candidates = result["candidates"]
    if not candidates:
        return f"No GC candidates found (threshold: unused for {days}+ days)."
    lines = [f"GC {mode} — {len(candidates)} candidate(s) unused for {days}+ days:\n"]
    for c in candidates[:30]:
        lines.append(f"  [{c['item_type'].upper()} ID:{c['item_id']}] created: {c['created_at'] or 'unknown'}")
    if mode == "dry-run":
        lines.append("\nCall with mode='archive' to soft-delete these items.")
    else:
        lines.append(f"\nArchived {result['processed']} items.")
    return "\n".join(lines)


def handle_memory_pin(args: McpToolArgs) -> str:
    item_type = args.get("item_type")
    item_id = args.get("item_id")
    if not item_type or not item_id:
        return "Error: item_type and item_id are required."
    if pin_item(item_type, int(item_id)):
        return f"Pinned {item_type} ID {item_id}. It will always appear at the top of memory_search results."
    return f"Error: could not pin {item_type} ID {item_id} (item not found)."


def handle_memory_unpin(args: McpToolArgs) -> str:
    item_type = args.get("item_type")
    item_id = args.get("item_id")
    if not item_type or not item_id:
        return "Error: item_type and item_id are required."
    if unpin_item(item_type, int(item_id)):
        return f"Unpinned {item_type} ID {item_id}."
    return f"Error: {item_type} ID {item_id} was not pinned."


def handle_memory_list_pinned(args: McpToolArgs) -> str:
    item_type = args.get("item_type")
    limit = int(args.get("limit", 20))
    pinned = get_pinned_items(item_type=item_type, limit=limit)
    if not pinned:
        return "No pinned memories."
    return format_and_truncate_results(pinned)


def handle_memory_auto_extract(args: McpToolArgs) -> str:
    from src.auto_extract import (
        extract_from_messages,
        extract_from_task,
        format_auto_extract_result,
    )

    messages_raw = args.get("messages")
    if messages_raw:
        if isinstance(messages_raw, str):
            try:
                messages = json.loads(messages_raw)
            except json.JSONDecodeError:
                return "Error: messages must be valid JSON array."
        else:
            messages = messages_raw
        if not isinstance(messages, list):
            return "Error: messages must be a JSON array of {role, content} objects."
        result = extract_from_messages(messages)
        return format_auto_extract_result(result)

    task_description = args.get("task_description", "")
    outcome = args.get("outcome", "")
    if not task_description or not outcome:
        return "Error: provide messages (JSON) or both task_description and outcome."

    combined = extract_from_task(
        task_description=task_description,
        outcome=outcome,
        errors_encountered=args.get("errors_encountered", ""),
        files_changed=args.get("files_changed") or [],
    )
    lines = [format_auto_extract_result(combined["auto_extract"]), "", "Engineering capture suggestion:"]
    from src.capture import format_capture_suggestion
    lines.append(format_capture_suggestion(combined["capture_suggestion"]))
    return "\n".join(lines)


def handle_memory_export_skill(args: McpToolArgs) -> str:
    from src.export import (
        export_skills,
        render_pattern_as_skill_md,
        slugify,
        write_skill_file,
    )

    skill_id = args.get("skill_id")
    pattern_id = args.get("pattern_id")
    project_skill = args.get("project_skill", False)
    output_path = args.get("output_path")

    if output_path:
        output_dir = os.path.expanduser(output_path)
    elif project_skill:
        output_dir = os.path.join(os.getcwd(), ".cursor", "skills")
    else:
        output_dir = os.path.expanduser("~/.cursor/skills")

    if skill_id:
        results = export_skills(output_dir=output_dir, ids=[skill_id], dry_run=False)
        if not results:
            return f"Skill ID {skill_id} not found."
        r = results[0]
        if r["action"] == "created":
            return f"Skill '{r['name']}' exported to:\n  {r['path']}\n\nThe Cursor agent will discover this skill automatically on the next session."
        elif r["action"] == "skipped":
            return f"Skill '{r['name']}' already exists at:\n  {r['path']}\n\nNo changes made."
        return f"Export result: {r['action']} — {r['path']}"

    elif pattern_id:
        with get_connection() as conn:
            pattern = conn.execute(
                """SELECT p.id, p.name, p.symptoms, p.root_cause, p.standard_fix,
                          COUNT(po.id) as occurrences
                   FROM patterns p
                   LEFT JOIN pattern_occurrences po ON po.pattern_id = p.id
                   WHERE p.id = ?
                   GROUP BY p.id""",
                (pattern_id,)
            ).fetchone()

        if not pattern:
            return f"Pattern ID {pattern_id} not found."

        pattern = dict(pattern)
        from src.database import get_tags_for_item
        with get_connection() as conn:
            tags = get_tags_for_item(conn, "pattern", pattern["id"])

        from src.export import render_pattern_as_skill_md, slugify, write_skill_file
        content = render_pattern_as_skill_md(pattern, tags)
        slug = slugify(pattern["name"])
        path = write_skill_file(output_dir, slug, content)
        return f"Pattern '{pattern['name']}' exported as skill to:\n  {path}"

    return "Error: provide either skill_id or pattern_id."


def handle_memory_sync_skills(args: McpToolArgs) -> str:
    from src.export import compute_sync_diff, export_skills, import_cursor_skills_dir

    skills_dir = os.path.expanduser(args.get("skills_dir") or "~/.cursor/skills")
    dry_run = args.get("dry_run", True)
    auto_sync = args.get("auto_sync", False)

    diff = compute_sync_diff(skills_dir)
    only_engram = diff["only_in_engram"]
    only_cursor = diff["only_in_cursor"]
    in_both = diff["in_both"]

    lines = [
        f"Engram ↔ Cursor Skill Sync — {skills_dir}\n",
        f"  In both:        {len(in_both)}",
        f"  Only in Engram: {len(only_engram)}  (can export)",
        f"  Only in Cursor: {len(only_cursor)}  (can import)",
        "",
    ]

    if only_engram:
        lines.append("Skills in Engram not yet in Cursor (→ export):")
        for _slug, skill in sorted(only_engram.items()):
            lines.append(f"  [SKILL ID:{skill['id']}] {skill['name']} [{skill['domain']}] usage:{skill.get('usage_count', 0)}")
        lines.append("")

    if only_cursor:
        lines.append("Skills in Cursor not yet in Engram (→ import):")
        for slug, path in sorted(only_cursor.items()):
            lines.append(f"  {slug}  ({path})")
        lines.append("")

    if auto_sync and not dry_run:
        # Export Engram-only skills
        if only_engram:
            engram_ids = [s["id"] for s in only_engram.values()]
            export_results = export_skills(output_dir=skills_dir, ids=engram_ids, dry_run=False)
            created = [r for r in export_results if r["action"] == "created"]
            lines.append(f"Exported {len(created)} skill(s) to Cursor:")
            for r in created:
                lines.append(f"  ✓ {r['name']} → {r['path']}")
            lines.append("")

        # Import Cursor-only skills
        if only_cursor:
            import_results = import_cursor_skills_dir(skills_dir, dry_run=False)
            imported = [r for r in import_results if r.get("action") == "imported"]
            lines.append(f"Imported {len(imported)} skill(s) into Engram:")
            for r in imported:
                lines.append(f"  ✓ {r['name']} (Skill #{r['id']})")
    elif dry_run:
        lines.append("Dry-run mode: no changes made. Set dry_run=false and auto_sync=true to sync.")

    return "\n".join(lines)


def handle_memory_suggest_capture(args: McpToolArgs) -> str:
    from src.capture import format_capture_suggestion, suggest_capture

    task_description = args.get("task_description", "")
    outcome = args.get("outcome", "")
    errors_encountered = args.get("errors_encountered", "")
    files_changed = args.get("files_changed") or []

    if not task_description or not outcome:
        return "Error: task_description and outcome are required."

    suggestion = suggest_capture(
        task_description=task_description,
        outcome=outcome,
        errors_encountered=errors_encountered,
        files_changed=files_changed,
    )
    return format_capture_suggestion(suggestion)


def handle_memory_llm_status(_args: McpToolArgs) -> str:
    from src.llm import get_llm_status

    status = get_llm_status()
    return json.dumps(status, indent=2)


def handle_memory_invalidate(args: McpToolArgs) -> str:
    item_type = args.get("item_type")
    item_id = args.get("item_id")
    if not item_type or item_id is None:
        return "Error: item_type and item_id are required."
    ok = invalidate_memory(
        item_type,
        int(item_id),
        superseded_by=args.get("superseded_by"),
        reason=args.get("reason"),
    )
    if not ok:
        return f"Error: could not invalidate {item_type} ID {item_id}."
    return f"Invalidated {item_type} ID {item_id}."


def handle_memory_sleep(args: McpToolArgs) -> str:
    dry_run = bool(args.get("dry_run", False))
    if not dry_run:
        aborted = _confirm_destructive(
            "Engram sleep-time consolidation will supersede near-duplicate memories "
            "and archive stale ones. Proceed?"
        )
        if aborted:
            return aborted
    summary = run_sleep(
        threshold=float(args.get("threshold", 0.85)),
        days_unused=int(args.get("days_unused", 30)),
        dry_run=dry_run,
    )
    return json.dumps(summary, indent=2)


TOOL_HANDLERS: dict[str, Callable[[McpToolArgs], str]] = {
    "memory_add": handle_memory_add,
    "memory_record_usage": handle_memory_record_usage,
    "memory_read_item": handle_memory_read_item,
    "memory_propose_decision": handle_memory_propose_decision,
    "memory_route": handle_memory_route,
    "memory_search": handle_memory_search,
    "memory_recent": handle_memory_recent,
    "memory_add_mistake": handle_memory_add_mistake,
    "memory_add_pattern": handle_memory_add_pattern,
    "memory_add_skill": handle_memory_add_skill,
    "memory_consolidate_skills": handle_memory_consolidate_skills,
    "memory_add_conversation": handle_memory_add_conversation,
    "memory_add_prompt": handle_memory_add_prompt,
    "memory_list": handle_memory_list,
    "memory_stats": handle_memory_stats,
    "memory_session_review": handle_memory_session_review,
    "memory_init_session": handle_memory_init_session,
    "memory_add_transcript": handle_memory_add_transcript,
    "memory_add_decision": handle_memory_add_decision,
    "memory_get_role": handle_memory_get_role,
    "memory_get_session": handle_memory_get_session,
    "memory_index_file": handle_memory_index_file,
    "memory_query_codebase": handle_memory_query_codebase,
    "memory_get_stale_files": handle_memory_get_stale_files,
    "memory_check_workflow_state": handle_memory_check_workflow_state,
    "memory_advance_phase": handle_memory_advance_phase,
    "memory_find_similar": handle_memory_find_similar,
    "memory_merge_entries": handle_memory_merge_entries,
    "memory_embedding_status": handle_memory_embedding_status,
    "memory_health": handle_memory_health,
    "memory_suggest_consolidations": handle_memory_suggest_consolidations,
    "memory_gc": handle_memory_gc,
    "memory_export_skill": handle_memory_export_skill,
    "memory_sync_skills": handle_memory_sync_skills,
    "memory_suggest_capture": handle_memory_suggest_capture,
    "memory_pin": handle_memory_pin,
    "memory_unpin": handle_memory_unpin,
    "memory_list_pinned": handle_memory_list_pinned,
    "memory_auto_extract": handle_memory_auto_extract,
    "memory_llm_status": handle_memory_llm_status,
    "memory_invalidate": handle_memory_invalidate,
    "memory_sleep": handle_memory_sleep,
}
