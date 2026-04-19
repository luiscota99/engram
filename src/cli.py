#!/usr/bin/env python3
"""
Engram — persistent memory for AI-assisted development.

Usage:
    engram search "query"             Search all memory
    engram search "query" -t mistake  Search specific type
    engram search --tags python,api   Search by tags
    engram recent                     Show recent entries
    engram recent -n 5 -t skill       Recent skills

    engram add mistake ...            Log a new mistake
    engram add pattern ...            Log a new pattern
    engram add skill ...              Log a new skill
    engram add conversation ...       Log a conversation

    engram list mistakes              List all mistakes
    engram list patterns              List all patterns
    engram list skills                List all skills
    engram list conversations         List all conversations

    engram link-pattern "name" ...    Link pattern to conversation
    engram stats                      Show database statistics
    engram init                       Initialize database
    engram seed                       Seed with historical data
"""

import argparse
import json
import os
import sys
import textwrap

# Allow running as `python -m src.cli` or directly
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "src"

from .database import get_connection, init_db, link_tags, index_in_fts, get_tags_for_item, get_db_path
from .search import search, get_recent, get_stats
from .seed import seed_database


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
    query = " ".join(args.query) if args.query else ""
    tags = args.tags.split(",") if args.tags else None
    results = search(query, item_type=args.type, tags=tags, limit=args.limit)

    if not results:
        print(fmt_dim("No results found."))
        return

    print(fmt_header(f"Found {len(results)} result(s):\n"))
    for r in results:
        print(f"  {fmt_type(r['item_type'])} {fmt_bold(r['title'])}")
        if r["snippet"]:
            snippet = r["snippet"].replace("\n", " ")[:120]
            print(f"    {fmt_dim(snippet)}")
        if r["tags"]:
            print(f"    {fmt_dim('tags: ' + r['tags'])}")
        print()


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
            """INSERT INTO conversations (conversation_id, title, date, domain, tasks_completed, key_decisions, mistakes_summary, skills_extracted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (args.id, args.title, args.date, args.domain, args.tasks, args.decisions, args.mistakes, args.skills),
        )
        cid = cursor.lastrowid
        tags = args.tags.split(",") if args.tags else []
        link_tags(conn, "conversation", cid, tags)
        content = f"{args.tasks or ''} | {args.decisions or ''} | {args.mistakes or ''}"
        index_in_fts(conn, "conversation", cid, args.title, content, tags)
    print(f"✓ Conversation #{cid} '{args.title}' logged.")


def cmd_list(args):
    kind = args.kind
    with get_connection() as conn:
        if kind == "mistakes":
            rows = conn.execute("SELECT id, date, mistake, fix FROM mistakes ORDER BY date DESC").fetchall()
            print(fmt_header(f"Mistakes ({len(rows)}):\n"))
            for r in rows:
                tags = get_tags_for_item(conn, "mistake", r["id"])
                print(f"  {fmt_type('mistake')} #{r['id']} [{r['date']}] {r['mistake'][:80]}")
                print(f"    Fix: {fmt_dim(r['fix'][:100])}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        elif kind == "patterns":
            rows = conn.execute("SELECT id, name, symptoms, standard_fix FROM patterns ORDER BY name").fetchall()
            print(fmt_header(f"Patterns ({len(rows)}):\n"))
            for r in rows:
                occ = conn.execute("SELECT COUNT(*) as c FROM pattern_occurrences WHERE pattern_id = ?", (r["id"],)).fetchone()["c"]
                tags = get_tags_for_item(conn, "pattern", r["id"])
                print(f"  {fmt_type('pattern')} {fmt_bold(r['name'])} ({occ} occurrence{'s' if occ != 1 else ''})")
                print(f"    Symptoms: {fmt_dim(r['symptoms'][:100])}")
                print(f"    Fix: {fmt_dim(r['standard_fix'][:100])}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        elif kind == "skills":
            rows = conn.execute("SELECT id, name, domain, trigger_desc FROM skills ORDER BY name").fetchall()
            print(fmt_header(f"Skills ({len(rows)}):\n"))
            for r in rows:
                tags = get_tags_for_item(conn, "skill", r["id"])
                print(f"  {fmt_type('skill')} {fmt_bold(r['name'])} [{r['domain']}]")
                print(f"    When: {fmt_dim(r['trigger_desc'][:100])}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()

        elif kind == "conversations":
            rows = conn.execute("SELECT id, conversation_id, title, date, domain FROM conversations ORDER BY date DESC").fetchall()
            print(fmt_header(f"Conversations ({len(rows)}):\n"))
            for r in rows:
                tags = get_tags_for_item(conn, "conversation", r["id"])
                print(f"  {fmt_type('conversation')} [{r['date']}] {fmt_bold(r['title'])}")
                print(f"    Domain: {r['domain']} | ID: {fmt_dim(r['conversation_id'][:12] + '...')}")
                if tags:
                    print(f"    {fmt_dim('tags: ' + ', '.join(tags))}")
                print()
        else:
            print(f"Unknown type: {kind}. Use: mistakes, patterns, skills, conversations")
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


def cmd_stats(args):
    stats = get_stats()
    print(fmt_header("Engram Stats\n"))
    print(f"  Mistakes:      {stats['mistakes']}")
    print(f"  Patterns:      {stats['patterns']}")
    print(f"  Skills:        {stats['skills']}")
    print(f"  Conversations: {stats['conversations']}")
    print(f"  Tags:          {stats['tags']}")
    print(f"  FTS indexed:   {stats['fts_indexed']}")
    print(f"\n  DB path: {fmt_dim(get_db_path())}")


def cmd_init(args):
    init_db()
    print(f"✓ Database initialized at {get_db_path()}")


def cmd_seed(args):
    seed_database()


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
    p_search.add_argument("-t", "--type", choices=["mistake", "pattern", "skill", "conversation"])
    p_search.add_argument("--tags", help="Comma-separated tags")
    p_search.add_argument("-n", "--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    # recent
    p_recent = sub.add_parser("recent", help="Show recent entries")
    p_recent.add_argument("-n", type=int, default=10)
    p_recent.add_argument("-t", "--type", choices=["mistake", "pattern", "skill", "conversation"])
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

    p_add.set_defaults(func=cmd_add)

    # list
    p_list = sub.add_parser("list", help="List entries by type")
    p_list.add_argument("kind", choices=["mistakes", "patterns", "skills", "conversations"])
    p_list.set_defaults(func=cmd_list)

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

    # init
    p_init = sub.add_parser("init", help="Initialize the database")
    p_init.set_defaults(func=cmd_init)

    # seed
    p_seed = sub.add_parser("seed", help="Seed with historical data")
    p_seed.set_defaults(func=cmd_seed)

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
