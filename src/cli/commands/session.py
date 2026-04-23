"""Session and workflow commands."""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from datetime import date

from ...database import (
    get_connection,
    get_or_create_project,
    get_session_details,
    index_in_fts,
    init_db,
    link_item_to_project,
    link_tags,
)
from ...session_review import build_session_review_prompt
from ..fmt import fmt_bold, fmt_dim, fmt_header, fmt_type


def cmd_get_session(args):
    session = get_session_details(args.id)
    if not session:
        print(f"Session '{args.id}' not found.")
        sys.exit(1)

    print(fmt_header(f"Session: {session['title']} ({session['session_id']})\n"))
    print(f"Date:   {session['date']}")
    print(f"Domain: {session['domain']}")
    if session.get("workflow_used"):
        print(f"Workflow: {session['workflow_used']}")
    print("")

    if session.get("key_decisions"):
        print(fmt_bold("Key Decisions:"))
        print(session["key_decisions"])
        print("")

    if session.get("transcripts"):
        print(fmt_bold("Transcripts:"))
        for t in session["transcripts"]:
            print(f"  {fmt_type('session')} [{t['role']}] {t['timestamp']}")
            print(f"    {fmt_dim(t['content'])}")
            print()


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


def cmd_session_review(args):
    project_path = None
    if args.no_project:
        project_path = None
    elif args.project is not None:
        project_path = os.path.abspath(os.path.expanduser(args.project))
    else:
        project_path = os.getcwd()
    text = build_session_review_prompt(
        conversation_id=args.conversation_id,
        project_path=project_path,
        tasks_completed=args.tasks,
        bugs_fixed=args.bugs_fixed,
        new_patterns_noticed=args.new_patterns,
        workflows_used=args.workflows_used,
    )
    print(text)


def _parse_session_summary_file(text: str) -> tuple[dict, str]:
    """Parse optional YAML-like front matter between --- lines; return (meta, body)."""
    text = text.lstrip("\ufeff").strip()
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta_raw, body = parts[1].strip(), parts[2].strip()
    meta: dict[str, str] = {}
    for line in meta_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            val = v.strip().strip('"').strip("'")
            if key:
                meta[key] = val
    return meta, body


def _title_from_body(body: str) -> str:
    m = re.match(r"^#\s+(.+)$", body.strip(), re.MULTILINE)
    if m:
        return m.group(1).strip()[:200]
    first = body.strip().splitlines()[0] if body.strip() else "Session summary"
    return first[:200]


def cmd_import_session_summary(args):
    init_db()
    path = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.isfile(path):
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    meta, body = _parse_session_summary_file(raw)
    if not body.strip():
        print("File is empty after parsing; nothing to import.", file=sys.stderr)
        sys.exit(1)

    title = (meta.get("title") or _title_from_body(body)).strip()
    when = (meta.get("date") or date.today().isoformat()).strip()
    domain = (meta.get("domain") or "engineering").strip()
    tags = [t.strip() for t in (meta.get("tags") or "").split(",") if t.strip()]

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    conversation_id = (meta.get("conversation_id") or "").strip() or f"import-ss-v1-{digest[:24]}"

    project_path = None
    if args.project is not None:
        project_path = os.path.abspath(os.path.expanduser(args.project))
    else:
        project_path = os.getcwd()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row and not args.force:
            print(
                f"Skip: conversation_id `{conversation_id}` already exists "
                f"(use --force to insert another copy with a new id, or set conversation_id: in front matter)."
            )
            return
        if row and args.force:
            conversation_id = f"{conversation_id}-r{int(time.time())}"

    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO conversations
               (conversation_id, title, date, domain, tasks_completed, key_decisions, mistakes_summary, skills_extracted)
               VALUES (?, ?, ?, ?, ?, '', '', '')""",
            (conversation_id, title, when, domain, body),
        )
        cid = cur.lastrowid
        link_tags(conn, "conversation", cid, tags)
        content = f"{body[:5000]}"
        index_in_fts(conn, "conversation", cid, title, content, tags)

    proj = get_or_create_project(project_path)
    link_item_to_project("conversation", cid, proj["id"], affinity="created")

    print(f"✓ Imported session summary as conversation #{cid} (`{conversation_id}`).")
    print(f"  Project link: {proj['path']} (affinity: created)")
