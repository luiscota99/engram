#!/usr/bin/env python3
from __future__ import annotations

"""
Engram — persistent memory for AI-assisted development.

Usage:
    engram search "query"             Search all memory (lexical + semantic)
    engram search "query" -t mistake  Search specific type (mistake, pattern, skill, etc.)
    engram search --tags python,api   Filter search by comma-separated tags
    engram recent                     Show the 10 most recent memory entries
    engram recent -n 5 -t skill       Show recent skills only

    engram add [type] ...             Log a new memory entry (mistake, pattern, skill, etc.)
    engram list [type]                List all entries of a specific type

    engram bootstrap                  Auto-setup agent rules for the current project
    engram doctor [--repair]          Run diagnostics and fix database/index issues
    engram stats                      Show memory statistics and database health
    engram init                       Initialize a fresh memory database
    engram seed                       Seed with professional engineering patterns
    engram backup [--git]             Backup memory to JSON and optionally sync to Git

    engram index-project [--path P]   Index file summaries for a project (Codebase Knowledge)
    engram query-codebase "query"     Search project-specific file summaries
    engram clean-codebase             Remove stale entries from the codebase index
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

# Allow running as `python -m src.cli` or directly
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "src"

from .backup import run_backup
from .benchmark import run_benchmark
from .compression import compress_caveman
from .database import (
    delete_item,
    get_connection,
    get_db_path,
    get_embedding_stats,
    get_or_create_project,
    get_session_details,
    index_in_fts,
    init_db,
    link_tags,
    mark_embeddings_stale,
    reembed_stale,
)
from .doctor import run_diagnostics
from .graph import format_dot, format_json, format_mermaid, index_file_relationships, query_relationships
from .maintenance import find_consolidation_candidates, run_gc, run_health_check
from .search import get_recent, get_stats, search, semantic_search
from .seed import seed_database
from .token_simulation import run_simulation
from .workflow import (
    WorkflowViolationError,
    advance_phase,
    get_session_state,
    init_session_state,
)


def calculate_hash(file_path):
    """Calculate SHA-256 hash of a file."""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception as e:
        return f"error:{e}"

# ── Formatting helpers ──────────────────────────────────────────────


def fmt_header(text):
    return f"\033[1;36m{text}\033[0m"


def fmt_type(t):
    colors = {"mistake": "31", "pattern": "33", "skill": "32", "conversation": "34"}
    code = colors.get(t, "37")
    return f"\033[1;{code}m[{t.upper()}]\033[0m"


def fmt_dim(text):
    return f"\033[2m{text}\033[0m"


def fmt_bold(text):
    return f"\033[1m{text}\033[0m"


# ── Commands ────────────────────────────────────────────────────────


def cmd_search(args):
    """Search the memory database."""
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

    if kind == "mistake":
        _add_mistake(args)
    elif kind == "pattern":
        _add_pattern(args)
    elif kind == "skill":
        _add_skill(args)
    elif kind == "conversation":
        _add_conversation(args)
    elif kind == "prompt":
        _add_prompt(args)
    elif kind == "session":
        _add_session(args)
    elif kind == "transcript":
        _add_transcript(args)
    elif kind == "decision":
        cmd_add_decision(args)
    else:
        print(f"Unknown type: {kind}")
        sys.exit(1)


def _add_mistake(args):
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO mistakes (date, context, mistake, root_cause, fix, prevention, conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                args.date,
                args.context,
                args.mistake,
                args.root_cause,
                args.fix,
                args.prevention,
                args.conversation,
            ),
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
            """INSERT INTO patterns (name, symptoms, root_cause, standard_fix)
               VALUES (?, ?, ?, ?)""",
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
            (
                args.name,
                args.domain,
                args.trigger,
                args.workflow,
                args.pitfalls,
                args.files,
                args.dependencies,
            ),
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
            """INSERT INTO conversations (conversation_id, title, date, domain, tasks_completed, key_decisions, mistakes_summary, skills_extracted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                args.id,
                args.title,
                args.date,
                args.domain,
                args.tasks,
                args.decisions,
                args.mistakes,
                args.skills,
            ),
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
            """INSERT INTO sessions (session_id, title, date, domain, workflow_used)
               VALUES (?, ?, ?, ?, ?)""",
            (args.id, args.title, args.date, args.domain, args.workflow_used),
        )
        sid = cursor.lastrowid
        content = f"{args.title} | {args.workflow_used or ''}"
        index_in_fts(conn, "session", sid, args.id, content, [])
    print(f"✓ Session '{args.id}' initialized.")


def _add_transcript(args):
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO session_transcripts (session_id, role, content)
               VALUES (?, ?, ?)""",
            (args.session_id, args.role, args.content),
        )
    print(f"✓ Transcript entry for '{args.role}' added to session '{args.session_id}'.")


def cmd_add_decision(args):
    with get_connection() as conn:
        conn.execute(
            """UPDATE sessions SET key_decisions = IFNULL(key_decisions, '') || char(10) || ?
               WHERE session_id = ?""",
            (args.decision, args.session_id),
        )
    print(f"✓ Decision added to session '{args.session_id}'.")


def _get_git_changed_files(project_path: str) -> set[str] | None:
    """Return set of relative paths changed per git status, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=project_path, capture_output=True, text=True, timeout=5
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_path, capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        changed = set(result.stdout.strip().splitlines())
        changed.update(untracked.stdout.strip().splitlines())
        return changed
    except Exception:
        return None


