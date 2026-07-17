"""Inbox — alerts and decision requests: agents/monitors propose, the user decides.

The asynchronous extension of the approval principle: elicitation works only
inside a live session; the inbox works when the user is away. Two item kinds:

- **alert**: informs ("container at 100% since 14:32, evidence attached").
- **decision**: proposes a concrete action (optionally a reflex + params) and
  WAITS. Nothing executes until ``engram decide <id> --approve [--run]``.

Delivery vs existence: every item lands in the table; only items at or above
``ENGRAM_NOTIFY_MIN_SEVERITY`` trigger the user's approved ``notify`` reflex
(a script the user reviewed, like any other reflex — the notification channel
itself is under the trust model).

Idempotent filing: a ``finding_key`` dedups recurring findings so the daily
self-check never re-files an item that is still open.
"""
from __future__ import annotations

import json
import logging
import os

from .database import connection_scope, get_connection

logger = logging.getLogger(__name__)

SEVERITIES = ("info", "warning", "high", "critical")
DEFAULT_NOTIFY_MIN_SEVERITY = "high"


def _severity_rank(sev: str) -> int:
    try:
        return SEVERITIES.index(sev)
    except ValueError:
        return 1


def file_item(
    *,
    kind: str = "alert",
    severity: str = "warning",
    title: str,
    body: str | None = None,
    source: str | None = None,
    finding_key: str | None = None,
    proposed_reflex_id: int | None = None,
    proposed_params: dict | None = None,
    db_path=None,
    conn=None,
    notify: bool = True,
) -> int | None:
    """File an inbox item. Returns the row id, or None when deduped.

    Dedup: if ``finding_key`` is set and an OPEN item with the same key exists,
    nothing is filed (the standing item already covers the finding).

    Callers that pass their own ``conn`` MUST pass ``notify=False`` and call
    ``maybe_notify`` after their transaction commits: the notify reflex is a
    subprocess, and dispatching it inside an open write transaction holds the
    DB write lock for the subprocess's lifetime (and its own bookkeeping
    INSERT then deadlocks against the uncommitted outer transaction).
    """
    with connection_scope(conn, db_path) as c:
        # Dedup is enforced by idx_inbox_open_finding (unique finding_key among
        # OPEN items, v24): OR IGNORE makes concurrent filers race-safe — the
        # loser's insert is a no-op and reports None, same as before.
        cursor = c.execute(
            """INSERT OR IGNORE INTO inbox (kind, severity, title, body, source, finding_key,
                                  proposed_reflex_id, proposed_params)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                kind,
                severity if severity in SEVERITIES else "warning",
                title,
                body,
                source,
                finding_key,
                proposed_reflex_id,
                json.dumps(proposed_params) if proposed_params else None,
            ),
        )
        # rowcount==0 → the partial unique index ignored the insert (an open
        # item with this finding_key exists); lastrowid is stale in that case.
        item_id = cursor.lastrowid if cursor.rowcount else None

    if item_id is not None and notify:
        maybe_notify(severity, title, body or "", source or "", db_path=db_path)
    return item_id


def maybe_notify(severity: str, title: str, body: str, source: str, *, db_path=None) -> None:
    """Deliver via the user-approved ``notify`` reflex when severity crosses
    the threshold. Never raises; never notifies about the notifier."""
    if source == "notify":
        return
    threshold = os.environ.get("ENGRAM_NOTIFY_MIN_SEVERITY", DEFAULT_NOTIFY_MIN_SEVERITY)
    if _severity_rank(severity) < _severity_rank(threshold):
        return
    try:
        with get_connection(db_path) as c:
            row = c.execute(
                "SELECT id FROM reflexes WHERE name = 'notify' AND approved_at IS NOT NULL"
            ).fetchone()
        if not row:
            return
        from .reflex import run_reflex

        run_reflex(
            row["id"],
            params={"title": title, "body": body[:500], "severity": severity},
            db_path=db_path,
        )
    except Exception:
        logger.warning("notify reflex delivery failed", exc_info=True)


def list_items(*, status: str = "open", db_path=None, conn=None) -> list[dict]:
    """Open items first by severity (critical→info), then newest first."""
    with connection_scope(conn, db_path) as c:
        rows = c.execute(
            """SELECT * FROM inbox WHERE status = ?
               ORDER BY CASE severity
                   WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                   WHEN 'warning' THEN 2 ELSE 3 END, id DESC""",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def open_counts(*, db_path=None, conn=None) -> dict:
    with connection_scope(conn, db_path) as c:
        rows = c.execute(
            "SELECT severity, COUNT(*) AS n FROM inbox WHERE status = 'open' GROUP BY severity"
        ).fetchall()
        return {r["severity"]: r["n"] for r in rows}


def decide(
    item_id: int,
    decision: str,
    *,
    run: bool = False,
    db_path=None,
) -> dict:
    """Resolve a decision/alert. decision: approve | reject | acknowledge.

    ``run=True`` with approve executes the proposed reflex (if any) with the
    proposed params — the ONLY path from proposal to execution, and it is
    human-invoked by construction.
    """
    status = {"approve": "approved", "reject": "rejected", "acknowledge": "acknowledged"}.get(
        decision
    )
    if not status:
        raise ValueError(f"Unknown decision {decision!r}")

    with get_connection(db_path) as c:
        row = c.execute("SELECT * FROM inbox WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise ValueError(f"Inbox item {item_id} not found")
        row = dict(row)
        # Guarded transition: two concurrent decide calls must not both win —
        # the WHERE clause claims the item atomically, and the loser gets the
        # already-decided error instead of a second execution.
        claimed = c.execute(
            "UPDATE inbox SET status = ?, decided_at = datetime('now') "
            "WHERE id = ? AND status = 'open'",
            (status, item_id),
        )
        if claimed.rowcount == 0:
            raise ValueError(f"Inbox item {item_id} is already {row['status']}")

    result: dict = {"id": item_id, "status": status, "title": row["title"]}
    if decision == "approve" and run and row["proposed_reflex_id"]:
        from .reflex import run_reflex

        params = json.loads(row["proposed_params"]) if row["proposed_params"] else {}
        outcome = run_reflex(row["proposed_reflex_id"], params=params, db_path=db_path)
        result["run"] = outcome
        # 'executed' means it RAN AND SUCCEEDED. A failed/refused/timed-out run
        # stays 'approved' so the inbox never claims an action happened when it
        # didn't; the outcome carries the error for the user.
        if outcome.get("ok"):
            with get_connection(db_path) as c:
                c.execute("UPDATE inbox SET status = 'executed' WHERE id = ?", (item_id,))
            result["status"] = "executed"
        else:
            result["run_failed"] = True
    return result
