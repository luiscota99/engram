"""Crash-proof session checkpoints — the "where did I leave off?" answer.

A Claude Code ``Stop`` hook calls ``engram hook checkpoint`` at the end of
every agent turn, upserting one row per (project, session): the last user
prompt, the agent's last reply, and the git HEAD at that moment. Because it
fires every turn — not at session end — the checkpoint survives any death
(spend-limit cutoff, crash, closed laptop). ``engram resume`` /
``memory_resume`` then answers a fresh session's first question directly,
instead of forcing archaeology over multi-MB transcript files.

Checkpoints are operational state, not memories: they are NOT FTS-indexed,
NOT embedded, and NOT registered as an item type. They never surface in
search, only through ``resume``.
"""

from __future__ import annotations

import json
import os
import subprocess

# The agent's final reply IS the handoff — keep enough of it to be useful.
MAX_SUMMARY_CHARS = 4000
MAX_PROMPT_CHARS = 500
# Only the tail of the transcript matters; a long session file can be >10MB.
TRANSCRIPT_TAIL_BYTES = 512 * 1024


def _normalize_project(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path or "")).rstrip(os.sep) or os.sep


def _git(project_path: str, *argv: str) -> str:
    """Run a git command in *project_path*; empty string on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", project_path, *argv],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _git_state(project_path: str) -> tuple[str, str]:
    """Return (head, branch) for the repo at *project_path* ('' when not a repo)."""
    head = _git(project_path, "log", "-1", "--format=%h %s")
    branch = _git(project_path, "rev-parse", "--abbrev-ref", "HEAD")
    return head, branch


def extract_transcript_tail(transcript_path: str) -> tuple[str, str]:
    """Return (last_user_prompt, last_assistant_text) from a session transcript.

    Reads only the tail of the JSONL file (sessions grow to many MB). Skips
    tool results, hook payloads, and injected system content — a "user" turn
    counts only when it is genuine typed text. Returns empty strings when the
    file is missing or holds no such turns; never raises.
    """
    last_prompt = ""
    last_summary = ""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > TRANSCRIPT_TAIL_BYTES:
                f.seek(size - TRANSCRIPT_TAIL_BYTES)
                f.readline()  # discard the partial line at the cut
            raw_lines = f.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return "", ""

    for line in reversed(raw_lines):
        if last_prompt and last_summary:
            break
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        # API error banners ("hit your spend limit") arrive as assistant text,
        # and sidechain events belong to subagents — neither is the handoff.
        if event.get("isApiErrorMessage") or event.get("isSidechain"):
            continue
        etype = event.get("type")
        message = event.get("message") or {}
        content = message.get("content")
        if etype == "assistant" and not last_summary:
            if isinstance(content, list):
                texts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                ]
                if texts:
                    last_summary = "\n\n".join(texts).strip()
        elif etype == "user" and not last_prompt:
            if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                last_prompt = content.strip()
            elif isinstance(content, list):
                texts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                    and b.get("text", "").strip() and not b["text"].lstrip().startswith("<")
                ]
                if texts:
                    last_prompt = "\n".join(texts).strip()
    return last_prompt, last_summary


def upsert_checkpoint(
    project_path: str,
    session_id: str,
    *,
    last_prompt: str = "",
    last_summary: str = "",
    git_head: str = "",
    git_branch: str = "",
    db_path=None,
) -> None:
    """Insert or refresh the checkpoint row for (project, session).

    A blank ``last_summary``/``last_prompt`` never overwrites a previous
    non-blank one — a turn that produced no visible text (pure tool work)
    must not erase the handoff captured on the turn before it.
    """
    from .database import get_connection

    project = _normalize_project(project_path)
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO checkpoints
                   (project_path, session_id, last_prompt, last_summary,
                    git_head, git_branch, turn_count)
               VALUES (?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(project_path, session_id) DO UPDATE SET
                   last_prompt  = CASE WHEN excluded.last_prompt  != ''
                                       THEN excluded.last_prompt  ELSE last_prompt  END,
                   last_summary = CASE WHEN excluded.last_summary != ''
                                       THEN excluded.last_summary ELSE last_summary END,
                   git_head     = CASE WHEN excluded.git_head != ''
                                       THEN excluded.git_head ELSE git_head END,
                   git_branch   = CASE WHEN excluded.git_branch != ''
                                       THEN excluded.git_branch ELSE git_branch END,
                   turn_count   = turn_count + 1,
                   updated_at   = datetime('now')""",
            (
                project,
                session_id,
                (last_prompt or "")[:MAX_PROMPT_CHARS],
                (last_summary or "")[:MAX_SUMMARY_CHARS],
                git_head or "",
                git_branch or "",
            ),
        )