def cmd_index_project(args):
    """Index or update codebase knowledge for a project."""
    from .summarize import ollama_available, summarize_file

    project_path = args.path or os.getcwd()
    project = get_or_create_project(project_path)
    project_id = project["id"]

    use_llm = getattr(args, "llm_summarize", False)
    if use_llm and not ollama_available():
        print(fmt_dim("  ⚠ Ollama not available — LLM summarization disabled."))
        use_llm = False

    # If a specific file is provided, just index that one
    if args.file:
        files = [args.file]
    else:
        # Use git-based change detection first (faster than full walk + hash)
        git_changed = _get_git_changed_files(project_path)

        files = []
        exclude_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".engram", "dist", "build"}
        supported_ext = (".py", ".js", ".ts", ".go", ".rs", ".c", ".cpp", ".h", ".md", ".json", ".sql")
        for root, dirs, filenames in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for f in filenames:
                if f.endswith(supported_ext):
                    rel_path = os.path.relpath(os.path.join(root, f), project_path)
                    # If git detected changes, only process changed + new files
                    # (unless --force, in which case process all)
                    if not args.force and git_changed is not None and rel_path not in git_changed:
                        # Still check hash for files not in git diff
                        pass
                    files.append(rel_path)

    stale_files = []
    with get_connection() as conn:
        for rel_path in files:
            abs_path = os.path.join(project_path, rel_path)
            if not os.path.exists(abs_path):
                continue

            current_mtime = os.path.getmtime(abs_path)

            # Check mtime first (fastest), then hash for confirmation
            existing = conn.execute(
                "SELECT file_hash, file_mtime, summary, exports, dependencies FROM codebase_knowledge WHERE project_id = ? AND file_path = ?",
                (project_id, rel_path)
            ).fetchone()

            # Fast path: mtime unchanged → skip
            if existing and existing["file_mtime"] and abs(existing["file_mtime"] - current_mtime) < 0.01 and not args.force:
                if args.verbose:
                    print(f"  - {rel_path} (unchanged, mtime match)")
                continue

            current_hash = calculate_hash(abs_path)

            # Hash unchanged → update mtime and skip
            if existing and existing["file_hash"] == current_hash and not args.force:
                conn.execute(
                    "UPDATE codebase_knowledge SET file_mtime = ? WHERE project_id = ? AND file_path = ?",
                    (current_mtime, project_id, rel_path)
                )
                if args.verbose:
                    print(f"  - {rel_path} (unchanged, hash match)")
                continue

            if hasattr(args, 'check') and args.check:
                stale_files.append({
                    "file_path": rel_path,
                    "old_hash": existing["file_hash"] if existing else None,
                    "new_hash": current_hash,
                    "old_summary": existing["summary"] if existing else None
                })
                continue

            # Determine summary
            summary = getattr(args, "summary", None)
            llm_exports = None
            llm_deps = None

            if not summary and use_llm:
                print(fmt_dim(f"  ✦ Summarizing {rel_path}..."))
                result = summarize_file(abs_path, project_root=project_path)
                if result:
                    summary = result["summary"]
                    llm_exports = result["exports"]
                    llm_deps = result["dependencies"]

            if not summary:
                if existing and existing["summary"] and not existing["summary"].startswith("Knowledge entry for"):
                    summary = existing["summary"]
                else:
                    summary = "Knowledge entry for " + rel_path

            exports = getattr(args, "exports", None) or llm_exports or (existing["exports"] if existing else None)
            deps = getattr(args, "deps", None) or llm_deps or (existing["dependencies"] if existing else None)

            # Apply Caveman compression if requested
            if hasattr(args, 'caveman') and args.caveman:
                if summary and not summary.startswith("Knowledge entry for"):
                    summary = compress_caveman(summary, level=args.caveman_level or "full")

            conn.execute(
                """INSERT INTO codebase_knowledge (project_id, file_path, file_hash, file_mtime, summary, exports, dependencies)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, file_path) DO UPDATE SET
                   file_hash = excluded.file_hash,
                   file_mtime = excluded.file_mtime,
                   summary = excluded.summary,
                   exports = excluded.exports,
                   dependencies = excluded.dependencies,
                   last_indexed_at = datetime('now')""",
                (project_id, rel_path, current_hash, current_mtime, summary, exports, deps)
            )
            if not args.verbose:
                print(f"✓ Indexed {rel_path}")

    if hasattr(args, 'check') and args.check:
        print(json.dumps(stale_files, indent=2))


def cmd_query_codebase(args):
    """Search codebase knowledge for a project."""
    project_path = args.path or os.getcwd()
    project = get_or_create_project(project_path)
    project_id = project["id"]

    query = " ".join(args.query) if args.query else ""

    with get_connection() as conn:
        if query:
            # Simple LIKE search for now, could use FTS if needed
            rows = conn.execute(
                """SELECT file_path, summary, exports, dependencies
                   FROM codebase_knowledge
                   WHERE project_id = ? AND (file_path LIKE ? OR summary LIKE ?)
                   ORDER BY file_path""",
                (project_id, f"%{query}%", f"%{query}%")
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT file_path, summary, exports, dependencies FROM codebase_knowledge WHERE project_id = ? ORDER BY file_path",
                (project_id,)
            ).fetchall()

    if not rows:
        print("No codebase knowledge found for this project matching your query.")
        return

    print(fmt_header(f"Codebase Knowledge for {project['name']} ({len(rows)} files):\n"))
    for r in rows:
        summary = r['summary']
        if hasattr(args, 'caveman') and args.caveman:
            summary = compress_caveman(summary, level=args.caveman_level or "full")

        print(f"  {fmt_bold(r['file_path'])}")
        print(f"    Summary: {summary}")
        if r['exports']:
            print(f"    Exports: {fmt_dim(r['exports'])}")
        if r['dependencies']:
            print(f"    Deps:    {fmt_dim(r['dependencies'])}")
        print()

def cmd_clean_codebase(args):
    """Remove stale codebase knowledge entries (files that no longer exist)."""
    project_path = args.path or os.getcwd()
    project = get_or_create_project(project_path)
    project_id = project["id"]

    removed = 0
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT file_path FROM codebase_knowledge WHERE project_id = ?", (project_id,)
        ).fetchall()

        for r in rows:
            abs_path = os.path.join(project_path, r["file_path"])
            if not os.path.exists(abs_path):
                conn.execute(
                    "DELETE FROM codebase_knowledge WHERE project_id = ? AND file_path = ?",
                    (project_id, r["file_path"])
                )
                removed += 1
                print(f"  - Cleaned stale entry: {r['file_path']}")

    if removed:
        print(f"\n✓ Removed {removed} stale entries from codebase knowledge.")
    else:
        print("✓ Codebase knowledge is already clean.")

