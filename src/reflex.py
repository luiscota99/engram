"""Reflexes — proven skills promoted to executable, human-approved scripts.

The lifecycle that keeps this safe and useful:

1. A workflow is captured as a text **skill** and applied by agents over time.
2. Once *proven* (used repeatedly — see ``usage_count`` and the reuse metric),
   ``engram promote <skill_id>`` drafts a parameterized script from the
   workflow text (LLM-assisted when available, template otherwise).
3. A human reviews the script and runs ``engram reflex approve <id>``.
   Nothing executes before explicit approval, and the approved script's hash
   is pinned — any later edit un-approves it.
4. Approved reflexes are exposed as **MCP tools** (``reflex_<name>``), so
   agents invoke them deterministically instead of re-reasoning through the
   workflow text: ~50 tokens per call instead of thousands, zero variance.
5. A failing reflex records its status; repeated failures are grounds for
   demotion back to a text skill (manual for now).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from datetime import datetime

from .database import connection_scope, get_connection

logger = logging.getLogger(__name__)

ALLOWED_INTERPRETERS = ("bash", "sh", "python3")
RUN_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 8000

_TEMPLATE = """#!/usr/bin/env {interpreter}
# Reflex draft for skill #{skill_id}: {skill_name}
# Drafted from the workflow below — REVIEW AND EDIT before approving.
# Parameters arrive as environment variables: {param_env_names}
#
# Workflow (from the skill entry):
{workflow_comment}

