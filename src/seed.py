"""
Seed module — populates the database with existing data from flat-file KIs.
Run once to migrate from markdown-based memory to SQLite.
"""

from .database import get_connection, index_in_fts, init_db, link_tags

SEED_MISTAKES = [
    {
        "date": "2026-04-10",
        "context": "Implementing a new search endpoint that returns users and their roles.",
        "mistake": "Fetched roles individually inside a loop iterating over users, causing database CPU spikes.",
        "root_cause": "The ORM lazy-loaded the relationships by default inside the loop without prefetching.",
        "fix": "Refactored the query to use a batched IN (...) statement to fetch all roles in one query.",
        "prevention": "Always use IN (...) or JOIN statements when fetching related entities for a collection. Add a performance test for endpoints returning lists.",
        "conversation_id": "conv-perf-opt-001",
        "tags": ["database", "performance", "n-plus-one", "orm"],
    },
    {
        "date": "2026-04-12",
        "context": "AI Agent refactoring a helper function in a large utility file.",
        "mistake": "Replaced the entire file content just to update one function, accidentally overwriting concurrent changes.",
        "root_cause": "Used a full-file overwrite tool instead of a targeted multi-line replace tool.",
        "fix": "Restored the file from git history and re-applied the specific function change using AST-based edits.",
        "prevention": "Always use targeted multi-line replace tools or AST-based edits instead of full-file overwrites for minor changes.",
        "conversation_id": "conv-agent-tools-002",
        "tags": ["ai-assistant", "tool-usage", "git", "refactoring"],
    },
    {
        "date": "2026-04-15",
        "context": "Fetching data on component mount in React UI.",
        "mistake": "Left the useEffect dependency array out entirely, causing an infinite render loop and DDOSing the backend.",
        "root_cause": "Developer forgot to add the empty array [] to specify the effect should only run on mount.",
        "fix": "Added the empty dependency array [] to the useEffect call.",
        "prevention": "Always include [] for mount-only effects, and enable ESLint rules (exhaustive-deps) to catch missing arrays.",
        "conversation_id": "conv-ui-debug-003",
        "tags": ["react", "frontend", "infinite-loop", "hooks"],
    },
]

SEED_PATTERNS = [
    {
        "name": "Unhandled Asynchronous State (Race Conditions)",
        "symptoms": "UI shows wrong data intermittently when clicking rapidly; logs show responses processed out of order.",
        "root_cause": "Firing multiple async requests without aborting previous ones or disabling UI interactions.",
        "standard_fix": "Implement an AbortController for fetch requests or a unique request ID check before updating state.",
        "tags": ["async", "race-condition", "frontend", "state-management"],
        "occurrences": [
            ("conv-ui-debug-003", "2026-04-15", "Search input debouncing failed due to race conditions")
        ],
    },
    {
        "name": "Silent Failure on API Schema Drift",
        "symptoms": "Frontend application renders blank components without explicit console errors.",
        "root_cause": "The backend changed a field from string to null, and the frontend blindly tried to call string methods on it.",
        "standard_fix": "Use strict schema validation (e.g., Zod or Pydantic) at the API boundary to catch contract violations early.",
        "tags": ["api", "schema", "validation", "silent-failure"],
        "occurrences": [
            ("conv-schema-drift-004", "2026-04-18", "User profile payload dropped the middle_name field")
        ],
    },
]