def cmd_gc(args):
    """Run garbage collection on unused memories."""
    mode = args.mode
    result = run_gc(
        mode=mode,
        days_unused=args.days,
        db_path=None,
    )
    candidates = result["candidates"]
    if not candidates:
        print(fmt_header("No GC candidates found."))
        print(fmt_dim(f"  (threshold: unused for {args.days}+ days with zero usage_count)"))
        return

    print(fmt_header(f"GC Candidates ({len(candidates)}):\n"))
    for c in candidates:
        print(f"  {fmt_type(c['item_type'])} ID:{c['item_id']}  created:{c['created_at'] or 'unknown'}")

    if mode == "dry-run":
        print(fmt_dim(f"\nDry-run complete. Run with --archive or --delete to act."))
    else:
        print(f"\n{fmt_bold('✓')} {mode.capitalize()}d {result['processed']} of {len(candidates)} items.")


def cmd_suggest_consolidate(args):
    """Suggest near-duplicate memory clusters that could be consolidated."""
    clusters = find_consolidation_candidates(
        threshold=args.threshold,
        item_types=[args.type] if args.type else None,
    )
    if not clusters:
        print(fmt_dim("No consolidation candidates found at this similarity threshold."))
        return

    print(fmt_header(f"Consolidation Candidates (similarity ≥ {args.threshold}):\n"))
    for i, cluster in enumerate(clusters[:args.limit], 1):
        print(f"  Cluster {i} — {fmt_type(cluster['item_type'])} (similarity: {cluster['similarity']})")
        for item in cluster["items"]:
            print(f"    ID:{item['item_id']}  {item['title']}")
        print()
    print(fmt_dim(f"Tip: Use `engram consolidate --delete-ids ID1,ID2 ...` to merge manually."))


def cmd_health(args):
    """Show a comprehensive health report for the memory database."""
    report = run_health_check()

    print(fmt_header("Engram Health Report\n"))

    # Item stats
    print(fmt_bold("Memory Items:"))
    for itype, stats in report["items"].items():
        total = stats["total"]
        if total == 0:
            continue
        gc = stats["unused_180_plus_days"]
        print(f"  {fmt_type(itype):30s} total:{total:4d}  "
              f"new(30d):{stats['added_last_30_days']:3d}  "
              f"gc-candidates:{gc:3d}")
    print()

    # Embedding status
    emb = report["embeddings"]
    total_emb = emb.get("total", 0)
    print(fmt_bold(f"Embeddings (model: {emb.get('model', 'unknown')}):"))
    if total_emb > 0:
        def pct(n):
            return f"{100*n/total_emb:.1f}%"
        print(f"  Ready:   {emb['ready']:4d} ({pct(emb['ready'])})")
        if emb["stale"] > 0:
            print(f"  Stale:   {emb['stale']:4d} ({pct(emb['stale'])})  ← regeneration needed")
        if emb["pending"] > 0:
            print(f"  Pending: {emb['pending']:4d} ({pct(emb['pending'])})")
        if emb["failed"] > 0:
            print(f"  Failed:  {emb['failed']:4d} ({pct(emb['failed'])})")
    else:
        print(fmt_dim("  No embeddings tracked yet."))
    print()

    # Index health
    print(fmt_bold("Index Health:"))
    print(f"  FTS entries:        {report['fts_total']}")
    print(f"  Vector entries:     {report['vec_total']}")
    drift = report["vec_drift"]
    if drift > 0:
        print(f"  {fmt_dim(f'⚠ Vector drift:     {drift} FTS entries missing vectors')}")
    else:
        print(fmt_dim("  ✓ No vector drift"))
    print(f"  Orphaned tags:      {report['orphaned_tags']}")
    print(f"  GC candidates:      {report['gc_candidates']}")
    print(f"  Archived memories:  {report['archived_memories']}")
    print()

    # Recommendations
    if report["recommendations"]:
        print(fmt_bold("Recommendations:"))
        for rec in report["recommendations"]:
            print(f"  • {rec}")
    else:
        print(fmt_dim("✓ No issues detected."))


def cmd_graph(args):
    """Build and display a file dependency graph for a project."""
    project_path = args.path or os.getcwd()
    output_format = args.format or "mermaid"

    if not args.no_index:
        print(fmt_dim("  Indexing file relationships..."))
        result = index_file_relationships(project_path)
        print(fmt_dim(f"  ✓ {result['files_processed']} files processed, {result['added']} relationships found."))

    file_filter = getattr(args, "file", None)
    direction = getattr(args, "direction", "both")
    relationships = query_relationships(project_path, file_path=file_filter, direction=direction)

    if not relationships:
        print(fmt_dim("No relationships found. Ensure files have been indexed."))
        return

    if output_format == "mermaid":
        output = "```mermaid\n" + format_mermaid(relationships) + "\n```"
    elif output_format == "dot":
        output = format_dot(relationships)
    else:
        output = format_json(relationships)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"✓ Graph written to {args.output}")
    else:
        print(output)


def cmd_reembed(args):
    """Re-generate embeddings for stale/pending items."""
    from .database import get_embedding_stats

    stats_before = get_embedding_stats()
    stale = stats_before.get("stale", 0)
    pending = stats_before.get("pending", 0)
    total_pending = stale + pending

    if total_pending == 0:
        print(fmt_dim("✓ All embeddings are up to date."))
        return

    print(fmt_header(f"Re-embedding {total_pending} items (stale: {stale}, pending: {pending})...\n"))

    batch = args.batch_size or 50
    total_done = 0
    while True:
        result = reembed_stale(batch_size=batch)
        total_done += result["succeeded"]
        if result["failed"]:
            print(fmt_dim(f"  ⚠ {result['failed']} failed this batch"))
        remaining = result["remaining"]
        print(f"  ✓ {total_done} done, {remaining} remaining...")
        if remaining == 0 or result["processed"] == 0:
            break

    print(f"\n{fmt_bold('✓')} Re-embedding complete. {total_done} items updated.")


