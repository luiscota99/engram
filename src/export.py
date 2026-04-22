"""
Engram Export Module — generate Cursor-compatible SKILL.md files from Engram memory.

Supports:
  - Exporting skills by ID, domain, or min usage count
  - Converting high-occurrence patterns to skills
  - Auto-detecting and importing Cursor SKILL.md format
  - Bidirectional sync diffing between Engram and a skills directory
"""
from __future__ import annotations

import hashlib
import os
import re
import json
from typing import Optional

from .database import get_connection, get_tags_for_item, get_or_create_project


# ── Slug helpers ──────────────────────────────────────────────────────


def slugify(name: str) -> str:
    """Convert a skill name to a valid Cursor skill slug (lowercase, hyphens)."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:64]


# ── SKILL.md rendering ────────────────────────────────────────────────


def render_skill_md(skill: dict, tags: list[str] | None = None) -> str:
    """
    Render an Engram skill dict into a Cursor-compatible SKILL.md string.

    The description field is built to maximise Cursor skill discovery:
      - trigger_desc provides the core "when to use" signal
      - domain + tags add additional keywords
    """
    name_slug = slugify(skill["name"])

    # Build description (max 1024 chars for Cursor)
    trigger = (skill.get("trigger_desc") or "").strip()
    domain = (skill.get("domain") or "engineering").strip()
    tag_list = tags or []
    tag_str = ", ".join(tag_list) if tag_list else ""

    description_parts = [trigger]
    if domain and domain not in trigger:
        description_parts.append(f"Domain: {domain}.")
    if tag_str:
        description_parts.append(f"Tags: {tag_str}.")

    description = " ".join(description_parts)[:1024]

    # Build body
    workflow = (skill.get("workflow") or "").strip()
    pitfalls = (skill.get("pitfalls") or "").strip()
    key_files = (skill.get("key_files") or "").strip()
    dependencies = (skill.get("dependencies") or "").strip()

    lines = [
        "---",
        f"name: {name_slug}",
        f"description: >-",
        f"  {description}",
        "---",
        "",
        f"# {skill['name']}",
        "",
    ]

    if workflow:
        lines.append(workflow)
        lines.append("")

    if trigger:
        lines += [
            "## When to Use",
            trigger,
            "",
        ]

    if pitfalls:
        lines += [
            "## Common Pitfalls",
            pitfalls,
            "",
        ]

    if key_files:
        lines += [
            "## Key Files",
            key_files,
            "",
        ]

    if dependencies:
        lines += [
            "## Dependencies",
            dependencies,
            "",
        ]

    lines += [
        "## Engram Metadata",
        f"- Engram ID: {skill.get('id', 'unknown')}",
        f"- Domain: {domain}",
        f"- Usage count: {skill.get('usage_count', 0)}",
    ]
    if tag_list:
        lines.append(f"- Tags: {tag_str}")
    lines.append("")

    return "\n".join(lines)


def render_pattern_as_skill_md(pattern: dict, tags: list[str] | None = None) -> str:
    """
    Convert an Engram pattern into a Cursor SKILL.md.
    Patterns become diagnostic / recognition skills.
    """
    name_slug = slugify(pattern["name"])
    symptoms = (pattern.get("symptoms") or "").strip()
    root_cause = (pattern.get("root_cause") or "").strip()
    fix = (pattern.get("standard_fix") or "").strip()
    tag_list = tags or []
    tag_str = ", ".join(tag_list)

    description = f"Recognise and fix the '{pattern['name']}' pattern. {symptoms}"[:1024]

    lines = [
        "---",
        f"name: {name_slug}",
        f"description: >-",
        f"  {description}",
        "---",
        "",
        f"# {pattern['name']} (Pattern)",
        "",
        "## Symptoms",
        symptoms,
        "",
        "## Root Cause",
        root_cause,
        "",
        "## Standard Fix",
        fix,
        "",
        "## Engram Metadata",
        f"- Engram Pattern ID: {pattern.get('id', 'unknown')}",
        f"- Occurrences: {pattern.get('occurrences', 0)}",
    ]
    if tag_list:
        lines.append(f"- Tags: {tag_str}")
    lines.append("")

    return "\n".join(lines)


# ── File write helpers ────────────────────────────────────────────────


def write_skill_file(output_dir: str, slug: str, content: str) -> str:
    """Write a SKILL.md file to output_dir/slug/SKILL.md. Returns the path."""
    skill_dir = os.path.join(output_dir, slug)
    os.makedirs(skill_dir, exist_ok=True)
    path = os.path.join(skill_dir, "SKILL.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ── Export functions ──────────────────────────────────────────────────


def export_skills(
    output_dir: str,
    ids: list[int] | None = None,
    domain: str | None = None,
    min_usage: int = 0,
    from_patterns: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """
    Export Engram skills (and optionally high-hit patterns) as Cursor SKILL.md files.

    Returns list of dicts with keys: slug, path, action ('created'|'skipped'|'dry-run').
    """
    output_dir = os.path.expanduser(output_dir)
    results = []

    with get_connection() as conn:
        # Build skill query
        clauses = []
        params: list = []

        if ids:
            placeholders = ",".join("?" * len(ids))
            clauses.append(f"id IN ({placeholders})")
            params.extend(ids)
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if min_usage > 0:
            clauses.append("usage_count >= ?")
            params.append(min_usage)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        skills = conn.execute(
            f"SELECT id, name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies, usage_count "
            f"FROM skills {where} ORDER BY usage_count DESC, name",
            params,
        ).fetchall()

        for row in skills:
            skill = dict(row)
            tags = get_tags_for_item(conn, "skill", skill["id"])
            content = render_skill_md(skill, tags)
            slug = slugify(skill["name"])

            path = os.path.join(output_dir, slug, "SKILL.md")
            action = "dry-run" if dry_run else ("skipped" if os.path.exists(path) else "created")

            if not dry_run and action == "created":
                write_skill_file(output_dir, slug, content)

            results.append({
                "slug": slug,
                "name": skill["name"],
                "path": path,
                "action": action,
                "usage_count": skill["usage_count"],
            })

        # Optionally export patterns as skills
        if from_patterns:
            patterns = conn.execute(
                """SELECT p.id, p.name, p.symptoms, p.root_cause, p.standard_fix,
                          COUNT(po.id) as occurrences
                   FROM patterns p
                   LEFT JOIN pattern_occurrences po ON po.pattern_id = p.id
                   GROUP BY p.id
                   HAVING occurrences >= 1
                   ORDER BY occurrences DESC""",
            ).fetchall()

            for row in patterns:
                pattern = dict(row)
                tags = get_tags_for_item(conn, "pattern", pattern["id"])
                content = render_pattern_as_skill_md(pattern, tags)
                slug = slugify(pattern["name"])

                path = os.path.join(output_dir, slug, "SKILL.md")
                action = "dry-run" if dry_run else ("skipped" if os.path.exists(path) else "created")

                if not dry_run and action == "created":
                    write_skill_file(output_dir, slug, content)

                results.append({
                    "slug": slug,
                    "name": pattern["name"],
                    "path": path,
                    "action": action,
                    "source": "pattern",
                    "occurrences": pattern["occurrences"],
                })

    return results


# ── Import helpers ────────────────────────────────────────────────────


def parse_skill_md(file_path: str) -> dict | None:
    """
    Parse a Cursor-style SKILL.md file.

    Returns a dict with keys: name, description, body, raw_frontmatter
    or None if the file cannot be parsed.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None

    # Split frontmatter from body
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not fm_match:
        return None

    frontmatter_str = fm_match.group(1)
    body = fm_match.group(2).strip()

    # Parse name and description from frontmatter
    name_match = re.search(r"^name:\s*(.+)$", frontmatter_str, re.MULTILINE)

    # description can be inline or multiline (>- block)
    desc_block_match = re.search(
        r"^description:\s*>-?\s*\n((?:[ \t]+.+\n?)+)", frontmatter_str, re.MULTILINE
    )
    desc_inline_match = re.search(r"^description:\s*(.+)$", frontmatter_str, re.MULTILINE)

    if not name_match:
        return None

    name = name_match.group(1).strip()

    if desc_block_match:
        description = " ".join(
            line.strip() for line in desc_block_match.group(1).strip().splitlines()
        )
    elif desc_inline_match:
        description = desc_inline_match.group(1).strip()
    else:
        description = ""

    return {
        "name": name,
        "description": description,
        "body": body,
        "raw_frontmatter": frontmatter_str,
        "file_path": file_path,
    }


