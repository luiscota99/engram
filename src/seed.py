"""
Seed module — populates the database with existing data from flat-file KIs.
Run once to migrate from markdown-based memory to SQLite.
"""

from .database import get_connection, init_db, link_tags, index_in_fts


SEED_MISTAKES = [
    {
        "date": "2026-04-18",
        "context": "Updating LinkedIn profile sections via browser subagent",
        "mistake": "Browser subagent invented content — changed '6 years' to '8+ years', made up job titles, merged separate companies into one entry",
        "root_cause": "Browser subagents hallucinate when given loose prompts. Optimized for what looked better rather than following exact content",
        "fix": "Manually reviewed each section via screenshots. Rewrote About section, deleted incorrect entries, re-added with exact specified content",
        "prevention": "Always verify subagent output against prepared content. Use exact copy-paste text in subagent prompts. Review screenshots after every action",
        "conversation_id": "dde56edf-1eb3-4887-a9a4-02dca808dd4c",
        "tags": ["browser-subagent", "linkedin", "content-accuracy", "hallucination"],
    },
    {
        "date": "2026-04-18",
        "context": "Building make_proxies.py to create print-ready Pokemon proxy cards",
        "mistake": "When combining upscaling + frame overlay into unified script, forgot to port color bleed logic from add_bleed.py",
        "root_cause": "Combining multiple scripts without reviewing all features. Frame overlay was the exciting new feature and overshadowed existing bleed requirement",
        "fix": "Injected the same color-scanning and flood-fill logic into make_proxies.py after frame compositing step",
        "prevention": "When unifying scripts, explicitly list ALL features from each source as a checklist before writing the combined version",
        "conversation_id": "da2c45e6-1df8-4a66-ad50-73d7cff11f85",
        "tags": ["python", "image-processing", "script-unification", "feature-loss"],
    },
    {
        "date": "2026-04-18",
        "context": "Overlaying transparent Frame.png on card images for print bleed",
        "mistake": "After compositing, tiny black outlines remained at rounded corners of the card",
        "root_cause": "Two compounding issues: (1) original card scan had black square corners, (2) Frame.png has anti-aliased semi-transparent inner edges — flood-fill stops at semi-transparent pixels",
        "fix": "Rewrote pipeline: flood-fill card corners first → tint Frame.png to match border color using alpha mask → composite",
        "prevention": "When compositing with alpha channels and rounded edges, tint the overlay to match target color using alpha mask. Anti-aliased edges require color-aware compositing",
        "conversation_id": "da2c45e6-1df8-4a66-ad50-73d7cff11f85",
        "tags": ["python", "pillow", "alpha-compositing", "image-processing", "anti-aliasing"],
    },
    {
        "date": "2026-04-18",
        "context": "Created HTML/CSS CV for print",
        "mistake": "Initial CSS used generous spacing that looked good on screen but didn't fit on a single A4 page",
        "root_cause": "Designed for screen aesthetics without testing print constraints. Missing @page rules and A4 container sizing",
        "fix": "Compacted spacing, reduced fonts ~15-20%, set container to A4 (210mm x 297mm), added @page print rules",
        "prevention": "For print-targeted documents, start from paper constraints first (@page { size: A4 }, container 210mm). Design within bounds",
        "conversation_id": "dde56edf-1eb3-4887-a9a4-02dca808dd4c",
        "tags": ["css", "print-layout", "a4", "cv-design"],
    },
    {
        "date": "2026-04-18",
        "context": "Fetching energy cards from Call of Legends expansion",
        "mistake": "First API query used the marketing name directly, returned no results",
        "root_cause": "Pokemon TCG API uses internal set IDs (col1) that differ from marketing names (Call of Legends)",
        "fix": "Queried /v2/sets first to find correct set ID, then used that ID in card query",
        "prevention": "Always look up resource ID from listing endpoint first. Never assume API parameter matches display name",
        "conversation_id": "da2c45e6-1df8-4a66-ad50-73d7cff11f85",
        "tags": ["api", "pokemon-tcg", "parameter-mismatch"],
    },
]