SEED_SKILLS = [
    {
        "name": "Safe Database Migration Workflow",
        "domain": "backend",
        "trigger_desc": "User wants to alter a database schema or add new tables in a production environment",
        "workflow": "1. Create up/down migration scripts.\n2. Test locally on a copy of production data.\n3. Run dry-run via CI/CD.\n4. Deploy migration *before* code that depends on it.\n5. Monitor error rates.",
        "pitfalls": "Locking tables for too long; dropping columns without a deprecation phase; failing to test the rollback (down) script.",
        "key_files": '["migrations/", "schema.sql"]',
        "dependencies": "Migration tool (Alembic, Flyway, etc.), staging DB",
        "tags": ["database", "migrations", "deployment", "safety"],
    },
    {
        "name": "Context-Aware Debugging",
        "domain": "ai-assistance",
        "trigger_desc": "User pastes an error stack trace or describes a bug",
        "workflow": "1. Do not immediately suggest a fix based on the error string.\n2. Use grep_search to find where the error is thrown.\n3. Read the surrounding 50 lines to understand state.\n4. Check git history to see when the code was introduced.\n5. Propose a targeted, localized fix.",
        "pitfalls": "Hallucinating a fix for a framework version the user isn't actually using; guessing variable types without checking definitions.",
        "key_files": '[]',
        "dependencies": "grep_search, view_file",
        "tags": ["debugging", "workflow", "ai-assistant", "investigation"],
    },
    {
        "name": "Engram Committee-Driven Workflow",
        "domain": "architecture",
        "trigger_desc": "When given a complex engineering task requiring architectural decisions",
        "workflow": "1. Initialize session using 'engram add session'\n2. Route reasoning to virtual personas (Analyst, Researcher, Skeptic, Archivist)\n3. Persist outputs using 'engram add transcript'\n4. Log key technical decisions using 'engram add decision'\n5. Present structured Facilitator summary to the user.",
        "pitfalls": "Skipping session initialization; forgetting to log decisions; acting as a single-agent solver.",
        "key_files": '["antigravity-skills/engram-committee-workflow.md", "cursor-rules/engram-committee.mdc"]',
        "dependencies": "Engram CLI",
        "tags": ["committee", "workflow", "mcp", "architecture"],
    },
    {
        "name": "Caveman Mode",
        "domain": "communication",
        "trigger_desc": "User wants ultra-compressed communication to save tokens",
        "workflow": "1. Activate 'Caveman' system prompt.\n2. Respond terse like smart caveman.\n3. Keep technical terms exact.\n4. Drop articles and filler words.",
        "pitfalls": "Losing clarity in complex instructions; dropping important URLs or code blocks.",
        "key_files": '[]',
        "dependencies": "src/compression.py",
        "tags": ["caveman", "token-efficiency", "compression"],
    },
    {
        "name": "Caveman Commit",
        "domain": "git",
        "trigger_desc": "Generating a git commit message",
        "workflow": "1. Analyze staged changes.\n2. Write terse, exact commit message in Conventional Commits format.\n3. Why over what.\n4. Body only for non-obvious context.",
        "pitfalls": "Over-compressing breaking changes; omitting linked issues.",
        "key_files": '[]',
        "dependencies": "git",
        "tags": ["git", "commit", "terse", "caveman"],
    },
    {
        "name": "Caveman Review",
        "domain": "code-review",
        "trigger_desc": "Reviewing a Pull Request or diff",
        "workflow": "1. Analyze the diff for bugs, risks, or nits.\n2. Each finding is one line: L<line>: <problem>. <fix>.\n3. No throat-clearing or pleasantries.",
        "pitfalls": "Missing security nuances due to brevity.",
        "key_files": '[]',
        "dependencies": "grep_search, view_file",
        "tags": ["code-review", "terse", "caveman"],
    },
]

SEED_PROMPTS = [
    {
        "name": "Caveman Protocol",
        "role": "expert-compressor",
        "domain": "communication",
        "description": "System prompt for ultra-terse, token-efficient communication.",
        "prompt_text": "Respond terse like smart caveman. All technical substance stay. Only fluff die. Intensity levels: lite, full, ultra. Drop articles, filler, pleasantries, hedging. [thing] [action] [reason]. [next step]. Keep code unchanged.",
        "best_for": "Saving tokens in long chat sessions.",
        "tags": ["caveman", "system-prompt", "efficiency"],
    }
]

SEED_CONVERSATIONS = [
    {
        "conversation_id": "conv-perf-opt-001",
        "title": "Optimizing API Performance",
        "date": "2026-04-10",
        "domain": "backend",
        "tasks_completed": "Analyzed slow endpoint; identified N+1 query issue; refactored ORM queries to use batching; added load test.",
        "key_decisions": "Use explicit prefetching for all list endpoints going forward; implement APM tracing.",
        "mistakes_summary": "Unbatched database queries inside a loop.",
        "skills_extracted": "None",
        "tags": ["backend", "performance", "optimization", "database"],
    },
    {
        "conversation_id": "conv-ui-debug-003",
        "title": "Refactoring User Authentication Flow",
        "date": "2026-04-15",
        "domain": "frontend",
        "tasks_completed": "Migrated auth context to Redux; fixed infinite render loops; handled race conditions in login requests.",
        "key_decisions": "Use AbortController for all authentication fetch requests; strict ESLint hooks rules.",
        "mistakes_summary": "Missing dependency array in useEffect.",
        "skills_extracted": "None",
        "tags": ["react", "frontend", "authentication", "refactoring"],
    },
]