def cmd_migrate(args):
    """Database migration utilities."""
    from .database import DB_PATH
    from .migrations import backup_before_migration, downgrade_to

    if args.rollback:
        # Find most recent backup and restore it
        backup_dir = os.path.join(os.path.dirname(DB_PATH), "backups")
        if not os.path.exists(backup_dir):
            print("No backups found.")
            return
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith("pre-migration")],
            reverse=True,
        )
        if not backups:
            print("No migration backups found.")
            return
        latest = os.path.join(backup_dir, backups[0])
        import shutil
        shutil.copy2(latest, DB_PATH)
        print(f"✓ Rolled back to {latest}")
        return

    if args.mark_stale:
        count = mark_embeddings_stale()
        print(f"✓ Marked {count} embeddings as stale (model changed).")
        print("  Run `engram reembed` to regenerate.")
        return

    print("Use --rollback to restore from backup, or --mark-stale after changing embedding model.")


def _batch_tags(conn, item_type, ids):
    """Fetch tags for a batch of item IDs in a single query.
    Returns a dict of {item_id: [tag, ...]} for O(1) lookup in display loops.
    """
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
            rows = conn.execute(
                "SELECT id, date, mistake, fix FROM mistakes ORDER BY date DESC"
            ).fetchall()
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
            rows = conn.execute(
                "SELECT id, name, symptoms, standard_fix FROM patterns ORDER BY name"
            ).fetchall()
            tags_map = _batch_tags(conn, "pattern", [r["id"] for r in rows])
            # Batch occurrence counts in one query
            occ_rows = conn.execute(
                "SELECT pattern_id, COUNT(*) as c FROM pattern_occurrences GROUP BY pattern_id"
            ).fetchall()
            occ_map = {r["pattern_id"]: r["c"] for r in occ_rows}
            print(fmt_header(f"Patterns ({len(rows)}):\n"))
            for r in rows:
                tags = tags_map.get(r["id"], [])
                occ = occ_map.get(r["id"], 0)
                print(
                    f"  {fmt_type('pattern')} {fmt_bold(r['name'])} ({occ} occurrence{'s' if occ != 1 else ''})"
                )
                print(f"    Symptoms: {fmt_dim(r['symptoms'][:100])}")
                print(f"    Fix: {fmt_dim(r['standard_fix'][:100])}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        elif kind == "skills":
            rows = conn.execute(
                "SELECT id, name, domain, trigger_desc FROM skills ORDER BY name"
            ).fetchall()
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
            rows = conn.execute(
                "SELECT id, conversation_id, title, date, domain FROM conversations ORDER BY date DESC"
            ).fetchall()
            tags_map = _batch_tags(conn, "conversation", [r["id"] for r in rows])
            print(fmt_header(f"Conversations ({len(rows)}):\n"))
            for r in rows:
                tags = tags_map.get(r["id"], [])
                print(f"  {fmt_type('conversation')} [{r['date']}] {fmt_bold(r['title'])}")
                print(
                    f"    Domain: {r['domain']} | ID: {fmt_dim(r['conversation_id'][:12] + '...')}"
                )
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        elif kind == "sessions":
            rows = conn.execute(
                "SELECT id, session_id, title, date, domain, workflow_used FROM sessions ORDER BY date DESC"
            ).fetchall()
            print(fmt_header(f"Sessions ({len(rows)}):\n"))
            for r in rows:
                print(f"  {fmt_type('session')} [{r['date']}] {fmt_bold(r['title'])}")
                print(f"    Domain: {r['domain']} | ID: {fmt_dim(r['session_id'][:12] + '...')} | Workflow: {r['workflow_used']}")
                print()

        elif kind == "prompts":
            rows = conn.execute(
                "SELECT id, name, role, domain, best_for FROM prompts ORDER BY name"
            ).fetchall()
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
            print(f"Unknown type: {kind}. Use: mistakes, patterns, skills, conversations, prompts")
            sys.exit(1)



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


def cmd_get_role(args):
    with get_connection() as conn:
        row = conn.execute("SELECT charter, heuristics FROM roles WHERE name = ?", (args.name,)).fetchone()
        if not row:
            print(f"Role '{args.name}' not found.")
            return
        print(fmt_header(f"Role: {args.name}\n"))
        print(fmt_bold("Charter:"))
        print(row["charter"])
        print("\n" + fmt_bold("Heuristics:"))
        print(row["heuristics"])


def cmd_stats(args):
    stats = get_stats()
    print(fmt_header("Engram Stats\n"))
    print(f"  Mistakes:      {stats['mistakes']}")
    print(f"  Patterns:      {stats['patterns']}")
    print(f"  Skills:        {stats['skills']}")
    print(f"  Conversations: {stats['conversations']}")
    print(f"  Prompts:       {stats['prompts']}")
    print(f"  Tags:          {stats['tags']}")
    print(f"  FTS indexed:   {stats['fts_indexed']}")

    emb = stats.get("embeddings", {})
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


def cmd_doctor(args):
    """Run database diagnostics and optionally repair issues."""
    run_diagnostics(repair=args.repair)


def cmd_backup(args):
    """Export database to JSON and optionally sync to Git."""
    run_backup(git_sync=args.git)


def cmd_consolidate(args):
    ids = [int(i.strip()) for i in args.delete_ids.split(",") if i.strip()]
    if not ids:
        print("Error: --delete-ids requires at least one ID.")
        sys.exit(1)

    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                args.name,
                args.domain,
                args.trigger,
                args.workflow,
                args.pitfalls,
                args.key_files,
                args.deps,
            ),
        )
        sid = cursor.lastrowid
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        link_tags(conn, "skill", sid, tags)

        content = f"{args.trigger} | {args.workflow} | {args.pitfalls or ''}"
        index_in_fts(conn, "skill", sid, args.name, content, tags)

        for old_id in ids:
            delete_item(conn, "skill", old_id)

    print(f"✓ Consolidated {len(ids)} skills into new Master Skill #{sid}.")


