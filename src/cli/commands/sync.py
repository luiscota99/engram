"""Skill sync commands: export-skills, import-cursor-skills, sync-skills, import-skills."""
from __future__ import annotations

import json
import os
import re
import sys

from ...database import get_connection, index_in_fts, link_tags
from ...export import compute_sync_diff, export_skills, import_cursor_skills_dir
from ..fmt import fmt_bold, fmt_dim, fmt_header, fmt_type


def cmd_export_skills(args):
    output_dir = args.output
    if args.project_skills:
        output_dir = os.path.join(os.getcwd(), ".cursor", "skills")
    output_dir = os.path.expanduser(output_dir)
    ids = [int(i.strip()) for i in args.ids.split(",") if i.strip()] if args.ids else None
    min_usage = args.min_usage or 0

    results = export_skills(
        output_dir=output_dir,
        ids=ids,
        domain=args.domain,
        min_usage=min_usage,
        from_patterns=args.from_patterns,
        dry_run=args.dry_run,
    )
    if not results:
        print(fmt_dim("No skills matched the given filters."))
        return

    created = [r for r in results if r.get("action") == "created"]
    skipped = [r for r in results if r.get("action") == "skipped"]
    dry_run_items = [r for r in results if r.get("action") == "dry-run"]

    if args.dry_run:
        print(fmt_header(f"Dry-run: {len(dry_run_items)} skill(s) would be exported to {output_dir}\n"))
        for r in dry_run_items:
            usage_info = f"  (usage: {r.get('usage_count', r.get('occurrences', 0))})"
            source_badge = fmt_dim("[pattern]") + " " if r.get("source") == "pattern" else ""
            print(f"  {source_badge}{fmt_bold(r['name'])}{fmt_dim(usage_info)}")
            print(fmt_dim(f"    → {r['path']}"))
    else:
        print(fmt_header(f"Export complete → {output_dir}\n"))
        for r in created:
            usage_info = f"  (usage: {r.get('usage_count', r.get('occurrences', 0))})"
            source_badge = fmt_dim("[pattern] ") if r.get("source") == "pattern" else ""
            print(f"  ✓ {source_badge}{fmt_bold(r['name'])}{fmt_dim(usage_info)}")
            print(fmt_dim(f"    {r['path']}"))
        if skipped:
            print(fmt_dim(f"\n  Skipped {len(skipped)} already-existing skill(s)."))
        if not created:
            print(fmt_dim("  Nothing new to export (all skills already exist on disk)."))


def cmd_import_cursor_skills(args):
    skills_dir = os.path.expanduser(args.path)
    results = import_cursor_skills_dir(skills_dir, dry_run=args.dry_run)
    if not results:
        print(fmt_dim("No SKILL.md files found."))
        return

    if args.dry_run:
        importable = [r for r in results if r.get("action") == "dry-run"]
        already = [r for r in results if r.get("action") == "skipped"]
        errors = [r for r in results if r.get("action") == "error"]
        print(fmt_header(f"Dry-run: {len(importable)} skill(s) would be imported from {skills_dir}\n"))
        for r in importable:
            print(f"  + {fmt_bold(r.get('name', r.get('file', '?')))}")
        if already:
            print(fmt_dim(f"\n  {len(already)} already exist in Engram (would be skipped)."))
        if errors:
            print(fmt_dim(f"\n  {len(errors)} file(s) could not be parsed."))
    else:
        imported = [r for r in results if r.get("action") == "imported"]
        skipped_r = [r for r in results if r.get("action") == "skipped"]
        errors = [r for r in results if r.get("action") == "error"]
        print(fmt_header(f"Import complete from {skills_dir}\n"))
        for r in imported:
            print(f"  ✓ {fmt_bold(r['name'])} (Skill #{r['id']})")
        if skipped_r:
            print(fmt_dim(f"\n  Skipped {len(skipped_r)} skill(s) already in Engram."))
        if errors:
            print(fmt_dim(f"\n  {len(errors)} file(s) failed to parse:"))
            for e in errors:
                print(fmt_dim(f"    {e.get('file', '?')} — {e.get('reason', 'unknown')}"))
        if not imported:
            print(fmt_dim("  Nothing new imported."))


