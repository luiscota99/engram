"""Shared memory write operations — single code path for CLI and MCP."""
from __future__ import annotations

from typing import Iterable

from .database import index_in_fts, link_tags


def _parse_tags(tags: str | Iterable[str] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t).strip() for t in tags if str(t).strip()]


def create_mistake(
    conn,
    *,
    date: str,
    context: str,
    mistake: str,
    fix: str,
    root_cause: str | None = None,
    prevention: str | None = None,
    conversation_id: str | None = None,
    tags: str | Iterable[str] | None = None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO mistakes (date, context, mistake, root_cause, fix, prevention, conversation_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (date, context, mistake, root_cause, fix, prevention, conversation_id),
    )
    mid = cursor.lastrowid
    tag_list = _parse_tags(tags)
    link_tags(conn, "mistake", mid, tag_list)
    content = (
        f"{context} | {mistake} | {root_cause or ''} | {fix} | {prevention or ''}"
    )
    index_in_fts(conn, "mistake", mid, mistake[:80], content, tag_list)
    return mid


def create_pattern(
    conn,
    *,
    name: str,
    symptoms: str,
    root_cause: str,
    standard_fix: str,
    tags: str | Iterable[str] | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO patterns (name, symptoms, root_cause, standard_fix) VALUES (?, ?, ?, ?)",
        (name, symptoms, root_cause, standard_fix),
    )
    pid = cursor.lastrowid
    tag_list = _parse_tags(tags)
    link_tags(conn, "pattern", pid, tag_list)
    content = f"{symptoms} | {root_cause} | {standard_fix}"
    index_in_fts(conn, "pattern", pid, name, content, tag_list)
    return pid


def create_skill(
    conn,
    *,
    name: str,
    domain: str,
    trigger: str,
    workflow: str,
    pitfalls: str | None = None,
    key_files: str | None = None,
    dependencies: str | None = None,
    tags: str | Iterable[str] | None = None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name, domain, trigger, workflow, pitfalls, key_files, dependencies),
    )
    sid = cursor.lastrowid
    tag_list = _parse_tags(tags)
    link_tags(conn, "skill", sid, tag_list)
    content = f"{trigger} | {workflow} | {pitfalls or ''}"
    index_in_fts(conn, "skill", sid, name, content, tag_list)
    return sid


def create_conversation(
    conn,
    *,
    conversation_id: str,
    title: str,
    date: str,
    domain: str,
    tasks_completed: str | None = None,
    key_decisions: str | None = None,
    mistakes_summary: str | None = None,
    skills_extracted: str | None = None,
    tags: str | Iterable[str] | None = None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO conversations (conversation_id, title, date, domain, tasks_completed,
           key_decisions, mistakes_summary, skills_extracted) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            conversation_id,
            title,
            date,
            domain,
            tasks_completed,
            key_decisions,
            mistakes_summary,
            skills_extracted,
        ),
    )
    cid = cursor.lastrowid
    tag_list = _parse_tags(tags)
    link_tags(conn, "conversation", cid, tag_list)
    content = f"{tasks_completed or ''} | {key_decisions or ''} | {mistakes_summary or ''}"
    index_in_fts(conn, "conversation", cid, title, content, tag_list)
    return cid


def create_session(
    conn,
    *,
    session_id: str,
    title: str,
    date: str,
    domain: str,
    workflow_used: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO sessions (session_id, title, date, domain, workflow_used) VALUES (?, ?, ?, ?, ?)",
        (session_id, title, date, domain, workflow_used),
    )
    sid = cursor.lastrowid
    content = f"{title} | {workflow_used or ''}"
    index_in_fts(conn, "session", sid, session_id, content, [])
    return sid


def create_transcript(
    conn,
    *,
    session_id: str,
    role: str,
    content: str,
) -> None:
    conn.execute(
        "INSERT INTO session_transcripts (session_id, role, content) VALUES (?, ?, ?)",
        (session_id, role, content),
    )


def add_decision(
    conn,
    *,
    session_id: str,
    decision: str,
) -> None:
    conn.execute(
        "UPDATE sessions SET key_decisions = IFNULL(key_decisions, '') || char(10) || ? WHERE session_id = ?",
        (decision, session_id),
    )


def create_prompt(
    conn,
    *,
    name: str,
    role: str,
    domain: str,
    description: str,
    prompt_text: str | None = None,
    source_path: str | None = None,
    best_for: str | None = None,
    tags: str | Iterable[str] | None = None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO prompts (name, role, domain, description, prompt_text, source_path, best_for)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name, role, domain, description, prompt_text or "", source_path, best_for),
    )
    pid = cursor.lastrowid
    tag_list = _parse_tags(tags)
    link_tags(conn, "prompt", pid, tag_list)
    content = f"{role} | {description} | {best_for or ''} | {(prompt_text or '')[:500]}"
    index_in_fts(conn, "prompt", pid, name, content, tag_list)
    return pid