def cmd_init(args):
    init_db()
    print(f"✓ Database initialized at {get_db_path()}")


def cmd_bootstrap(args):
    """Bootstrap agent rules for the current project."""
    import shutil
    project_root = os.getcwd()
    engram_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 0. Ensure Engram is initialized
    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(fmt_header("Engram database not found. Initializing..."))
        init_db()
        print(f"✓ Created database at {db_path}")

    # 1. Cursor
    cursor_rules_dir = os.path.join(project_root, ".cursor", "rules")
    os.makedirs(cursor_rules_dir, exist_ok=True)
    source_cursor = os.path.join(engram_root, "cursor-rules", "engram-committee.mdc")
    if os.path.exists(source_cursor):
        shutil.copy2(source_cursor, os.path.join(cursor_rules_dir, "engram.mdc"))
        print(f"✓ Created {os.path.join('.cursor', 'rules', 'engram.mdc')}")
    else:
        print(fmt_dim(f"Warning: Source rule {source_cursor} not found."))

    # 2. Antigravity
    antigravity_dir = os.path.join(project_root, ".antigravity")
    os.makedirs(antigravity_dir, exist_ok=True)
    ag_instructions = os.path.join(antigravity_dir, "instructions.md")
    source_ag = os.path.join(engram_root, "antigravity-skills", "engram-committee-workflow.md")

    with open(ag_instructions, "w") as f:
        f.write("# 🧠 Engram Project Instructions\n\n")
        f.write("You are operating in a project backed by the **Engram Persistent Memory System**.\n")
        f.write("You MUST follow the Engram Committee-Driven Workflow for all complex tasks, architectural analysis, and codebase reviews.\n\n")
        if os.path.exists(source_ag):
            with open(source_ag, "r") as src:
                f.write("## Engram Committee-Driven Workflow\n")
                f.write(src.read())
        print(f"✓ Created {os.path.join('.antigravity', 'instructions.md')}")

    # 3. Codebase Knowledge Indexing Suggestion
    print(fmt_header("\nProject successfully bootstrapped for AI Agents!"))
    print("Cursor and Antigravity will now default to the Committee Workflow.")
    print(f"\n{fmt_bold('Next Step:')} Run `{fmt_bold('engram index-project')}` to create a persistent map of this codebase.")


def cmd_seed(args):
    seed_database()


def cmd_benchmark(args):
    """Run the LLM benchmarking suite."""
    run_benchmark()


def cmd_simulate(args):
    """Run token usage simulation."""
    run_simulation(mock=args.mock)


def _add_prompt(args):
    # Read prompt text from file if --file is provided
    prompt_text = args.prompt_text or ""
    if args.file:
        with open(args.file, "r") as f:
            prompt_text = f.read()

    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO prompts (name, role, domain, description, prompt_text, source_path, best_for)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                args.name,
                args.role,
                args.domain,
                args.description,
                prompt_text,
                args.file,
                args.best_for,
            ),
        )
        pid = cursor.lastrowid
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
        link_tags(conn, "prompt", pid, tags)
        content = f"{args.role} | {args.description} | {args.best_for or ''} | {prompt_text[:500]}"
        index_in_fts(conn, "prompt", pid, args.name, content, tags)
    print(f"✓ Prompt #{pid} '{args.name}' stored.")


def cmd_get_session(args):
    """Fetch session details including transcripts and decisions."""
    session = get_session_details(args.id)
    if not session:
        print(f"Session '{args.id}' not found.")
        sys.exit(1)

    print(fmt_header(f"Session: {session['title']} ({session['session_id']})\n"))
    print(f"Date:   {session['date']}")
    print(f"Domain: {session['domain']}")
    if session.get('workflow_used'):
        print(f"Workflow: {session['workflow_used']}")
    print("")

    if session.get('key_decisions'):
        print(fmt_bold("Key Decisions:"))
        print(session['key_decisions'])
        print("")

    if session.get('transcripts'):
        print(fmt_bold("Transcripts:"))
        for t in session['transcripts']:
            print(f"  {fmt_type('transcript')} [{t['role']}] {t['timestamp']}")
            print(f"    {fmt_dim(t['content'])}")
            print()


def cmd_run(args):
    """Run a prompt using Claw-Code and log to Engram."""
    prompt_text = " ".join(args.prompt)
    claw_path = args.claw_path or os.environ.get("CLAW_PATH")

    # Try to find claw in common locations if not provided
    if not claw_path:
        claw_path = shutil.which("claw")

    if not claw_path:
        # Check standard dev location relative to AI root
        # /Users/luismiguel/Desktop/AI/engram/src/cli.py -> /Users/luismiguel/Desktop/AI
        ai_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        dev_path = os.path.join(ai_root, "claw-code", "rust", "target", "release", "claw")
        if os.path.exists(dev_path):
            claw_path = dev_path
        else:
            dev_path_debug = os.path.join(ai_root, "claw-code", "rust", "target", "debug", "claw")
            if os.path.exists(dev_path_debug):
                claw_path = dev_path_debug

    if not claw_path:
        print(fmt_header("Error: Claw-Code binary ('claw') not found."))
        print("Please build claw-code (cargo build --release) or set CLAW_PATH environment variable.")
        sys.exit(1)

    # 1. Fetch Role context if provided
    context_prefix = ""
    if args.role:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT charter, heuristics FROM roles WHERE name = ?", (args.role,)
            ).fetchone()
            if row:
                context_prefix = f"Role: {args.role}\nCharter: {row['charter']}\nHeuristics: {row['heuristics']}\n\n"

    full_prompt = context_prefix + prompt_text

    # 2. Build claw command
    # Use 'prompt' subcommand of claw
    cmd = [claw_path, "prompt", full_prompt]
    if args.model:
        # claw supports --model flag before subcommand or after?
        # usually it's claw --model sonnet prompt "..."
        cmd = [claw_path, "--model", args.model, "prompt", full_prompt]

    print(fmt_header(f"Executing via Claw ({claw_path})...\n"))

    # 3. Execute
    try:
        # We use a subprocess and pipe output to capture it while still showing it to the user
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )

        output_lines = []
        for line in process.stdout:
            print(line, end="")
            output_lines.append(line)

        process.wait()
        full_output = "".join(output_lines)

        # 4. Log to Engram if session_id is provided
        if args.session_id:
            with get_connection() as conn:
                conn.execute(
                    "INSERT INTO session_transcripts (session_id, role, content) VALUES (?, ?, ?)",
                    (args.session_id, args.role or "Claw", full_output),
                )
            print(f"\n✓ Output logged to Engram session '{args.session_id}'.")

    except Exception as e:
        print(f"\nError executing claw: {e}")
        sys.exit(1)