SEED_PATTERNS = [
    {
        "name": "Alpha Compositing Edge Artifacts",
        "symptoms": "Dark fringes, black outlines, or color bleeding at edges where images with transparency are composited",
        "root_cause": "Anti-aliased edges have semi-transparent pixels that blend with wrong background color (usually black from RGBA default)",
        "standard_fix": "Tint the overlay image to match target background color using its alpha channel as a mask",
        "tags": ["image-processing", "pillow", "alpha-compositing", "anti-aliasing"],
        "occurrences": [("da2c45e6-1df8-4a66-ad50-73d7cff11f85", "2026-04-18", "Frame overlay on energy cards")],
    },
    {
        "name": "Feature Loss During Script Unification",
        "symptoms": "Combined script works but is missing functionality from one of the source scripts",
        "root_cause": "When merging scripts, exciting new features get attention while existing features are overlooked",
        "standard_fix": "Enumerate ALL features from each source as a checklist before writing combined version",
        "tags": ["script-unification", "feature-loss", "python"],
        "occurrences": [("da2c45e6-1df8-4a66-ad50-73d7cff11f85", "2026-04-18", "add_bleed.py logic missing from make_proxies.py")],
    },
    {
        "name": "Browser Subagent Content Hallucination",
        "symptoms": "Text on web form is different from specified. Numbers, titles, or descriptions are improved or fabricated",
        "root_cause": "Browser subagents have limited context and may optimize for appearance rather than following exact instructions",
        "standard_fix": "Provide exact copy-paste text in prompts. Verify every action via screenshot before proceeding",
        "tags": ["browser-subagent", "hallucination", "content-accuracy"],
        "occurrences": [("dde56edf-1eb3-4887-a9a4-02dca808dd4c", "2026-04-18", "LinkedIn experience entries and About section modified")],
    },
    {
        "name": "API Parameter Name != Display Name",
        "symptoms": "API query returns empty results when using human-readable name",
        "root_cause": "APIs use internal IDs/slugs/codes that differ from marketing names. Spaces and case sensitivity compound the problem",
        "standard_fix": "Look up resource ID from a listing/search endpoint first, then use returned ID in subsequent queries",
        "tags": ["api", "parameter-mismatch", "lookup-first"],
        "occurrences": [("da2c45e6-1df8-4a66-ad50-73d7cff11f85", "2026-04-18", "Call of Legends vs col1")],
    },
    {
        "name": "Print Layout Overflow",
        "symptoms": "HTML/CSS looks fine on screen but overflows target paper size when printed",
        "root_cause": "Designing for screen without constraining to physical dimensions. Missing @page CSS rules",
        "standard_fix": "Start from paper constraints (A4: 210mm x 297mm). Set @page { size: A4 } and container from the start",
        "tags": ["css", "print-layout", "a4"],
        "occurrences": [("dde56edf-1eb3-4887-a9a4-02dca808dd4c", "2026-04-18", "CV design")],
    },
]

SEED_SKILLS = [
    {
        "name": "Pokemon Proxy Pipeline",
        "domain": "image-processing",
        "trigger_desc": "User wants print-ready Pokemon TCG proxy cards from any expansion",
        "workflow": "1. Fetch card images from pokemontcg.io (set ID lookup → card query → HD download)\n2. Upscale to 1995x2799 using Lanczos\n3. Apply color bleed (sample border color → floodfill black edges)\n4. Overlay Frame.png with color-tinted alpha compositing\n5. Save at 800 DPI as PNG",
        "pitfalls": "API set IDs != marketing names; Frame alpha edges cause black outlines if not tinted; Must floodfill card corners BEFORE frame; Sample color 15px inward to avoid anti-aliased edge",
        "key_files": '["~/Desktop/luismi/create printable Proxy/make_proxies.py", "~/Desktop/luismi/add_bleed.py", "~/Desktop/luismi/upscale_energies.py"]',
        "dependencies": "Python 3, Pillow, pokemontcg.io API key",
        "tags": ["python", "pillow", "pokemon-tcg", "print-production", "image-processing"],
    },
    {
        "name": "LinkedIn Profile Automation",
        "domain": "career",
        "trigger_desc": "User wants to optimize, audit, or update their LinkedIn profile",
        "workflow": "1. Audit current profile via browser subagent\n2. Gather career info from user\n3. Craft ALL content in text artifact FIRST\n4. Update one section at a time, verify each via screenshot\n5. Generate assets (banner, CV)",
        "pitfalls": "Browser subagents hallucinate content — provide exact text; LinkedIn modals appear frequently; Rate limiting after many actions; For CVs, start from A4 constraints",
        "key_files": '["~/Desktop/luismi/cv/index.html", "~/Desktop/luismi/cv/style.css"]',
        "dependencies": "Browser access, LinkedIn login",
        "tags": ["linkedin", "career", "browser-automation", "cv"],
    },
]