def seed_database(db_path=None): # Seed initial memory data (v2)
    """Populate the database with existing data from conversation history."""
    init_db(db_path)

    with get_connection(db_path) as conn:
        # Check if already seeded
        count = conn.execute("SELECT COUNT(*) as c FROM mistakes").fetchone()["c"]
        if count > 0:
            print(f"Database already has {count} mistakes. Skipping seed.")
            return

        print("Seeding database...")

        # Seed mistakes
        for m in SEED_MISTAKES:
            cursor = conn.execute(
                """INSERT INTO mistakes (date, context, mistake, root_cause, fix, prevention, conversation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    m["date"],
                    m["context"],
                    m["mistake"],
                    m["root_cause"],
                    m["fix"],
                    m["prevention"],
                    m["conversation_id"],
                ),
            )
            mid = cursor.lastrowid
            link_tags(conn, "mistake", mid, m["tags"])
            content = f"{m['context']} | {m['mistake']} | {m['root_cause']} | {m['fix']} | {m['prevention']}"
            index_in_fts(conn, "mistake", mid, m["mistake"][:80], content, m["tags"])

        print(f"  ✓ {len(SEED_MISTAKES)} mistakes")

        # Seed patterns
        for p in SEED_PATTERNS:
            cursor = conn.execute(
                """INSERT INTO patterns (name, symptoms, root_cause, standard_fix)
                   VALUES (?, ?, ?, ?)""",
                (p["name"], p["symptoms"], p["root_cause"], p["standard_fix"]),
            )
            pid = cursor.lastrowid
            link_tags(conn, "pattern", pid, p["tags"])
            content = f"{p['symptoms']} | {p['root_cause']} | {p['standard_fix']}"
            index_in_fts(conn, "pattern", pid, p["name"], content, p["tags"])
            for conv_id, date, notes in p.get("occurrences", []):
                conn.execute(
                    "INSERT INTO pattern_occurrences (pattern_id, conversation_id, date, notes) VALUES (?, ?, ?, ?)",
                    (pid, conv_id, date, notes),
                )

        print(f"  ✓ {len(SEED_PATTERNS)} patterns")

        # Seed skills
        for s in SEED_SKILLS:
            cursor = conn.execute(
                """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    s["name"],
                    s["domain"],
                    s["trigger_desc"],
                    s["workflow"],
                    s["pitfalls"],
                    s["key_files"],
                    s["dependencies"],
                ),
            )
            sid = cursor.lastrowid
            link_tags(conn, "skill", sid, s["tags"])
            content = f"{s['trigger_desc']} | {s['workflow']} | {s['pitfalls']}"
            index_in_fts(conn, "skill", sid, s["name"], content, s["tags"])

        print(f"  ✓ {len(SEED_SKILLS)} skills")

        # Seed conversations
        for c in SEED_CONVERSATIONS:
            cursor = conn.execute(
                """INSERT INTO conversations (conversation_id, title, date, domain, tasks_completed, key_decisions, mistakes_summary, skills_extracted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    c["conversation_id"],
                    c["title"],
                    c["date"],
                    c["domain"],
                    c["tasks_completed"],
                    c["key_decisions"],
                    c["mistakes_summary"],
                    c["skills_extracted"],
                ),
            )
            cid = cursor.lastrowid
            link_tags(conn, "conversation", cid, c["tags"])
            content = f"{c['tasks_completed']} | {c['key_decisions']} | {c['mistakes_summary']}"
            index_in_fts(conn, "conversation", cid, c["title"], content, c["tags"])

        print(f"  ✓ {len(SEED_CONVERSATIONS)} conversations")

        # Seed prompts
        for p in SEED_PROMPTS:
            cursor = conn.execute(
                """INSERT INTO prompts (name, role, domain, description, prompt_text, best_for)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    p["name"],
                    p["role"],
                    p["domain"],
                    p["description"],
                    p["prompt_text"],
                    p["best_for"],
                ),
            )
            pid = cursor.lastrowid
            link_tags(conn, "prompt", pid, p["tags"])
            content = f"{p['role']} | {p['description']} | {p['best_for']} | {p['prompt_text'][:500]}"
            index_in_fts(conn, "prompt", pid, p["name"], content, p["tags"])

        print(f"  ✓ {len(SEED_PROMPTS)} prompts")
        print("Done!")


if __name__ == "__main__":
    seed_database()
