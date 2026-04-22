"""Tests for src/workflow.py — committee phase state machine."""
from __future__ import annotations

import json
import os

import pytest

from src.database import init_db
from src.workflow import (
    DEFAULT_PHASE_REQUIREMENTS,
    DEFAULT_PHASES,
    WorkflowViolationError,
    advance_phase,
    init_session_state,
    record_role_contribution,
)


@pytest.fixture
def wf_db(tmp_path):
    """Lightweight DB fixture that does NOT hold an open connection."""
    db_path = str(tmp_path / "wf.db")
    os.environ["ENGRAM_DB_PATH"] = db_path
    init_db(db_path)
    return db_path


# ── Helpers ──────────────────────────────────────────────────────────

def _create_session(db_path: str, session_id: str = "test-session") -> None:
    """Insert a minimal sessions row so workflow functions can find it."""
    from src.database import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO sessions
               (session_id, title, date, domain, workflow_used)
               VALUES (?, 'Test Session', '2026-01-01', 'engineering', NULL)""",
            (session_id,),
        )


def _create_session_with_workflow(
    db_path: str,
    session_id: str,
    workflow_name: str,
    phases: list[str],
    requirements: dict[str, list[str]],
) -> None:
    """Insert a workflow and a session that references it."""
    from src.database import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO workflows (name, description, steps, phases, phase_requirements)
               VALUES (?, 'Test workflow', '[]', ?, ?)""",
            (workflow_name, json.dumps(phases), json.dumps(requirements)),
        )
        conn.execute(
            """INSERT OR IGNORE INTO sessions
               (session_id, title, date, domain, workflow_used)
               VALUES (?, 'Test Session', '2026-01-01', 'engineering', ?)""",
            (session_id, workflow_name),
        )


# ── init_session_state ───────────────────────────────────────────────

class TestInitSessionState:
    def test_initial_phase_is_first_default_phase(self, wf_db):
        _create_session(wf_db)
        state = init_session_state("test-session", db_path=wf_db)
        assert state["current_phase"] == DEFAULT_PHASES[0]

    def test_initial_required_roles_match_first_phase(self, wf_db):
        _create_session(wf_db)
        state = init_session_state("test-session", db_path=wf_db)
        expected = DEFAULT_PHASE_REQUIREMENTS[DEFAULT_PHASES[0]]
        assert state["required_roles"] == expected

    def test_initial_completed_roles_is_empty(self, wf_db):
        _create_session(wf_db)
        state = init_session_state("test-session", db_path=wf_db)
        assert state["completed_roles"] == []

    def test_can_proceed_is_false_initially(self, wf_db):
        _create_session(wf_db)
        state = init_session_state("test-session", db_path=wf_db)
        assert state["can_proceed"] is False

    def test_idempotent_on_duplicate_session(self, wf_db):
        _create_session(wf_db)
        state1 = init_session_state("test-session", db_path=wf_db)
        state2 = init_session_state("test-session", db_path=wf_db)
        assert state1["current_phase"] == state2["current_phase"]


# ── record_role_contribution ─────────────────────────────────────────

class TestRecordRoleContribution:
    def test_role_appears_in_completed_after_recording(self, wf_db):
        _create_session(wf_db)
        init_session_state("test-session", db_path=wf_db)
        state = record_role_contribution("test-session", "Analyst", db_path=wf_db)
        assert "Analyst" in state["completed_roles"]

    def test_can_proceed_true_when_all_roles_done(self, wf_db):
        _create_session(wf_db)
        init_session_state("test-session", db_path=wf_db)
        first_phase = DEFAULT_PHASES[0]
        for role in DEFAULT_PHASE_REQUIREMENTS[first_phase]:
            state = record_role_contribution("test-session", role, db_path=wf_db)
        assert state["can_proceed"] is True
        assert state["missing_roles"] == []

    def test_duplicate_role_not_double_counted(self, wf_db):
        _create_session(wf_db)
        init_session_state("test-session", db_path=wf_db)
        record_role_contribution("test-session", "Analyst", db_path=wf_db)
        state = record_role_contribution("test-session", "Analyst", db_path=wf_db)
        assert state["completed_roles"].count("Analyst") == 1


# ── advance_phase ────────────────────────────────────────────────────