def cmd_suggest(args):
    """Suggest a prompt or skill based on task description."""
    query = " ".join(args.query) if args.query else ""

    # Try semantic search first if query is long enough
    results = []
    is_semantic = False
    if len(query.split()) > 2:
        sem_results = semantic_search(query, limit=args.limit)
        # Filter for prompts or requested type
        item_type = getattr(args, "type", "prompt")
        results = [r for r in sem_results if r["item_type"] == item_type]
        if results:
            is_semantic = True

    # Fallback to FTS5 lexical search
    if not results:
        results = search(query, item_type=getattr(args, "type", "prompt"), limit=args.limit)

    if not results:
        print(fmt_dim(f"No matching {getattr(args, 'type', 'prompt')}s found."))
        return

    search_type = "Semantic" if is_semantic else "Lexical"
    print(
        fmt_header(f"Suggested {getattr(args, 'type', 'prompt')}s ({search_type}) for: {query}\n")
    )
    for r in results:
        print(f"  {fmt_type('prompt')} {fmt_bold(r['title'])}")
        if r["snippet"]:
            snippet = r["snippet"].replace("\n", " ")[:150]
            print(f"    {fmt_dim(snippet)}")
        if r["tags"]:
            print(f"    {fmt_dim('tags: ' + r['tags'])}")
        print()