def classify_domain_from_skill(name: str, description: str) -> str:
    """Heuristically classify a skill into an Engram domain."""
    text = (name + " " + description).lower()
    if any(k in text for k in ["react", "frontend", "ui", "css", "html", "web", "vercel", "tailwind"]):
        return "frontend"
    if any(k in text for k in ["backend", "node", "api", "database", "auth", "server", "sql"]):
        return "backend"
    if any(k in text for k in ["security", "secure", "vulnerability", "auth"]):
        return "security"
    if any(k in text for k in ["test", "tdd", "spec", "coverage", "jest", "pytest"]):
        return "testing"
    if any(k in text for k in ["debug", "error", "incident", "post-mortem", "fix"]):
        return "debugging"
    if any(k in text for k in ["git", "ship", "branch", "deploy", "ci", "cd"]):
        return "process"
    if any(k in text for k in ["brainstorm", "requirements", "prd", "spec", "plan"]):
        return "planning"
    if any(k in text for k in ["memory", "engram", "cursor", "agent", "skill"]):
        return "tooling"
    return "engineering"


def extract_when_to_use(body: str, description: str) -> str:
    """Extract or synthesise the trigger description for a skill."""
    # Look for "## When to Use" section
    when_match = re.search(
        r"##\s+When to Use\s*\n((?:(?!##).)+)", body, re.DOTALL
    )
    if when_match:
        return when_match.group(1).strip()
    return description or ""


