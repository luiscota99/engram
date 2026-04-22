"""
Workflow module — session state machine and committee phase enforcement.

Manages the committee-driven workflow lifecycle:
  analysis → research → critique → decision → archive

Each phase has required roles that must contribute (via add_transcript) before
the session can advance. Attempting to add a decision without all required roles
having contributed raises a WorkflowViolationError.
"""

from __future__ import annotations


import json

from .database import get_connection


# Default workflow: ordered phases and the roles required in each.
DEFAULT_PHASES = ["analysis", "research", "critique", "decision", "archive"]

DEFAULT_PHASE_REQUIREMENTS: dict[str, list[str]] = {
    "analysis": ["Analyst"],
    "research": ["Researcher"],
    "critique": ["Skeptic"],
    "decision": ["Facilitator"],
    "archive": ["Archivist"],
}


class WorkflowViolationError(Exception):
    """Raised when a workflow rule is violated (e.g., missing role contribution)."""


def _load_state(conn, session_id: str) -> dict | None:
    """Return raw session_state row as dict, or None."""
    row = conn.execute(
        "SELECT * FROM session_state WHERE session_id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def init_session_state(
    session_id: str,
    workflow_name: str | None = None,
    db_path=None,
) -> dict:
    """Create a session_state record for a new session.

    Optionally loads phase requirements from a named workflow in the DB.
    Falls back to DEFAULT_PHASE_REQUIREMENTS.
    """
    phases = DEFAULT_PHASES[:]
    requirements = DEFAULT_PHASE_REQUIREMENTS.copy()

    if workflow_name:
        with get_connection(db_path) as conn:
            wf = conn.execute(
                "SELECT phases, phase_requirements FROM workflows WHERE name = ?",
                (workflow_name,),
            ).fetchone()
            if wf:
                if wf["phases"]:
                    try:
                        phases = json.loads(wf["phases"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if wf["phase_requirements"]:
                    try:
                        requirements = json.loads(wf["phase_requirements"])
                    except (json.JSONDecodeError, TypeError):
                        pass

    first_phase = phases[0] if phases else "analysis"
    required = requirements.get(first_phase, [])

    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO session_state
               (session_id, current_phase, required_roles, completed_roles, can_proceed)
               VALUES (?, ?, ?, '[]', 0)
               ON CONFLICT(session_id) DO NOTHING""",
            (session_id, first_phase, json.dumps(required)),
        )
        return get_session_state(session_id, db_path=db_path)


def get_session_state(session_id: str, db_path=None) -> dict:
    """Return current workflow state for a session."""
    with get_connection(db_path) as conn:
        row = _load_state(conn, session_id)
        if not row:
            return {
                "session_id": session_id,
                "current_phase": None,
                "required_roles": [],
                "completed_roles": [],
                "can_proceed": False,
                "missing_roles": [],
            }
        required = json.loads(row["required_roles"] or "[]")
        completed = json.loads(row["completed_roles"] or "[]")
        missing = [r for r in required if r not in completed]
        return {
            "session_id": session_id,
            "current_phase": row["current_phase"],
            "required_roles": required,
            "completed_roles": completed,
            "can_proceed": bool(row["can_proceed"]),
            "missing_roles": missing,
        }


def record_role_contribution(session_id: str, role: str, db_path=None) -> dict:
    """Record that a role has contributed a transcript for the current phase.

    Returns the updated state dict including whether we can now proceed.
    """
    with get_connection(db_path) as conn:
        row = _load_state(conn, session_id)
        if not row:
            return get_session_state(session_id, db_path=db_path)

        completed = json.loads(row["completed_roles"] or "[]")
        required = json.loads(row["required_roles"] or "[]")

        if role not in completed:
            completed.append(role)

        all_done = all(r in completed for r in required)

        conn.execute(
            "UPDATE session_state SET completed_roles = ?, can_proceed = ? WHERE session_id = ?",
            (json.dumps(completed), 1 if all_done else 0, session_id),
        )

    return get_session_state(session_id, db_path=db_path)


def advance_phase(session_id: str, db_path=None) -> dict:
    """Attempt to advance the session to the next phase.

    Raises WorkflowViolationError if required roles have not all contributed.
    Returns the updated state after advancing.
    """
    state = get_session_state(session_id, db_path=db_path)
    if not state["current_phase"]:
        raise WorkflowViolationError(f"Session '{session_id}' has no workflow state. Call memory_init_session first.")

    if state["missing_roles"]:
        missing = ", ".join(state["missing_roles"])
        raise WorkflowViolationError(
            f"Cannot advance from phase '{state['current_phase']}': "
            f"roles still required: {missing}"
        )

    # Determine next phase
    phases = DEFAULT_PHASES
    current_idx = phases.index(state["current_phase"]) if state["current_phase"] in phases else -1
    if current_idx == -1 or current_idx >= len(phases) - 1:
        return {**state, "message": f"Already at final phase '{state['current_phase']}'."}

    next_phase = phases[current_idx + 1]
    next_required = DEFAULT_PHASE_REQUIREMENTS.get(next_phase, [])

    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE session_state SET current_phase = ?, required_roles = ?, "
            "completed_roles = '[]', can_proceed = 0 WHERE session_id = ?",
            (next_phase, json.dumps(next_required), session_id),
        )

    return get_session_state(session_id, db_path=db_path)


def check_decision_allowed(session_id: str, db_path=None) -> None:
    """Raise WorkflowViolationError if a decision cannot be added yet.

    Call this before memory_add_decision to enforce the committee gate.
    """
    state = get_session_state(session_id, db_path=db_path)
    if not state["current_phase"]:
        return  # No state machine — allow (backwards compat)

    if state["missing_roles"]:
        missing = ", ".join(state["missing_roles"])
        raise WorkflowViolationError(
            f"Cannot add decision: required roles have not yet contributed in phase "
            f"'{state['current_phase']}': {missing}. "
            f"Add transcripts from those roles first."
        )