def cmd_sync_skills(args):
    skills_dir = args.path or os.path.expanduser("~/.cursor/skills")
    diff = compute_sync_diff(skills_dir)
    only_engram = diff["only_in_engram"]
    only_cursor = diff["only_in_cursor"]
    in_both = diff["in_both"]

    print(fmt_header(f"Engram ↔ Cursor Skill Sync — {skills_dir}\n"))
    print(f"  {fmt_bold('In both:')}        {len(in_both)}")
    print(f"  {fmt_bold('Only in Engram:')} {len(only_engram)}  (can export)")
    print(f"  {fmt_bold('Only in Cursor:')} {len(only_cursor)}  (can import)")
    print()

    if only_engram:
        print(fmt_bold("Skills in Engram but NOT in Cursor (→ export):"))
        for _slug, skill in sorted(only_engram.items()):
            usage = skill.get("usage_count", 0)
            print(f"  {fmt_type('skill')} {fmt_bold(skill['name'])} [{skill['domain']}]  usage:{usage}")
        print()

    if only_cursor:
        print(fmt_bold("Skills in Cursor but NOT in Engram (→ import):"))
        for slug, path in sorted(only_cursor.items()):
            print(f"  {fmt_type('skill')} {fmt_bold(slug)}  {fmt_dim(path)}")
        print()

    if args.dry_run or not (args.auto or args.export_missing or args.import_missing):
        if not args.dry_run:
            print(fmt_dim("Run with --auto, --export-missing, or --import-missing to sync."))
        return

    if args.auto or args.export_missing:
        if only_engram:
            engram_ids = [s["id"] for s in only_engram.values()]
            export_results = export_skills(output_dir=skills_dir, ids=engram_ids, dry_run=False)
            created = [r for r in export_results if r["action"] == "created"]
            print(fmt_header(f"Exported {len(created)} skill(s) to Cursor.\n"))
            for r in created:
                print(f"  ✓ {r['name']} → {r['path']}")

    if args.auto or args.import_missing:
        if only_cursor:
            import_results = import_cursor_skills_dir(skills_dir, dry_run=False)
            imported = [r for r in import_results if r.get("action") == "imported"]
            print(fmt_header(f"Imported {len(imported)} skill(s) into Engram.\n"))
            for r in imported:
                print(f"  ✓ {r['name']} (Skill #{r['id']})")


def cmd_import_skills(args):
    """Import skills from orchestrator SKILL.md files (legacy format)."""
    import glob as _glob

    skills_path = args.path
    if not os.path.isdir(skills_path):
        print(f"Directory not found: {skills_path}")
        sys.exit(1)

    skill_dirs = sorted(_glob.glob(os.path.join(skills_path, "*/SKILL.md")))
    if not skill_dirs:
        print(f"No SKILL.md files found in {skills_path}")
        sys.exit(1)

    imported = skipped = 0
    with get_connection() as conn:
        for skill_file in skill_dirs:
            with open(skill_file, "r") as f:
                content = f.read()

            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
            if not fm_match:
                skipped += 1
                continue

            frontmatter = fm_match.group(1)
            body = fm_match.group(2).strip()

            name_match = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
            desc_match = re.search(r"description:\s*>-?\s*\n((?:\s+.+\n?)*)", frontmatter)
            if not desc_match:
                desc_match = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)

            name = (name_match.group(1).strip() if name_match else os.path.basename(os.path.dirname(skill_file)))
            description = ""
            if desc_match:
                description = " ".join(line.strip() for line in desc_match.group(1).strip().split("\n"))

            trigger = ""
            when_match = re.search(r"## When to Use\s*\n((?:- .+\n?)*)", body)
            if when_match:
                trigger = when_match.group(1).strip()
            else:
                trigger = description

            domain = "engineering"
            nl = name.lower()
            if any(k in nl for k in ["react", "frontend", "ui", "web-design"]):
                domain = "frontend"
            elif any(k in nl for k in ["backend", "nodejs", "api", "database"]):
                domain = "backend"
            elif any(k in nl for k in ["security"]):
                domain = "security"
            elif any(k in nl for k in ["test", "tdd"]):
                domain = "testing"
            elif any(k in nl for k in ["debug", "error", "incident"]):
                domain = "debugging"

            existing = conn.execute("SELECT id FROM skills WHERE name = ?", (name,)).fetchone()
            if existing:
                skipped += 1
                continue

            cursor = conn.execute(
                """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, domain, trigger[:500], body[:3000], None, json.dumps([skill_file]), "ks-cursor-orchestrator"),
            )
            sid = cursor.lastrowid
            tags = [domain, "orchestrator", "cursor-skill"]
            link_tags(conn, "skill", sid, tags)
            index_content = f"{trigger} | {description} | {body[:500]}"
            index_in_fts(conn, "skill", sid, name, index_content, tags)
            imported += 1

    print(f"✓ Imported {imported} skills, skipped {skipped} (already exist or no frontmatter).")