def import_cursor_skill(file_path: str, conn, dry_run: bool = False) -> dict:
    """
    Import a single Cursor SKILL.md file into Engram.

    Returns a dict with: name, action ('imported'|'skipped'|'dry-run'|'error').
    """
    from .database import index_in_fts, link_tags

    parsed = parse_skill_md(file_path)
    if not parsed:
        return {"file": file_path, "action": "error", "reason": "parse_failed"}

    name = parsed["name"]
    description = parsed["description"]
    body = parsed["body"]

    # Skip if already exists
    existing = conn.execute("SELECT id FROM skills WHERE name = ?", (name,)).fetchone()
    if existing:
        return {"file": file_path, "name": name, "action": "skipped", "reason": "already_exists"}

    if dry_run:
        return {"file": file_path, "name": name, "action": "dry-run"}

    domain = classify_domain_from_skill(name, description)
    trigger = extract_when_to_use(body, description)

    cursor = conn.execute(
        """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            domain,
            trigger[:500],
            body[:3000],
            None,
            json.dumps([file_path]),
            "cursor-skill",
        ),
    )
    sid = cursor.lastrowid
    tags = [domain, "cursor-skill"]
    link_tags(conn, "skill", sid, tags)
    index_content = f"{trigger} | {description} | {body[:500]}"
    index_in_fts(conn, "skill", sid, name, index_content, tags)

    return {"file": file_path, "name": name, "action": "imported", "id": sid}


def import_cursor_skills_dir(
    skills_dir: str,
    dry_run: bool = False,
) -> list[dict]:
    """
    Recursively find and import all SKILL.md files from a Cursor skills directory.
    """
    from .database import get_connection

    skills_dir = os.path.expanduser(skills_dir)
    if not os.path.isdir(skills_dir):
        return [{"error": f"Directory not found: {skills_dir}"}]

    skill_files = []
    for root, dirs, files in os.walk(skills_dir):
        # Skip Cursor's internal skills-cursor directory
        dirs[:] = [d for d in dirs if d != "skills-cursor"]
        if "SKILL.md" in files:
            skill_files.append(os.path.join(root, "SKILL.md"))

    results = []
    with get_connection() as conn:
        for path in sorted(skill_files):
            result = import_cursor_skill(path, conn, dry_run=dry_run)
            results.append(result)

    return results


# ── Sync diff helpers ─────────────────────────────────────────────────


def compute_sync_diff(skills_dir: str) -> dict:
    """
    Compare Engram skills against a Cursor skills directory.

    Returns a dict with:
      - only_in_engram: skills in Engram not in the directory
      - only_in_cursor: SKILL.md files not yet in Engram
      - in_both: skills present in both
    """
    skills_dir = os.path.expanduser(skills_dir)

    # Collect cursor skills
    cursor_skills: dict[str, str] = {}  # slug -> file_path
    if os.path.isdir(skills_dir):
        for item in os.listdir(skills_dir):
            skill_file = os.path.join(skills_dir, item, "SKILL.md")
            if os.path.isfile(skill_file):
                cursor_skills[item] = skill_file

    # Collect Engram skills
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, domain, usage_count FROM skills ORDER BY name"
        ).fetchall()

    engram_skills = {slugify(dict(r)["name"]): dict(r) for r in rows}

    only_in_engram = {
        slug: skill for slug, skill in engram_skills.items() if slug not in cursor_skills
    }
    only_in_cursor = {
        slug: path for slug, path in cursor_skills.items() if slug not in engram_skills
    }
    in_both = {
        slug: {"engram": engram_skills[slug], "cursor_path": cursor_skills[slug]}
        for slug in engram_skills
        if slug in cursor_skills
    }

    return {
        "only_in_engram": only_in_engram,
        "only_in_cursor": only_in_cursor,
        "in_both": in_both,
        "cursor_dir": skills_dir,
    }
