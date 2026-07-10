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
RUN_TIMEOUT_SECONDS = 300  # real workflows (test suites, builds) exceed 2 min
# Head+tail budget: failures live at the END of test/build logs, so plain
# head-truncation used to discard exactly the signal the agent needed.
OUTPUT_HEAD_CHARS = 1500
OUTPUT_TAIL_CHARS = 2500


CHANGE_LINE = re.compile(
    r"^ENGRAM_CHANGE\s+target=(?P<target>\S+)"
    r"(?:\s+before=(?P<before>\S*))?(?:\s+after=(?P<after>\S*))?\s*$",
    re.MULTILINE,
)


def _journal_changes(conn, run_id: int, output: str) -> int:
    """Parse `ENGRAM_CHANGE target=... before=... after=...` lines from a run's
    output into the reflex_changes journal — every mutation a reflex reports
    becomes revertible-by-information."""
    n = 0
    for m in CHANGE_LINE.finditer(output or ""):
        conn.execute(
            "INSERT INTO reflex_changes (reflex_run_id, target, before_value, after_value) "
            "VALUES (?, ?, ?, ?)",
            (run_id, m.group("target"), m.group("before"), m.group("after")),
        )
        n += 1
    return n


def _clip_output(text: str) -> str:
    """Keep the start and the end of long output; elide the middle."""
    limit = OUTPUT_HEAD_CHARS + OUTPUT_TAIL_CHARS
    if len(text) <= limit:
        return text
    elided = len(text) - limit
    return (
        text[:OUTPUT_HEAD_CHARS]
        + f"\n…[{elided} chars elided]…\n"
        + text[-OUTPUT_TAIL_CHARS:]
    )

# Consecutive failures before a reflex is auto-demoted (approval revoked,
# failure captured as a mistake). Correctness beats convenience: a script
# that fails twice in a row no longer deserves deterministic-tool status.
DEMOTION_FAIL_STREAK = 2

