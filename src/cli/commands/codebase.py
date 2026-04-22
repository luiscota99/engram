"""Codebase commands: index-project, query-codebase, clean-codebase, graph."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

from ...database import get_connection, get_or_create_project
from ...graph import (
    format_dot,
    format_json,
    format_mermaid,
    index_file_relationships,
    query_relationships,
)
from ..fmt import fmt_bold, fmt_dim, fmt_header


def _get_git_changed_files(project_path: str) -> set[str] | None:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        changed = set(result.stdout.strip().splitlines())
        changed.update(untracked.stdout.strip().splitlines())
        return changed
    except Exception:
        return None


def _calculate_hash(file_path: str) -> str:
    sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception as e:
        return f"error:{e}"


def cmd_index_project(args):
    from ...compression import compress_caveman
    from ...summarize import ollama_available, summarize_file

    project_path = args.path or os.getcwd()
    project = get_or_create_project(project_path)
    project_id = project["id"]

    use_llm = getattr(args, "llm_summarize", False)
    if use_llm and not ollama_available():
        print(fmt_dim("  ⚠ Ollama not available — LLM summarization disabled."))
        use_llm = False

    if args.file:
        files = [args.file]
    else:
        files = []
        exclude_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".engram", "dist", "build"}
        supported_ext = (".py", ".js", ".ts", ".go", ".rs", ".c", ".cpp", ".h", ".md", ".json", ".sql")
        for root, dirs, filenames in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for f in filenames:
                if f.endswith(supported_ext):
                    rel_path = os.path.relpath(os.path.join(root, f), project_path)
                    files.append(rel_path)

    stale_files = []
    with get_connection() as conn:
        for rel_path in files:
            abs_path = os.path.join(project_path, rel_path)
            if not os.path.exists(abs_path):
                continue

            current_mtime = os.path.getmtime(abs_path)
            existing = conn.execute(
                "SELECT file_hash, file_mtime, summary, exports, dependencies FROM codebase_knowledge WHERE project_id = ? AND file_path = ?",
                (project_id, rel_path),
            ).fetchone()

            if existing and existing["file_mtime"] and abs(existing["file_mtime"] - current_mtime) < 0.01 and not args.force:
                if args.verbose:
                    print(f"  - {rel_path} (unchanged, mtime match)")
                continue

            current_hash = _calculate_hash(abs_path)
            if existing and existing["file_hash"] == current_hash and not args.force:
                conn.execute(
                    "UPDATE codebase_knowledge SET file_mtime = ? WHERE project_id = ? AND file_path = ?",
                    (current_mtime, project_id, rel_path),
                )
                if args.verbose:
                    print(f"  - {rel_path} (unchanged, hash match)")
                continue

            if hasattr(args, "check") and args.check:
                stale_files.append({
                    "file_path": rel_path,
                    "old_hash": existing["file_hash"] if existing else None,
                    "new_hash": current_hash,
                })
                continue

            summary = getattr(args, "summary", None)
            llm_exports = llm_deps = None
            if not summary and use_llm:
                print(fmt_dim(f"  ✦ Summarizing {rel_path}..."))
                result = summarize_file(abs_path, project_root=project_path)
                if result:
                    summary, llm_exports, llm_deps = result["summary"], result["exports"], result["dependencies"]
            if not summary:
                if existing and existing["summary"] and not existing["summary"].startswith("Knowledge entry for"):
                    summary = existing["summary"]
                else:
                    summary = "Knowledge entry for " + rel_path

            exports = getattr(args, "exports", None) or llm_exports or (existing["exports"] if existing else None)
            deps = getattr(args, "deps", None) or llm_deps or (existing["dependencies"] if existing else None)

            if hasattr(args, "caveman") and args.caveman:
                if summary and not summary.startswith("Knowledge entry for"):
                    summary = compress_caveman(summary, level=args.caveman_level or "full")

            conn.execute(
                """INSERT INTO codebase_knowledge (project_id, file_path, file_hash, file_mtime, summary, exports, dependencies)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, file_path) DO UPDATE SET
                   file_hash = excluded.file_hash, file_mtime = excluded.file_mtime,
                   summary = excluded.summary, exports = excluded.exports,
                   dependencies = excluded.dependencies, last_indexed_at = datetime('now')""",
                (project_id, rel_path, current_hash, current_mtime, summary, exports, deps),
            )
            if not args.verbose:
                print(f"✓ Indexed {rel_path}")

    if hasattr(args, "check") and args.check:
        print(json.dumps(stale_files, indent=2))


def cmd_query_codebase(args):
    project_path = args.path or os.getcwd()
    project = get_or_create_project(project_path)
    project_id = project["id"]
    query = " ".join(args.query) if args.query else ""

    with get_connection() as conn:
        if query:
            rows = conn.execute(
                """SELECT file_path, summary, exports, dependencies FROM codebase_knowledge
                   WHERE project_id = ? AND (file_path LIKE ? OR summary LIKE ?) ORDER BY file_path""",
                (project_id, f"%{query}%", f"%{query}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT file_path, summary, exports, dependencies FROM codebase_knowledge WHERE project_id = ? ORDER BY file_path",
                (project_id,),
            ).fetchall()

    if not rows:
        print("No codebase knowledge found for this project matching your query.")
        return

    print(fmt_header(f"Codebase Knowledge for {project['name']} ({len(rows)} files):\n"))
    for r in rows:
        from ...compression import compress_caveman
        summary = r["summary"]
        if hasattr(args, "caveman") and args.caveman:
            summary = compress_caveman(summary, level=args.caveman_level or "full")
        print(f"  {fmt_bold(r['file_path'])}")
        print(f"    Summary: {summary}")
        if r["exports"]:
            print(f"    Exports: {fmt_dim(r['exports'])}")
        if r["dependencies"]:
            print(f"    Deps:    {fmt_dim(r['dependencies'])}")
        print()


def cmd_clean_codebase(args):
    project_path = args.path or os.getcwd()
    project = get_or_create_project(project_path)
    project_id = project["id"]
    removed = 0
    with get_connection() as conn:
        rows = conn.execute("SELECT file_path FROM codebase_knowledge WHERE project_id = ?", (project_id,)).fetchall()
        for r in rows:
            if not os.path.exists(os.path.join(project_path, r["file_path"])):
                conn.execute(
                    "DELETE FROM codebase_knowledge WHERE project_id = ? AND file_path = ?",
                    (project_id, r["file_path"]),
                )
                removed += 1
                print(f"  - Cleaned stale entry: {r['file_path']}")
    if removed:
        print(f"\n✓ Removed {removed} stale entries from codebase knowledge.")
    else:
        print("✓ Codebase knowledge is already clean.")


def cmd_graph(args):
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