def cmd_import_skills(args):
    """Import skills from orchestrator SKILL.md files."""
    import glob
    import re

    skills_path = args.path
    if not os.path.isdir(skills_path):
        print(f"Directory not found: {skills_path}")
        sys.exit(1)

    skill_dirs = sorted(glob.glob(os.path.join(skills_path, "*/SKILL.md")))
    if not skill_dirs:
        print(f"No SKILL.md files found in {skills_path}")
        sys.exit(1)

    imported = 0
    skipped = 0

    with get_connection() as conn:
        for skill_file in skill_dirs:
            with open(skill_file, "r") as f:
                content = f.read()

            # Parse YAML frontmatter
            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
            if not fm_match:
                skipped += 1
                continue

            frontmatter = fm_match.group(1)
            body = fm_match.group(2).strip()

            # Extract name and description from frontmatter
            name_match = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
            desc_match = re.search(r"description:\s*>-?\s*\n((?:\s+.+\n?)*)", frontmatter)
            if not desc_match:
                desc_match = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)

            name = (
                name_match.group(1).strip()
                if name_match
                else os.path.basename(os.path.dirname(skill_file))
            )
            description = ""
            if desc_match:
                description = " ".join(
                    line.strip() for line in desc_match.group(1).strip().split("\n")
                )

            # Extract "When to Use" section as trigger
            trigger = ""
            when_match = re.search(r"## When to Use\s*\n((?:- .+\n?)*)", body)
            if when_match:
                trigger = when_match.group(1).strip()
            else:
                trigger = description

            # Classify domain from name
            domain = "engineering"
            if any(
                kw in name
                for kw in ["react", "frontend", "ui", "web-design", "web-accessibility", "vercel"]
            ):
                domain = "frontend"
            elif any(kw in name for kw in ["backend", "nodejs", "api", "database", "auth"]):
                domain = "backend"
            elif any(kw in name for kw in ["security", "review-and-secure"]):
                domain = "security"
            elif any(kw in name for kw in ["test", "tdd", "webapp-testing"]):
                domain = "testing"
            elif any(kw in name for kw in ["debug", "error", "incident", "post-mortem"]):
                domain = "debugging"
            elif any(kw in name for kw in ["git", "ship", "branch", "sdlc", "phase"]):
                domain = "process"
            elif any(
                kw in name
                for kw in ["brainstorm", "requirements", "prd", "spec", "project-bootstrap"]
            ):
                domain = "planning"

            # Check if already exists
            existing = conn.execute("SELECT id FROM skills WHERE name = ?", (name,)).fetchone()
            if existing:
                skipped += 1
                continue

            cursor = conn.execute(
                """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    domain,
                    trigger[:500],
                    body[:3000],
                    None,
                    json.dumps([skill_file]),
                    "ks-cursor-orchestrator",
                ),
            )
            sid = cursor.lastrowid
            tags = [domain, "orchestrator", "cursor-skill"]
            link_tags(conn, "skill", sid, tags)
            index_content = f"{trigger} | {description} | {body[:500]}"
            index_in_fts(conn, "skill", sid, name, index_content, tags)
            imported += 1

    print(f"✓ Imported {imported} skills, skipped {skipped} (already exist or no frontmatter).")


# ── Argument parser ─────────────────────────────────────────────────


def build_parser():
    parser = argparse.ArgumentParser(
        prog="engram",
        description="Engram — persistent memory for AI-assisted development",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # search
    p_search = sub.add_parser("search", help="Search all memory")
    p_search.add_argument("query", nargs="*", help="Search query")
    p_search.add_argument(
        "-t", "--type", choices=["mistake", "pattern", "skill", "conversation", "prompt"]
    )
    p_search.add_argument("--tags", help="Comma-separated tags")
    p_search.add_argument("-n", "--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    # recent
    p_recent = sub.add_parser("recent", help="Show recent entries")
    p_recent.add_argument("-n", type=int, default=10)
    p_recent.add_argument(
        "-t", "--type", choices=["mistake", "pattern", "skill", "conversation", "prompt"]
    )
    p_recent.set_defaults(func=cmd_recent)

    # add
    p_add = sub.add_parser("add", help="Add a new entry")
    add_sub = p_add.add_subparsers(dest="kind")

    # add mistake
    p_am = add_sub.add_parser("mistake", help="Log a mistake")
    p_am.add_argument("--date", required=True)
    p_am.add_argument("--context", required=True)
    p_am.add_argument("--mistake", required=True)
    p_am.add_argument("--root-cause")
    p_am.add_argument("--fix", required=True)
    p_am.add_argument("--prevention")
    p_am.add_argument("--conversation")
    p_am.add_argument("--tags")

    # add pattern
    p_ap = add_sub.add_parser("pattern", help="Log a pattern")
    p_ap.add_argument("--name", required=True)
    p_ap.add_argument("--symptoms", required=True)
    p_ap.add_argument("--root-cause", required=True)
    p_ap.add_argument("--fix", required=True)
    p_ap.add_argument("--tags")

    # add skill
    p_as = add_sub.add_parser("skill", help="Log a skill")
    p_as.add_argument("--name", required=True)
    p_as.add_argument("--domain", required=True)
    p_as.add_argument("--trigger", required=True)
    p_as.add_argument("--workflow", required=True)
    p_as.add_argument("--pitfalls")
    p_as.add_argument("--files")
    p_as.add_argument("--dependencies")
    p_as.add_argument("--tags")

    # add conversation
    p_ac = add_sub.add_parser("conversation", help="Log a conversation")
    p_ac.add_argument("--id", required=True)
    p_ac.add_argument("--title", required=True)
    p_ac.add_argument("--date", required=True)
    p_ac.add_argument("--domain", required=True)
    p_ac.add_argument("--tasks")
    p_ac.add_argument("--decisions")
    p_ac.add_argument("--mistakes")
    p_ac.add_argument("--skills")
    p_ac.add_argument("--tags")

    # add session
    p_asess = add_sub.add_parser("session", help="Initialize a session")
    p_asess.add_argument("--id", required=True)
    p_asess.add_argument("--title", required=True)
    p_asess.add_argument("--date", required=True)
    p_asess.add_argument("--domain", required=True)
    p_asess.add_argument("--workflow-used")

    # add transcript
    p_at = add_sub.add_parser("transcript", help="Add transcript entry")
    p_at.add_argument("--session-id", required=True)
    p_at.add_argument("--role", required=True)
    p_at.add_argument("--content", required=True)

    # add decision
    p_ad = add_sub.add_parser("decision", help="Add decision to session")
    p_ad.add_argument("--session-id", required=True)
    p_ad.add_argument("--decision", required=True)

    p_add.set_defaults(func=cmd_add)

    # index-project
    p_idx = sub.add_parser("index-project", help="Index project codebase knowledge")
    p_idx.add_argument("--path", help="Project path (default: current)")
    p_idx.add_argument("--file", help="Specific file to index")
    p_idx.add_argument("--summary", help="Summary of the file")
    p_idx.add_argument("--exports", help="Exported symbols (JSON string)")
    p_idx.add_argument("--deps", help="Dependencies (JSON string)")
    p_idx.add_argument("--force", action="store_true", help="Force re-indexing even if hash matches")
    p_idx.add_argument("--check", action="store_true", help="Just check for stale files and return JSON")
    p_idx.add_argument("--caveman", action="store_true", help="Compress summaries using Caveman protocol")
    p_idx.add_argument("--caveman-level", choices=["lite", "full", "ultra"], default="full")
    p_idx.add_argument("--llm-summarize", action="store_true",
                       help="Use Ollama to auto-generate file summaries (requires Ollama running)")
    p_idx.add_argument("--verbose", action="store_true")
    p_idx.set_defaults(func=cmd_index_project)

    # query-codebase
    p_qc = sub.add_parser("query-codebase", help="Query indexed codebase knowledge")
    p_qc.add_argument("query", nargs="*", help="Query string")
    p_qc.add_argument("--path", help="Project path")
    p_qc.add_argument("--caveman", action="store_true", help="Show results in Caveman format")
    p_qc.add_argument("--caveman-level", choices=["lite", "full", "ultra"], default="full")
    p_qc.set_defaults(func=cmd_query_codebase)

    # clean-codebase
    p_clean = sub.add_parser("clean-codebase", help="Remove stale entries from codebase knowledge")
    p_clean.add_argument("--path", help="Project path")
    p_clean.set_defaults(func=cmd_clean_codebase)

    # add prompt
    p_apr = add_sub.add_parser("prompt", help="Store an LLM prompt")
    p_apr.add_argument("--name", required=True)
    p_apr.add_argument("--role", required=True, help="What role/persona the prompt creates")
    p_apr.add_argument("--domain", required=True)
    p_apr.add_argument("--description", required=True)
    p_apr.add_argument("--prompt-text", help="Prompt text (or use --file)")
    p_apr.add_argument("--file", help="Path to prompt file")
    p_apr.add_argument("--best-for", help="What this prompt is best for")
    p_apr.add_argument("--tags")

    # list
    p_list = sub.add_parser("list", help="List entries by type")
    p_list.add_argument(
        "kind", choices=["mistakes", "patterns", "skills", "conversations", "prompts", "sessions"]
    )
    p_list.set_defaults(func=cmd_list)

    # suggest
    p_suggest = sub.add_parser("suggest", help="Suggest a prompt or skill for a task")
    p_suggest.add_argument("query", nargs="*", help="Task description")
    p_suggest.add_argument("-t", "--type", choices=["prompt", "skill", "mistake"], default="prompt")
    p_suggest.add_argument("-n", "--limit", type=int, default=3)
    p_suggest.set_defaults(func=cmd_suggest)

    # get-session
    p_session = sub.add_parser("get-session", help="Get full details of a session")
    p_session.add_argument("--id", required=True, help="Session ID to fetch")
    p_session.set_defaults(func=cmd_get_session)

    # get-role
    p_role = sub.add_parser("get-role", help="Get a subagent role profile")
    p_role.add_argument("name", help="Name of the role")
    p_role.set_defaults(func=cmd_get_role)

    # import-skills
    p_import = sub.add_parser(
        "import-skills", help="Import skills from orchestrator SKILL.md files"
    )
    p_import.add_argument("path", help="Path to skills directory")
    p_import.set_defaults(func=cmd_import_skills)

    # link-pattern
    p_link = sub.add_parser("link-pattern", help="Link pattern to a conversation")
    p_link.add_argument("name", help="Pattern name")
    p_link.add_argument("--conversation", required=True)
    p_link.add_argument("--date")
    p_link.add_argument("--notes")
    p_link.set_defaults(func=cmd_link_pattern)

    # stats
    p_stats = sub.add_parser("stats", help="Show database statistics")
    p_stats.set_defaults(func=cmd_stats)

    # doctor
    p_doctor = sub.add_parser("doctor", help="Run database diagnostics and repair")
    p_doctor.add_argument(
        "--repair", action="store_true", help="Attempt to auto-repair found issues"
    )
    p_doctor.set_defaults(func=cmd_doctor)

    # backup
    p_backup = sub.add_parser("backup", help="Export database to JSON format")
    p_backup.add_argument(
        "--git",
        action="store_true",
        help="Automatically commit and push backup to Git if configured",
    )
    p_backup.set_defaults(func=cmd_backup)

    # consolidate
    p_cons = sub.add_parser("consolidate", help="Consolidate multiple skills into one")
    p_cons.add_argument(
        "--delete-ids", required=True, help="Comma-separated IDs of old skills to delete"
    )
    p_cons.add_argument("--name", required=True)
    p_cons.add_argument("--domain", required=True)
    p_cons.add_argument("--trigger", required=True)
    p_cons.add_argument("--workflow", required=True)
    p_cons.add_argument("--pitfalls")
    p_cons.add_argument("--key-files")
    p_cons.add_argument("--deps")
    p_cons.add_argument("--tags")
    p_cons.set_defaults(func=cmd_consolidate)

    # gc
    p_gc = sub.add_parser("gc", help="Garbage collect unused memories")
    p_gc.add_argument("--mode", choices=["dry-run", "archive", "delete"], default="dry-run",
                      help="dry-run (default), archive (soft-delete), or delete (permanent)")
    p_gc.add_argument("--days", type=int, default=180,
                      help="Age threshold in days (default: 180)")
    p_gc.set_defaults(func=cmd_gc)

    # suggest-consolidate
    p_sc = sub.add_parser("suggest-consolidate", help="Find near-duplicate memories to consolidate")
    p_sc.add_argument("--threshold", type=float, default=0.80,
                      help="Similarity threshold 0-1 (default: 0.80)")
    p_sc.add_argument("--type", choices=["mistake", "pattern", "skill"],
                      help="Limit to a specific memory type")
    p_sc.add_argument("-n", "--limit", type=int, default=20,
                      help="Max clusters to show (default: 20)")
    p_sc.set_defaults(func=cmd_suggest_consolidate)

    # health
    p_health = sub.add_parser("health", help="Show a health report for the memory database")
    p_health.set_defaults(func=cmd_health)

    # graph
    p_graph = sub.add_parser("graph", help="Build and visualize file dependency graph")
    p_graph.add_argument("--path", help="Project path (default: current directory)")
    p_graph.add_argument("--file", help="Show relationships for a specific file only")
    p_graph.add_argument("--direction", choices=["outgoing", "incoming", "both"], default="both")
    p_graph.add_argument("--format", choices=["mermaid", "dot", "json"], default="mermaid")
    p_graph.add_argument("--output", help="Write output to a file instead of stdout")
    p_graph.add_argument("--no-index", action="store_true",
                         help="Skip re-indexing, use existing relationship data")
    p_graph.set_defaults(func=cmd_graph)

    # reembed
    p_reembed = sub.add_parser("reembed", help="Re-generate embeddings for stale/pending items")
    p_reembed.add_argument("--batch-size", type=int, default=50,
                           help="Items per batch (default: 50)")
    p_reembed.set_defaults(func=cmd_reembed)

    # migrate
    p_migrate = sub.add_parser("migrate", help="Database migration utilities")
    p_migrate.add_argument("--rollback", action="store_true",
                           help="Restore database from most recent pre-migration backup")
    p_migrate.add_argument("--mark-stale", action="store_true",
                           help="Mark all embeddings as stale (use after changing ENGRAM_EMBEDDING_MODEL)")
    p_migrate.set_defaults(func=cmd_migrate)

    # init
    p_init = sub.add_parser("init", help="Initialize the database")
    p_init.set_defaults(func=cmd_init)

    # bootstrap
    p_bootstrap = sub.add_parser("bootstrap", help="Bootstrap agent rules for the current project")
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    # seed
    p_seed = sub.add_parser("seed", help="Seed with historical data")
    p_seed.set_defaults(func=cmd_seed)

    # benchmark
    p_benchmark = sub.add_parser("benchmark", help="Run LLM benchmark suite")
    p_benchmark.set_defaults(func=cmd_benchmark)

    # simulate
    p_simulate = sub.add_parser("simulate", help="Simulate token usage of Engram vs Traditional")
    p_simulate.add_argument("--mock", action="store_true", help="Run simulation using estimated token counts (no API key required)")
    p_simulate.set_defaults(func=cmd_simulate)

    # run (Claw-Code Bridge)
    p_run = sub.add_parser("run", help="Run a prompt using Claw-Code execution engine")
    p_run.add_argument("prompt", nargs="+", help="The prompt to execute")
    p_run.add_argument("--role", help="Engram role to use (e.g., Analyst, Facilitator)")
    p_run.add_argument("--model", help="Override model (e.g., opus, sonnet, haiku)")
    p_run.add_argument("--session-id", help="Engram session ID to associate with")
    p_run.add_argument("--claw-path", help="Path to claw binary")
    p_run.set_defaults(func=cmd_run)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Auto-init DB for all commands
    if args.command not in ("init",):
        init_db()

    args.func(args)


if __name__ == "__main__":
    main()