echo "TODO: implement this reflex, then: engram reflex approve <id>"
exit 1
"""


def _script_hash(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def _tool_safe_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug[:48] or "unnamed"


def _llm_draft_script(skill: dict) -> str | None:
    """Ask the optional LLM layer for a script draft; None when unavailable."""
    try:
        from .llm import call_chat_completion, is_llm_available, parse_json_from_llm

        if not is_llm_available():
            return None
        prompt = (
            "Convert this engineering workflow into a single executable bash script.\n"
            "Parameters must be read from environment variables named PARAM_<NAME>.\n"
            "Reply with JSON: {\"script\": \"...\"}.\n\n"
            f"Workflow name: {skill['name']}\n"
            f"Trigger: {skill.get('trigger_desc', '')}\n"
            f"Steps:\n{skill.get('workflow', '')}\n"
            f"Pitfalls to guard against:\n{skill.get('pitfalls') or 'none listed'}\n"
        )
        raw = call_chat_completion(
            [{"role": "user", "content": prompt}], task="extract"
        )
        parsed = parse_json_from_llm(raw or "")
        if isinstance(parsed, dict) and parsed.get("script"):
            return str(parsed["script"])
    except Exception:
        logger.debug("LLM reflex draft failed; falling back to template", exc_info=True)
    return None


def promote_skill(skill_id: int, *, db_path=None, conn=None) -> dict:
    """Draft a reflex from a skill. Returns the created reflex row (unapproved).

    Uses the LLM layer to draft the script when reachable; otherwise emits a
    template with the workflow steps as comments for the human to fill in.
    Either way the reflex is inert until ``approve_reflex``.
    """
    with connection_scope(conn, db_path) as c:
        skill = c.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if not skill:
            raise ValueError(f"Skill {skill_id} not found")
        skill = dict(skill)

        name = _tool_safe_name(skill["name"])
        existing = c.execute("SELECT id FROM reflexes WHERE name = ?", (name,)).fetchone()
        if existing:
            raise ValueError(
                f"Reflex '{name}' already exists (id {existing['id']}); "
                f"delete it first to re-promote."
            )

        script = _llm_draft_script(skill)
        drafted_by = "llm"
        if not script:
            drafted_by = "template"
            workflow_comment = "\n".join(
                f"#   {line}" for line in (skill.get("workflow") or "").splitlines()
            )
            script = _TEMPLATE.format(
                interpreter="bash",
                skill_id=skill_id,
                skill_name=skill["name"],
                param_env_names="PARAM_*",
                workflow_comment=workflow_comment or "#   (empty)",
            )

        description = (
            f"Reflex (compiled skill): {skill['name']}. "
            f"Trigger: {(skill.get('trigger_desc') or '')[:150]}"
        )
        cursor = c.execute(
            """INSERT INTO reflexes (skill_id, name, description, script, interpreter, params_schema)
               VALUES (?, ?, ?, ?, 'bash', ?)""",
            (
                skill_id,
                name,
                description,
                script,
                json.dumps(
                    {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": True,
                        "description": "Values are exported as PARAM_<UPPERCASED_KEY> env vars.",
                    }
                ),
            ),
        )
        return {
            "id": cursor.lastrowid,
            "name": name,
            "skill_id": skill_id,
            "drafted_by": drafted_by,
            "script": script,
        }


def approve_reflex(reflex_id: int, *, db_path=None, conn=None) -> dict:
    """Mark a reflex approved, pinning the current script hash."""
    with connection_scope(conn, db_path) as c:
        row = c.execute("SELECT * FROM reflexes WHERE id = ?", (reflex_id,)).fetchone()
        if not row:
            raise ValueError(f"Reflex {reflex_id} not found")
        h = _script_hash(row["script"])
        c.execute(
            "UPDATE reflexes SET approved_at = datetime('now'), approved_hash = ? WHERE id = ?",
            (h, reflex_id),
        )
        return {"id": reflex_id, "name": row["name"], "approved_hash": h}


def list_reflexes(*, approved_only: bool = False, db_path=None, conn=None) -> list[dict]:
    with connection_scope(conn, db_path) as c:
        where = "WHERE approved_at IS NOT NULL" if approved_only else ""
        rows = c.execute(
            f"SELECT id, skill_id, name, description, interpreter, approved_at, "
            f"approved_hash, script, params_schema, run_count, last_run_at, last_status "
            f"FROM reflexes {where} ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def run_reflex(reflex_id: int, params: dict | None = None, *, db_path=None) -> dict:
    """Execute an approved reflex. Refuses unapproved or tampered scripts.

    Params are exported as ``PARAM_<UPPERCASED_KEY>`` environment variables —
    never interpolated into the script text.
    """
    import os

    with get_connection(db_path) as c:
        row = c.execute("SELECT * FROM reflexes WHERE id = ?", (reflex_id,)).fetchone()
        if not row:
            raise ValueError(f"Reflex {reflex_id} not found")
        row = dict(row)

    if not row["approved_at"]:
        return {"ok": False, "error": "Reflex is not approved. Run: engram reflex approve"}
    if _script_hash(row["script"]) != row["approved_hash"]:
        return {
            "ok": False,
            "error": "Script changed since approval — re-approve before running.",
        }
    if row["interpreter"] not in ALLOWED_INTERPRETERS:
        return {"ok": False, "error": f"Interpreter {row['interpreter']!r} not allowed."}

    env = dict(os.environ)
    for key, value in (params or {}).items():
        env_key = "PARAM_" + re.sub(r"[^A-Z0-9_]", "_", str(key).upper())
        env[env_key] = str(value)

    started = datetime.now().isoformat(timespec="seconds")
    try:
        proc = subprocess.run(
            [row["interpreter"], "-c", row["script"]]
            if row["interpreter"] in ("bash", "sh")
            else [row["interpreter"], "-"],
            input=None if row["interpreter"] in ("bash", "sh") else row["script"],
            env=env,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
        )
        status = "ok" if proc.returncode == 0 else f"exit_{proc.returncode}"
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    except subprocess.TimeoutExpired:
        status = "timeout"
        output = f"Timed out after {RUN_TIMEOUT_SECONDS}s"

    with get_connection(db_path) as c:
        c.execute(
            "UPDATE reflexes SET run_count = run_count + 1, last_run_at = ?, last_status = ? "
            "WHERE id = ?",
            (started, status, reflex_id),
        )
        # A reflex run is also a use of the underlying skill — feeds the reuse metric.
        c.execute(
            "UPDATE skills SET usage_count = usage_count + 1, last_used_at = datetime('now') "
            "WHERE id = ?",
            (row["skill_id"],),
        )

    return {
        "ok": status == "ok",
        "status": status,
        "output": output[:MAX_OUTPUT_CHARS],
        "reflex": row["name"],
    }


def reflex_tools_for_mcp(db_path=None) -> list[dict]:
    """MCP tool definitions for every approved reflex (``reflex_<name>``)."""
    tools = []
    try:
        for r in list_reflexes(approved_only=True, db_path=db_path):
            try:
                schema = json.loads(r["params_schema"] or "{}")
            except (TypeError, ValueError):
                schema = {"type": "object", "additionalProperties": True}
            tools.append(
                {
                    "name": f"reflex_{r['name']}",
                    "description": r["description"][:300],
                    "inputSchema": schema,
                }
            )
    except Exception:
        logger.debug("reflex tool listing unavailable", exc_info=True)
    return tools


def handle_reflex_call(tool_name: str, args: dict, db_path=None) -> str:
    """Route an MCP ``reflex_<name>`` call to the executor."""
    name = tool_name.removeprefix("reflex_")
    with get_connection(db_path) as c:
        row = c.execute("SELECT id FROM reflexes WHERE name = ?", (name,)).fetchone()
    if not row:
        return f"Error: no reflex named {name!r}."
    result = run_reflex(row["id"], params=dict(args or {}), db_path=db_path)
    if result["ok"]:
        return f"Reflex {name} completed.\n\n{result['output']}"
    return f"Reflex {name} failed ({result.get('status', 'error')}): {result.get('error') or result.get('output', '')}"