def checkpoint_from_stop_payload(stdin_text: str, *, db_path=None) -> None:
    """Handle a Claude Code ``Stop`` hook payload: upsert one checkpoint row.

    Reads ``session_id``, ``transcript_path`` and ``cwd`` from the payload.
    Prints nothing and never raises — a broken checkpoint must not disturb
    the agent turn it is recording.
    """
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except (ValueError, AttributeError):
        return
    if not isinstance(payload, dict):
        return

    session_id = str(payload.get("session_id") or "").strip()
    transcript_path = str(payload.get("transcript_path") or "").strip()
    cwd = str(payload.get("cwd") or "").strip() or os.getcwd()
    if not session_id:
        return

    last_prompt, last_summary = (
        extract_transcript_tail(transcript_path) if transcript_path else ("", "")
    )
    git_head, git_branch = _git_state(cwd)
    try:
        upsert_checkpoint(
            cwd,
            session_id,
            last_prompt=last_prompt,
            last_summary=last_summary,
            git_head=git_head,
            git_branch=git_branch,
            db_path=db_path,
        )
    except Exception:
        return
    if transcript_path:
        detect_and_record_echoes(session_id, transcript_path, db_path=db_path)


def record_milestone(
    project_path: str,
    session_id: str,
    summary: str | None = None,
    *,
    db_path=None,
) -> str:
    """Write a DELIBERATE handoff — richer than the ambient per-turn capture.

    The every-turn checkpoint stores whatever the last message happened to be;
    a milestone is a briefing written at a good stopping point. With no
    summary given, one is auto-composed from the git position and recent
    commits. Milestone fields persist until the next milestone, so a later
    "Now running tests..." turn never overwrites the briefing.

    Returns the summary that was stored.
    """
    from .database import get_connection

    project = _normalize_project(project_path)
    git_head, git_branch = _git_state(project)
    if not summary:
        recent = _git(project, "log", "--format=- %h %s", "-5")
        summary = (
            f"Milestone on [{git_branch or '?'}] at {git_head or '(no git)'}.\n"
            f"Recent commits:\n{recent}" if recent else
            f"Milestone checkpoint at {git_head or '(no git)'}."
        )
    summary = summary[:MAX_SUMMARY_CHARS]
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO checkpoints
                   (project_path, session_id, git_head, git_branch,
                    milestone_summary, milestone_at, turn_count)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 0)
               ON CONFLICT(project_path, session_id) DO UPDATE SET
                   milestone_summary = excluded.milestone_summary,
                   milestone_at = excluded.milestone_at,
                   git_head = CASE WHEN excluded.git_head != ''
                                   THEN excluded.git_head ELSE git_head END,
                   git_branch = CASE WHEN excluded.git_branch != ''
                                     THEN excluded.git_branch ELSE git_branch END,
                   updated_at = datetime('now')""",
            (project, session_id, git_head or "", git_branch or "", summary),
        )
    return summary


_CITATION_RE = None


def detect_and_record_echoes(session_id: str, transcript_path: str, *, db_path=None) -> int:
    """Injection echo: memories the hooks injected that the agent then CITED.

    A citation like "[MISTAKE #26]" or "mistake #26" in the agent's visible
    output is textual evidence the injected memory shaped the work — a free,
    automatic helpfulness signal (case study 2026-07-17: 137 items surfaced,
    the 11 echoed ones were exactly the load-bearing memories). Each echoed
    (item, session) records ONE weak-positive feedback row (source='echo');
    explicit feedback stays separable by source, and no FSRS event fires —
    an echo is evidence of use, not a graded recall outcome.

    Returns the number of new echo rows. Never raises.
    """
    import re

    global _CITATION_RE
    if _CITATION_RE is None:
        _CITATION_RE = re.compile(
            r"\b(mistake|pattern|skill|conversation|prompt)\s*#(\d+)", re.IGNORECASE
        )
    try:
        from . import config
        from .database import get_connection

        audit_path = config.audit_log_path()
        if not audit_path or not os.path.exists(audit_path) or not session_id:
            return 0

        injected: set[tuple[str, int]] = set()
        with open(audit_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(rec, dict)
                    and str(rec.get("source", "")).endswith("_inject")
                    and rec.get("session_id") == session_id
                ):
                    for ref in rec.get("items") or []:
                        injected.add((str(ref["item_type"]), int(ref["item_id"])))
        if not injected:
            return 0

        # Assistant text from the transcript tail (bounded read, as ever).
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > TRANSCRIPT_TAIL_BYTES:
                f.seek(size - TRANSCRIPT_TAIL_BYTES)
                f.readline()
            raw = f.read().decode("utf-8", errors="ignore")
        cited: set[tuple[str, int]] = set()
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            if event.get("isSidechain") or event.get("isApiErrorMessage"):
                continue
            for b in event.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "text":
                    for m in _CITATION_RE.finditer(b.get("text", "")):
                        cited.add((m.group(1).lower(), int(m.group(2))))

        echoes = injected & cited
        if not echoes:
            return 0
        marker = f"echo:{session_id}"
        new = 0
        with get_connection(db_path) as conn:
            for item_type, item_id in sorted(echoes):
                exists = conn.execute(
                    "SELECT 1 FROM retrieval_feedback WHERE item_type = ? AND item_id = ? "
                    "AND source = 'echo' AND query = ?",
                    (item_type, item_id, marker),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO retrieval_feedback (item_type, item_id, helpful, query, source) "
                    "VALUES (?, ?, 1, ?, 'echo')",
                    (item_type, item_id, marker),
                )
                new += 1
        return new
    except Exception:
        import logging

        logging.getLogger(__name__).debug("echo detection failed", exc_info=True)
        return 0


def get_checkpoints(project_path: str, *, limit: int = 3, db_path=None) -> list[dict]:
    """Most recent checkpoints for a project, newest first."""
    from .database import get_connection

    project = _normalize_project(project_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM checkpoints WHERE project_path = ?
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (project, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _commits_since(project_path: str, git_head: str) -> list[str]:
    """Commit subjects made after *git_head* ('' entries filtered)."""
    if not git_head:
        return []
    ref = git_head.split()[0]
    out = _git(project_path, "log", "--format=%h %s", f"{ref}..HEAD")
    return [line for line in out.splitlines() if line.strip()]


def build_resume_report(project_path: str, *, limit: int = 1, db_path=None) -> str:
    """Human/agent-readable "where we left off" for a project.

    Latest checkpoint(s): when, which session, git position (plus any commits
    made since — e.g. by another session), the user's last prompt and the
    agent's final reply. Empty string when the project has no checkpoints.
    """
    checkpoints = get_checkpoints(project_path, limit=max(1, limit), db_path=db_path)
    if not checkpoints:
        return ""

    project = _normalize_project(project_path)
    parts: list[str] = []
    for i, cp in enumerate(checkpoints):
        head = "Latest checkpoint" if i == 0 else "Earlier checkpoint"
        lines = [
            f"## {head} — {cp['updated_at']} UTC "
            f"(session {cp['session_id'][:8]}…, {cp['turn_count']} turns)"
        ]
        if cp["git_head"]:
            lines.append(f"Git: [{cp['git_branch'] or '?'}] at {cp['git_head']}")
            if i == 0:
                newer = _commits_since(project, cp["git_head"])
                if newer:
                    lines.append(f"Commits since this checkpoint ({len(newer)}):")
                    lines.extend(f"  - {c}" for c in newer)
        if cp.get("milestone_summary"):
            lines.append(
                f"\nMILESTONE HANDOFF ({cp.get('milestone_at')} UTC):\n{cp['milestone_summary']}"
            )
        if cp["last_prompt"]:
            lines.append(f"\nLast user prompt:\n> {cp['last_prompt']}")
        if cp["last_summary"]:
            lines.append(f"\nAgent's last reply:\n{cp['last_summary']}")
        parts.append("\n".join(lines))
    return f"# Resume — {project}\n\n" + "\n\n---\n\n".join(parts)