_TEMPLATE = """#!/usr/bin/env {interpreter}
set -euo pipefail  # fail fast: a silent mid-script failure would record a
                   # false 'ok' run and corrupt the reuse metric
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


def validate_script_syntax(script: str, interpreter: str = "bash") -> str | None:
    """Parse-check a script without executing it. Returns an error or None.

    ``bash -n`` / ``sh -n`` parse only; python uses ``compile``. Approving a
    syntactically broken script used to burn two real runs before
    auto-demotion caught it.
    """
    try:
        if interpreter in ("bash", "sh"):
            proc = subprocess.run(
                [interpreter, "-n", "-c", script], capture_output=True, text=True, timeout=10
            )
            return None if proc.returncode == 0 else (proc.stderr.strip() or "syntax error")
        if interpreter == "python3":
            compile(script, "<reflex>", "exec")
            return None
        return f"unknown interpreter {interpreter!r}"
    except SyntaxError as e:
        return str(e)
    except Exception as e:
        return f"validation failed: {e}"


def _tool_safe_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug[:48] or "unnamed"


def _related_mistakes(skill: dict, *, db_path=None) -> list[dict]:
    """Top known failures related to this workflow — drafted reflexes should
    encode them as preflight guards (a reflex must be MORE correct than fresh
    reasoning, not just cheaper)."""
    try:
        from .search import search

        query = f"{skill['name']} {skill.get('trigger_desc', '')}"[:200]
        hits = search(query, item_type="mistake", limit=3, db_path=db_path, skip_audit=True)
        return [{"title": h["title"], "snippet": (h.get("snippet") or "")[:300]} for h in hits]
    except Exception:
        logger.debug("related-mistake lookup failed for draft", exc_info=True)
        return []


def _llm_draft_script(skill: dict, *, db_path=None) -> tuple[str, dict] | None:
    """Ask the optional LLM layer for a script draft + declared params.

    Returns ``(script, params_schema)`` or None when unavailable.
    """
    try:
        from .llm import call_chat_completion, is_llm_available, parse_json_from_llm

        if not is_llm_available():
            return None
        guards = _related_mistakes(skill, db_path=db_path)
        guard_text = "\n".join(f"- {g['title']}: {g['snippet']}" for g in guards) or "none known"
        prompt = (
            "Convert this engineering workflow into a single executable bash script.\n"
            "Rules:\n"
            "1. Begin with `set -euo pipefail` — fail fast on any error.\n"
            "2. Parameters are read from environment variables named PARAM_<NAME>.\n"
            "3. Add preflight guard checks for the known failures listed below.\n"
            "4. No destructive commands (rm -rf, curl|sh) unless the workflow explicitly requires them.\n"
            'Reply with JSON: {"script": "...", "params": [{"name": "...", "description": "...", "required": true}]}.\n\n'
            f"Workflow name: {skill['name']}\n"
            f"Trigger: {skill.get('trigger_desc', '')}\n"
            f"Steps:\n{skill.get('workflow', '')}\n"
            f"Pitfalls to guard against:\n{skill.get('pitfalls') or 'none listed'}\n"
            f"Known related failures (add guards):\n{guard_text}\n"
        )
        raw = call_chat_completion(
            [{"role": "user", "content": prompt}], task="extract"
        )
        parsed = parse_json_from_llm(raw or "")
        if isinstance(parsed, dict) and parsed.get("script"):
            props = {}
            required = []
            for prm in parsed.get("params") or []:
                if isinstance(prm, dict) and prm.get("name"):
                    key = str(prm["name"]).lower()
                    props[key] = {
                        "type": "string",
                        "description": str(prm.get("description", ""))[:150],
                    }
                    if prm.get("required"):
                        required.append(key)
            schema = {
                "type": "object",
                "properties": props,
                "additionalProperties": True,
                "description": "Values are exported as PARAM_<UPPERCASED_KEY> env vars.",
            }
            if required:
                schema["required"] = required
            return str(parsed["script"]), schema
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

        drafted = _llm_draft_script(skill, db_path=db_path)
        script, params_schema = (drafted if drafted else (None, None))
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

        if params_schema is None:
            params_schema = {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
                "description": "Values are exported as PARAM_<UPPERCASED_KEY> env vars.",
            }
        syntax_error = validate_script_syntax(script, "bash")
        description = (
            f"Reflex (compiled skill): {skill['name']}. "
            f"Trigger: {(skill.get('trigger_desc') or '')[:150]}"
        )
        cursor = c.execute(
            """INSERT INTO reflexes (skill_id, name, description, script, interpreter, params_schema)
               VALUES (?, ?, ?, ?, 'bash', ?)""",
            (skill_id, name, description, script, json.dumps(params_schema)),
        )
        return {
            "id": cursor.lastrowid,
            "name": name,
            "skill_id": skill_id,
            "drafted_by": drafted_by,
            "script": script,
            "syntax_ok": syntax_error is None,
            "syntax_error": syntax_error,
        }


def approve_reflex(reflex_id: int, *, db_path=None, conn=None) -> dict:
    """Mark a reflex approved, pinning the current script hash."""
    with connection_scope(conn, db_path) as c:
        row = c.execute("SELECT * FROM reflexes WHERE id = ?", (reflex_id,)).fetchone()
        if not row:
            raise ValueError(f"Reflex {reflex_id} not found")
        syntax_error = validate_script_syntax(row["script"], row["interpreter"])
        if syntax_error:
            raise ValueError(
                f"Refusing to approve reflex {reflex_id}: script does not parse — {syntax_error}"
            )
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
    import time as _time

    t0 = _time.perf_counter()
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
    duration_ms = int((_time.perf_counter() - t0) * 1000)

    is_monitor = row.get("kind") == "monitor"
    demoted = False
    with get_connection(db_path) as c:
        cur = c.execute(
            "INSERT INTO reflex_runs (reflex_id, started_at, duration_ms, status) VALUES (?, ?, ?, ?)",
            (reflex_id, started, duration_ms, status),
        )
        _journal_changes(c, cur.lastrowid, output)
        if is_monitor and status != "ok":
            # A monitor firing is a FINDING on the watched system, not a broken
            # script: file an alert (deduped while open) instead of demoting —
            # demotion would disable the smoke detector for detecting smoke.
            from .inbox import file_item

            file_item(
                kind="alert",
                severity="high",
                title=f"Monitor {row['name']} fired ({status})",
                body=_clip_output(output),
                source=row["name"],
                finding_key=f"monitor:{row['name']}",
                conn=c,
            )
            c.execute(
                "UPDATE reflexes SET run_count = run_count + 1, last_run_at = ?, "
                "last_status = ? WHERE id = ?",
                (started, status, reflex_id),
            )
        elif status == "ok":
            c.execute(
                "UPDATE reflexes SET run_count = run_count + 1, last_run_at = ?, "
                "last_status = ?, fail_streak = 0 WHERE id = ?",
                (started, status, reflex_id),
            )
        else:
            c.execute(
                "UPDATE reflexes SET run_count = run_count + 1, last_run_at = ?, "
                "last_status = ?, fail_streak = fail_streak + 1 WHERE id = ?",
                (started, status, reflex_id),
            )
            streak = c.execute(
                "SELECT fail_streak FROM reflexes WHERE id = ?", (reflex_id,)
            ).fetchone()["fail_streak"]
            if streak >= DEMOTION_FAIL_STREAK:
                # Auto-demote: revoke approval and capture the failure so the
                # next agent that hits this workflow knows what broke.
                c.execute(
                    "UPDATE reflexes SET approved_at = NULL, approved_hash = NULL, "
                    "fail_streak = 0 WHERE id = ?",
                    (reflex_id,),
                )
                demoted = True
                try:
                    from .memory_ops import create_mistake

                    create_mistake(
                        c,
                        date=started[:10],
                        context=f"Reflex '{row['name']}' (skill #{row['skill_id']})",
                        mistake=f"Auto-demoted after {DEMOTION_FAIL_STREAK} consecutive failures ({status})",
                        fix="(fill in once root cause is known; re-approve with: engram reflex approve)",
                        root_cause=output[:500] or None,
                        tags="reflex,auto-demotion",
                    )
                except Exception:
                    logger.exception("Failed to capture demotion mistake for reflex %s", reflex_id)
        # A reflex run is also a use of the underlying skill — feeds the reuse metric.
        c.execute(
            "UPDATE skills SET usage_count = usage_count + 1, last_used_at = datetime('now') "
            "WHERE id = ?",
            (row["skill_id"],),
        )

    result = {
        "ok": status == "ok",
        "status": status,
        "output": _clip_output(output),
        "reflex": row["name"],
    }
    if demoted:
        result["demoted"] = True
        result["error"] = (
            f"Reflex auto-demoted after {DEMOTION_FAIL_STREAK} consecutive failures. "
            f"The failure was captured as a mistake; fix the script and re-approve."
        )
    return result


# Uses required before a skill is suggested for promotion. Reuse is the
# proof gate: compiling unproven workflows is how prior art (Voyager, AWM)
# drowned in brittle scripts.
PROMOTION_MIN_USES = 5


def get_promotion_candidates(*, min_uses: int = PROMOTION_MIN_USES, db_path=None, conn=None) -> list[dict]:
    """Skills proven by reuse that have no reflex yet — ready for `engram promote`."""
    with connection_scope(conn, db_path) as c:
        rows = c.execute(
            """SELECT s.id, s.name, s.usage_count, s.last_used_at
               FROM skills s
               LEFT JOIN reflexes r ON r.skill_id = s.id
               WHERE r.id IS NULL AND s.usage_count >= ?
                 AND (s.superseded_by IS NULL)
               ORDER BY s.usage_count DESC
               LIMIT 10""",
            (min_uses,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_reflex_success_rates(*, db_path=None, conn=None) -> dict[int, dict]:
    """Per-reflex run stats from reflex_runs: {reflex_id: {runs, ok, rate, avg_ms}}."""
    with connection_scope(conn, db_path) as c:
        try:
            rows = c.execute(
                """SELECT reflex_id, COUNT(*) AS runs,
                          SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok,
                          AVG(duration_ms) AS avg_ms
                   FROM reflex_runs GROUP BY reflex_id"""
            ).fetchall()
        except Exception:
            return {}
        return {
            r["reflex_id"]: {
                "runs": r["runs"],
                "ok": r["ok"],
                "rate": round(r["ok"] / r["runs"], 3) if r["runs"] else None,
                "avg_ms": round(r["avg_ms"] or 0),
            }
            for r in rows
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
