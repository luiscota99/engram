"""Skill validation — proof that a memory changes behavior, not just that it's stored.

Adapted from Superpowers' "TDD for skills": *if you didn't watch an agent fail
without the skill, you don't know if the skill teaches the right thing.* Engram's
reuse metric proves a memory gets **retrieved**; this proves it **works**.

A validation test attaches a scenario + assertion to a skill/pattern/mistake. To
run it, the LLM answers the scenario twice — once cold (baseline), once with the
memory's content injected (treatment). Verdicts:

- ``validated``   — baseline FAILS, treatment PASSES: the memory earned its keep.
- ``redundant``   — both pass: the model already knew this; the memory adds nothing.
- ``ineffective`` — treatment fails: the memory did not fix the behavior.
- ``untested``    — no LLM backend reachable.

Only ``validated`` is a meaningful pass — a test that passes cold proves nothing,
exactly the loophole Superpowers' "watch it fail first" rule closes.
"""
from __future__ import annotations

import logging

from .database import connection_scope, get_connection, get_item

logger = logging.getLogger(__name__)

GRADERS = ("contains", "llm_judge")


def add_skill_test(
    item_type: str,
    item_id: int,
    scenario: str,
    assertion: str,
    *,
    grader: str = "contains",
    db_path=None,
    conn=None,
) -> int:
    """Attach a validation scenario to a memory item. Returns the test id."""
    if grader not in GRADERS:
        raise ValueError(f"grader must be one of {GRADERS}")
    with connection_scope(conn, db_path) as c:
        cur = c.execute(
            "INSERT INTO skill_tests (item_type, item_id, scenario, assertion, grader) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_type, int(item_id), scenario, assertion, grader),
        )
        return int(cur.lastrowid or 0)


def _memory_content(item_type: str, item_id: int, db_path=None) -> str:
    """Flatten a memory item into the text a real agent would receive."""
    item = get_item(item_type, item_id)
    if not item:
        return ""
    fields = ("name", "trigger_desc", "workflow", "pitfalls", "symptoms",
              "root_cause", "standard_fix", "context", "mistake", "fix", "prevention")
    parts = [str(item[f]) for f in fields if item.get(f)]
    return "\n".join(parts)


def _grade(answer: str, assertion: str, grader: str) -> bool:
    """Did *answer* satisfy *assertion*? ``contains`` is deterministic; ``llm_judge``
    asks the model (falls back to contains when the LLM is unreachable)."""
    answer = answer or ""
    if grader == "contains":
        return assertion.lower() in answer.lower()
    try:
        from .llm import call_chat_completion, is_llm_available

        if not is_llm_available():
            return assertion.lower() in answer.lower()
        verdict = call_chat_completion(
            [{
                "role": "user",
                "content": (
                    "Answer only YES or NO. Does the following response satisfy this "
                    f"requirement?\nRequirement: {assertion}\n\nResponse:\n{answer}"
                ),
            }],
            task="audit",
            max_tokens=5,
        )
        return bool(verdict) and "yes" in verdict.lower()
    except Exception:
        logger.debug("llm_judge grading failed; falling back to contains", exc_info=True)
        return assertion.lower() in answer.lower()


def run_skill_test(test_id: int, *, db_path=None) -> dict:
    """Run one validation: baseline (cold) vs treatment (memory injected)."""
    from .llm import call_chat_completion, is_llm_available

    with get_connection(db_path) as c:
        row = c.execute("SELECT * FROM skill_tests WHERE id = ?", (test_id,)).fetchone()
        if not row:
            raise ValueError(f"Skill test {test_id} not found")
        row = dict(row)

    if not is_llm_available():
        _record(test_id, "untested", None, None, db_path=db_path)
        return {"id": test_id, "result": "untested", "reason": "no LLM backend reachable"}

    content = _memory_content(row["item_type"], row["item_id"], db_path=db_path)
    if not content:
        _record(test_id, "untested", None, None, db_path=db_path)
        return {"id": test_id, "result": "untested", "reason": "memory item empty/missing"}

    baseline = call_chat_completion(
        [{"role": "user", "content": row["scenario"]}], task="audit"
    )
    treatment = call_chat_completion(
        [{"role": "user", "content": f"{row['scenario']}\n\nRelevant knowledge:\n{content}"}],
        task="audit",
    )
    # A failed/timed-out generation is "we don't know", never "the memory
    # doesn't work" — grading an empty string as a real answer converted
    # LLM timeouts into INEFFECTIVE verdicts (observed live, llama3.2 on
    # CPU exceeding the client timeout).
    if baseline is None or treatment is None:
        _record(test_id, "untested", None, None, db_path=db_path)
        return {
            "id": test_id,
            "result": "untested",
            "reason": "LLM generation failed or timed out (try ENGRAM_LLM_TIMEOUT=180)",
        }

    b_ok = _grade(baseline, row["assertion"], row["grader"])
    t_ok = _grade(treatment, row["assertion"], row["grader"])

    if not b_ok and t_ok:
        result = "validated"
    elif b_ok and t_ok:
        result = "redundant"
    elif not t_ok:
        result = "ineffective"
    else:  # b_ok and not t_ok — the memory made it worse
        result = "regressed"

    _record(test_id, result, b_ok, t_ok, db_path=db_path)
    return {
        "id": test_id,
        "result": result,
        "baseline_passed": b_ok,
        "treatment_passed": t_ok,
    }


def _record(test_id, result, b_ok, t_ok, *, db_path=None) -> None:
    with get_connection(db_path) as c:
        c.execute(
            "UPDATE skill_tests SET last_result = ?, baseline_passed = ?, "
            "treatment_passed = ?, last_run_at = datetime('now') WHERE id = ?",
            (result, None if b_ok is None else int(b_ok),
             None if t_ok is None else int(t_ok), test_id),
        )


def run_all_tests(*, only_stale: bool = False, db_path=None) -> dict:
    """Run every validation test (or only never-run ones). Returns a tally."""
    with get_connection(db_path) as c:
        where = "WHERE last_run_at IS NULL" if only_stale else ""
        ids = [r["id"] for r in c.execute(f"SELECT id FROM skill_tests {where} ORDER BY id")]
    tally: dict = {}
    for tid in ids:
        res = run_skill_test(tid, db_path=db_path)["result"]
        tally[res] = tally.get(res, 0) + 1
    return {"ran": len(ids), "by_result": tally}


def validation_status(item_type: str, item_id: int, *, db_path=None, conn=None) -> str | None:
    """Best known validation verdict for an item: 'validated' wins if any test passed."""
    with connection_scope(conn, db_path) as c:
        rows = c.execute(
            "SELECT last_result FROM skill_tests WHERE item_type = ? AND item_id = ?",
            (item_type, int(item_id)),
        ).fetchall()
    results = [r["last_result"] for r in rows if r["last_result"]]
    if not results:
        return None
    if "validated" in results:
        return "validated"
    return results[0]


def validated_item_ids(*, db_path=None, conn=None) -> set:
    """Set of (item_type, item_id) with at least one 'validated' test — for ranking."""
    with connection_scope(conn, db_path) as c:
        rows = c.execute(
            "SELECT DISTINCT item_type, item_id FROM skill_tests WHERE last_result = 'validated'"
        ).fetchall()
    return {(r["item_type"], r["item_id"]) for r in rows}