SEED_CONVERSATIONS = [
    {
        "conversation_id": "da2c45e6-1df8-4a66-ad50-73d7cff11f85",
        "title": "Sourcing High-Definition Pokemon Cards",
        "date": "2026-04-18",
        "domain": "image-processing",
        "tasks_completed": "Fetched energy cards via API; built upscaling script; built color bleed script; generated custom Lucario card; built unified proxy pipeline; fixed alpha compositing; set up 800 DPI; created gravity alias",
        "key_decisions": "Target 1995x2799 (800 DPI); Lanczos resampling; tint frame approach over flood-fill",
        "mistakes_summary": "API set ID mismatch; forgot color bleed in unified script; black corner outlines from untinted alpha",
        "skills_extracted": "Pokemon Proxy Pipeline",
        "tags": ["python", "pillow", "image-processing", "pokemon-tcg", "api", "print-production"],
    },
    {
        "conversation_id": "dde56edf-1eb3-4887-a9a4-02dca808dd4c",
        "title": "Optimizing LinkedIn Profile Content",
        "date": "2026-04-18",
        "domain": "career",
        "tasks_completed": "Audited blank profile; gathered career info; crafted headline/about/experience; updated via browser; fixed hallucinated content; added education/skills; created HTML/CSS CV; fixed A4 overflow",
        "key_decisions": "Senior Fullstack Engineer positioning; remote US/Canada/Mexico target; HTML+CSS CV format",
        "mistakes_summary": "Browser subagent hallucinated content; CV didn't fit A4; browser rate-limited",
        "skills_extracted": "LinkedIn Profile Automation",
        "tags": ["linkedin", "career", "browser-automation", "html", "css", "cv"],
    },
    {
        "conversation_id": "9daa751e-30ca-4372-89dc-7051da381ced",
        "title": "Setting Up Cursor Orchestration + Memory System",
        "date": "2026-04-19",
        "domain": "knowledge-management",
        "tasks_completed": "Cloned LLM-Prompts and ks-cursor-orchestrator repos; created KIs for both; designed persistent memory system; built SQLite memory database with Docker",
        "key_decisions": "KI-based then SQLite-backed memory; automatic retrospectives; Docker for portability",
        "mistakes_summary": "None significant",
        "skills_extracted": "None (infrastructure work)",
        "tags": ["cursor", "knowledge-management", "agentic", "memory-system", "docker", "sqlite"],
    },
]


def seed_database(db_path=None):
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
                (m["date"], m["context"], m["mistake"], m["root_cause"], m["fix"], m["prevention"], m["conversation_id"]),
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
                (s["name"], s["domain"], s["trigger_desc"], s["workflow"], s["pitfalls"], s["key_files"], s["dependencies"]),
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
                (c["conversation_id"], c["title"], c["date"], c["domain"], c["tasks_completed"], c["key_decisions"], c["mistakes_summary"], c["skills_extracted"]),
            )
            cid = cursor.lastrowid
            link_tags(conn, "conversation", cid, c["tags"])
            content = f"{c['tasks_completed']} | {c['key_decisions']} | {c['mistakes_summary']}"
            index_in_fts(conn, "conversation", cid, c["title"], content, c["tags"])

        print(f"  ✓ {len(SEED_CONVERSATIONS)} conversations")
        print("Done!")


if __name__ == "__main__":
    seed_database()