class TestAdvancePhaseRequiresRoles:
    def test_raises_when_roles_incomplete(self, wf_db):
        _create_session(wf_db)
        init_session_state("test-session", db_path=wf_db)
        with pytest.raises(WorkflowViolationError, match="roles still required"):
            advance_phase("test-session", db_path=wf_db)

    def test_advances_when_all_roles_contributed(self, wf_db):
        _create_session(wf_db)
        init_session_state("test-session", db_path=wf_db)
        first_phase = DEFAULT_PHASES[0]
        for role in DEFAULT_PHASE_REQUIREMENTS[first_phase]:
            record_role_contribution("test-session", role, db_path=wf_db)
        state = advance_phase("test-session", db_path=wf_db)
        assert state["current_phase"] == DEFAULT_PHASES[1]

    def test_completed_roles_reset_after_advance(self, wf_db):
        _create_session(wf_db)
        init_session_state("test-session", db_path=wf_db)
        first_phase = DEFAULT_PHASES[0]
        for role in DEFAULT_PHASE_REQUIREMENTS[first_phase]:
            record_role_contribution("test-session", role, db_path=wf_db)
        state = advance_phase("test-session", db_path=wf_db)
        assert state["completed_roles"] == []

    def test_final_phase_returns_message_not_error(self, wf_db):
        """Advancing past the last phase should return gracefully with a message."""
        _create_session(wf_db)
        init_session_state("test-session", db_path=wf_db)

        for phase in DEFAULT_PHASES[:-1]:
            for role in DEFAULT_PHASE_REQUIREMENTS.get(phase, []):
                record_role_contribution("test-session", role, db_path=wf_db)
            advance_phase("test-session", db_path=wf_db)

        last_phase_roles = DEFAULT_PHASE_REQUIREMENTS.get(DEFAULT_PHASES[-1], [])
        for role in last_phase_roles:
            record_role_contribution("test-session", role, db_path=wf_db)

        state = advance_phase("test-session", db_path=wf_db)
        assert "message" in state
        assert "final" in state["message"].lower() or "already" in state["message"].lower()

    def test_raises_without_session_state(self, wf_db):
        with pytest.raises(WorkflowViolationError):
            advance_phase("nonexistent-session", db_path=wf_db)


# ── advance_phase with custom workflow ───────────────────────────────

class TestAdvancePhaseHonorsCustomWorkflow:
    def test_custom_phases_are_used(self, wf_db):
        custom_phases = ["intake", "review", "close"]
        custom_requirements = {
            "intake": ["Initiator"],
            "review": ["Reviewer"],
            "close": ["Closer"],
        }
        _create_session_with_workflow(
            wf_db, "custom-session", "custom-wf",
            custom_phases, custom_requirements,
        )
        init_session_state("custom-session", workflow_name="custom-wf", db_path=wf_db)
        record_role_contribution("custom-session", "Initiator", db_path=wf_db)
        state = advance_phase("custom-session", db_path=wf_db)
        assert state["current_phase"] == "review"

    def test_custom_required_roles_enforced(self, wf_db):
        custom_phases = ["intake", "review", "close"]
        custom_requirements = {
            "intake": ["Initiator"],
            "review": ["Reviewer"],
            "close": ["Closer"],
        }
        _create_session_with_workflow(
            wf_db, "custom-session2", "custom-wf2",
            custom_phases, custom_requirements,
        )
        init_session_state("custom-session2", workflow_name="custom-wf2", db_path=wf_db)
        with pytest.raises(WorkflowViolationError, match="Initiator"):
            advance_phase("custom-session2", db_path=wf_db)

    def test_wrong_role_does_not_satisfy_requirement(self, wf_db):
        custom_phases = ["intake", "close"]
        custom_requirements = {"intake": ["Initiator"], "close": ["Closer"]}
        _create_session_with_workflow(
            wf_db, "custom-session3", "custom-wf3",
            custom_phases, custom_requirements,
        )
        init_session_state("custom-session3", workflow_name="custom-wf3", db_path=wf_db)
        record_role_contribution("custom-session3", "WrongRole", db_path=wf_db)
        with pytest.raises(WorkflowViolationError, match="Initiator"):
            advance_phase("custom-session3", db_path=wf_db)
