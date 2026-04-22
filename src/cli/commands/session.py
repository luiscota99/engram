"""Session and workflow commands."""
from __future__ import annotations

import sys

from ...database import get_connection, get_session_details
from ..fmt import fmt_bold, fmt_dim, fmt_header, fmt_type


def cmd_get_session(args):
    session = get_session_details(args.id)
    if not session:
        print(f"Session '{args.id}' not found.")
        sys.exit(1)

    print(fmt_header(f"Session: {session['title']} ({session['session_id']})\n"))
    print(f"Date:   {session['date']}")
    print(f"Domain: {session['domain']}")
    if session.get("workflow_used"):
        print(f"Workflow: {session['workflow_used']}")
    print("")

    if session.get("key_decisions"):
        print(fmt_bold("Key Decisions:"))
        print(session["key_decisions"])
        print("")

    if session.get("transcripts"):
        print(fmt_bold("Transcripts:"))
        for t in session["transcripts"]:
            print(f"  {fmt_type('session')} [{t['role']}] {t['timestamp']}")
            print(f"    {fmt_dim(t['content'])}")
            print()


def cmd_get_role(args):
    with get_connection() as conn:
        row = conn.execute("SELECT charter, heuristics FROM roles WHERE name = ?", (args.name,)).fetchone()
        if not row:
            print(f"Role '{args.name}' not found.")
            return
        print(fmt_header(f"Role: {args.name}\n"))
        print(fmt_bold("Charter:"))
        print(row["charter"])
        print("\n" + fmt_bold("Heuristics:"))
        print(row["heuristics"])
