#!/usr/bin/env python3
"""
Engram MCP Server — Model Context Protocol interface for persistent memory.
Exposes memory operations as tools over stdio JSON-RPC transport.

This server works with Cursor, Claude Desktop, and any MCP-compatible client.
No external dependencies — implements the MCP protocol directly.

Usage in Cursor MCP config (~/.cursor/mcp.json):
{
  "mcpServers": {
    "engram": {
      "command": "python3",
      "args": ["/path/to/engram/src/mcp_server.py"],
      "enabled": true,
      "timeout": 30
    }
  }
}
"""

import json
import os
import sys
import traceback

# Ensure our package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import get_connection, get_tags_for_item, index_in_fts, init_db, link_tags
from src.search import get_recent, get_stats
from src.search import search as memory_search

# ── MCP Protocol Constants ──────────────────────────────────────────

SERVER_NAME = "engram"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"


# ── Tool Definitions ────────────────────────────────────────────────

TOOLS = [
    {
        "name": "memory_search",
        "description": "Search across all memory (mistakes, patterns, skills, conversations) using full-text search. Use this to find relevant context, similar issues, or applicable skills before starting work.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text search query (e.g., 'alpha compositing', 'API parameter mismatch')",
                },
                "type": {
                    "type": "string",
                    "enum": ["mistake", "pattern", "skill", "conversation"],
                    "description": "Optional: filter results to a specific memory type",
                },
                "tags": {
                    "type": "string",
                    "description": "Optional: comma-separated tags to filter by (e.g., 'python,pillow')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default: 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_recent",
        "description": "Get the most recent memory entries. Use at session start to recall recent context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of recent entries to return (default: 5)",
                    "default": 5,
                },
                "type": {
                    "type": "string",
                    "enum": ["mistake", "pattern", "skill", "conversation"],
                    "description": "Optional: filter to a specific type",
                },
            },
        },
    },
    {
        "name": "memory_add_mistake",
        "description": "Log a mistake with root cause analysis. Use during retrospectives when an error or retry occurred.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Date of the mistake (YYYY-MM-DD)"},
                "context": {
                    "type": "string",
                    "description": "What you were doing when the mistake happened",
                },
                "mistake": {"type": "string", "description": "What went wrong"},
                "root_cause": {"type": "string", "description": "Why it happened"},
                "fix": {"type": "string", "description": "How it was resolved"},
                "prevention": {"type": "string", "description": "How to avoid it next time"},
                "conversation_id": {
                    "type": "string",
                    "description": "ID of the conversation where this occurred",
                },
                "tags": {"type": "string", "description": "Comma-separated tags"},
            },
            "required": ["date", "context", "mistake", "fix"],
        },
    },
    {
        "name": "memory_add_pattern",
        "description": "Log a recurring issue pattern with its standard solution. Use when you notice the same type of problem appearing across sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Pattern name (e.g., 'Alpha Compositing Edge Artifacts')",
                },
                "symptoms": {"type": "string", "description": "What the problem looks like"},
                "root_cause": {"type": "string", "description": "Why it typically happens"},
                "standard_fix": {"type": "string", "description": "What usually resolves it"},
                "tags": {"type": "string", "description": "Comma-separated tags"},
            },
            "required": ["name", "symptoms", "root_cause", "standard_fix"],
        },
    },
    {
        "name": "memory_add_skill",
        "description": "Log a reusable workflow/skill extracted from a completed task. Use when a repeatable multi-step process has been successfully executed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name (e.g., 'Pokemon Proxy Pipeline')",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain area (e.g., 'image-processing', 'career', 'devops')",
                },
                "trigger": {"type": "string", "description": "When to use this skill"},
                "workflow": {"type": "string", "description": "Step-by-step workflow (markdown)"},
                "pitfalls": {"type": "string", "description": "Known issues and gotchas"},
                "key_files": {"type": "string", "description": "JSON array of relevant file paths"},
                "dependencies": {
                    "type": "string",
                    "description": "What's needed to run this workflow",
                },
                "tags": {"type": "string", "description": "Comma-separated tags"},
            },
            "required": ["name", "domain", "trigger", "workflow"],
        },
    },
    {
        "name": "memory_add_conversation",
        "description": "Log a conversation summary at session end. Captures tasks, decisions, and mistakes for cross-session continuity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Unique conversation ID"},
                "title": {"type": "string", "description": "Descriptive title"},
                "date": {"type": "string", "description": "Date (YYYY-MM-DD)"},
                "domain": {
                    "type": "string",
                    "description": "Primary domain (e.g., 'image-processing')",
                },
                "tasks_completed": {"type": "string", "description": "What was accomplished"},
                "key_decisions": {"type": "string", "description": "Important choices made"},
                "mistakes_summary": {"type": "string", "description": "What went wrong"},
                "skills_extracted": {
                    "type": "string",
                    "description": "Skills created from this session",
                },
                "tags": {"type": "string", "description": "Comma-separated tags"},
            },
            "required": ["conversation_id", "title", "date", "domain"],
        },
    },
    {
        "name": "memory_add_prompt",
        "description": "Store a reusable LLM system prompt for specialized tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Prompt name (e.g., 'Log Analyzer')"},
                "role": {"type": "string", "description": "What role/persona the prompt creates"},
                "domain": {
                    "type": "string",
                    "description": "Domain area (e.g., 'debugging', 'architecture')",
                },
                "description": {"type": "string", "description": "What the prompt does"},
                "prompt_text": {
                    "type": "string",
                    "description": "The full text of the system prompt",
                },
                "best_for": {"type": "string", "description": "When to use this prompt"},
                "tags": {"type": "string", "description": "Comma-separated tags"},
            },
            "required": ["name", "role", "domain", "description", "prompt_text"],
        },
    },
    {
        "name": "memory_list",
        "description": "List all entries of a specific type.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["mistakes", "patterns", "skills", "conversations", "prompts"],
                    "description": "Type of entries to list",
                }
            },
            "required": ["type"],
        },
    },
    {
        "name": "memory_stats",
        "description": "Get database statistics — counts of each memory type.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ── Tool Handlers ───────────────────────────────────────────────────


def handle_memory_search(args):
    query = args.get("query", "")
    item_type = args.get("type")
    tags = args.get("tags", "").split(",") if args.get("tags") else None
    limit = args.get("limit", 10)
    results = memory_search(query, item_type=item_type, tags=tags, limit=limit)
    if not results:
        return "No results found."
    lines = []
    for r in results:
        lines.append(f"[{r['item_type'].upper()}] {r['title']}")
        if r["snippet"]:
            lines.append(f"  {r['snippet'][:150]}")
        if r["tags"]:
            lines.append(f"  tags: {r['tags']}")
        lines.append("")
    return "\n".join(lines)


def handle_memory_recent(args):
    count = args.get("count", 5)
    item_type = args.get("type")
    results = get_recent(limit=count, item_type=item_type)
    if not results:
        return "No entries yet."
    lines = []
    for r in results:
        lines.append(f"[{r['item_type'].upper()}] {r['title']}")
        if r.get("tags"):
            lines.append(f"  tags: {r['tags']}")
    return "\n".join(lines)


def handle_memory_add_mistake(args):
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO mistakes (date, context, mistake, root_cause, fix, prevention, conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                args["date"],
                args["context"],
                args["mistake"],
                args.get("root_cause"),
                args["fix"],
                args.get("prevention"),
                args.get("conversation_id"),
            ),
        )
        mid = cursor.lastrowid
        tags = [t.strip() for t in args.get("tags", "").split(",") if t.strip()]
        link_tags(conn, "mistake", mid, tags)
        content = (
            f"{args['context']} | {args['mistake']} | {args.get('root_cause', '')} | {args['fix']}"
        )
        index_in_fts(conn, "mistake", mid, args["mistake"][:80], content, tags)
    return f"Mistake #{mid} logged successfully."


def handle_memory_add_pattern(args):
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO patterns (name, symptoms, root_cause, standard_fix)
               VALUES (?, ?, ?, ?)""",
            (args["name"], args["symptoms"], args["root_cause"], args["standard_fix"]),
        )
        pid = cursor.lastrowid
        tags = [t.strip() for t in args.get("tags", "").split(",") if t.strip()]
        link_tags(conn, "pattern", pid, tags)
        content = f"{args['symptoms']} | {args['root_cause']} | {args['standard_fix']}"
        index_in_fts(conn, "pattern", pid, args["name"], content, tags)
    return f"Pattern #{pid} '{args['name']}' logged successfully."


def handle_memory_add_skill(args):
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                args["name"],
                args["domain"],
                args["trigger"],
                args["workflow"],
                args.get("pitfalls"),
                args.get("key_files"),
                args.get("dependencies"),
            ),
        )
        sid = cursor.lastrowid
        tags = [t.strip() for t in args.get("tags", "").split(",") if t.strip()]
        link_tags(conn, "skill", sid, tags)
        content = f"{args['trigger']} | {args['workflow']} | {args.get('pitfalls', '')}"
        index_in_fts(conn, "skill", sid, args["name"], content, tags)
    return f"Skill #{sid} '{args['name']}' logged successfully."


def handle_memory_add_conversation(args):
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO conversations (conversation_id, title, date, domain, tasks_completed, key_decisions, mistakes_summary, skills_extracted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                args["conversation_id"],
                args["title"],
                args["date"],
                args["domain"],
                args.get("tasks_completed"),
                args.get("key_decisions"),
                args.get("mistakes_summary"),
                args.get("skills_extracted"),
            ),
        )
        cid = cursor.lastrowid
        tags = [t.strip() for t in args.get("tags", "").split(",") if t.strip()]
        link_tags(conn, "conversation", cid, tags)
        content = f"{args.get('tasks_completed', '')} | {args.get('key_decisions', '')}"
        index_in_fts(conn, "conversation", cid, args["title"], content, tags)
    return f"Conversation #{cid} '{args['title']}' logged successfully."


def handle_memory_add_prompt(args):
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO prompts (name, role, domain, description, prompt_text, best_for)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                args["name"],
                args["role"],
                args["domain"],
                args["description"],
                args["prompt_text"],
                args.get("best_for"),
            ),
        )
        pid = cursor.lastrowid
        tags = [t.strip() for t in args.get("tags", "").split(",") if t.strip()]
        link_tags(conn, "prompt", pid, tags)
        content = f"{args['role']} | {args['description']} | {args.get('best_for', '')} | {args['prompt_text'][:500]}"
        index_in_fts(conn, "prompt", pid, args["name"], content, tags)
    return f"Prompt #{pid} '{args['name']}' stored successfully."


def handle_memory_list(args):
    item_type = args["type"]
    with get_connection() as conn:
        if item_type == "mistakes":
            rows = conn.execute(
                "SELECT id, date, mistake, fix FROM mistakes ORDER BY date DESC"
            ).fetchall()
            lines = [f"Mistakes ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "mistake", r["id"])
                lines.append(f"  #{r['id']} [{r['date']}] {r['mistake'][:80]}")
                lines.append(f"    Fix: {r['fix'][:100]}")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

        elif item_type == "patterns":
            rows = conn.execute(
                "SELECT id, name, symptoms, standard_fix FROM patterns ORDER BY name"
            ).fetchall()
            lines = [f"Patterns ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "pattern", r["id"])
                lines.append(f"  {r['name']}")
                lines.append(f"    Symptoms: {r['symptoms'][:100]}")
                lines.append(f"    Fix: {r['standard_fix'][:100]}")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

        elif item_type == "skills":
            rows = conn.execute(
                "SELECT id, name, domain, trigger_desc FROM skills ORDER BY name"
            ).fetchall()
            lines = [f"Skills ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "skill", r["id"])
                lines.append(f"  {r['name']} [{r['domain']}]")
                lines.append(f"    When: {r['trigger_desc'][:100]}")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

        elif item_type == "conversations":
            rows = conn.execute(
                "SELECT id, conversation_id, title, date, domain FROM conversations ORDER BY date DESC"
            ).fetchall()
            lines = [f"Conversations ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "conversation", r["id"])
                lines.append(f"  [{r['date']}] {r['title']}")
                lines.append(f"    Domain: {r['domain']} | ID: {r['conversation_id'][:12]}...")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

        elif item_type == "prompts":
            rows = conn.execute(
                "SELECT id, name, role, domain, best_for FROM prompts ORDER BY name"
            ).fetchall()
            lines = [f"Prompts ({len(rows)}):"]
            for r in rows:
                tags = get_tags_for_item(conn, "prompt", r["id"])
                lines.append(f"  {r['name']} [{r['domain']}]")
                lines.append(f"    Role: {r['role'][:100]}")
                if r["best_for"]:
                    lines.append(f"    Best for: {r['best_for'][:100]}")
                if tags:
                    lines.append(f"    tags: {', '.join(tags)}")
            return "\n".join(lines)

    return f"Unknown type: {item_type}"


def handle_memory_stats(args):
    stats = get_stats()
    lines = [
        "Antigravity Memory Stats:",
        f"  Mistakes:      {stats['mistakes']}",
        f"  Patterns:      {stats['patterns']}",
        f"  Skills:        {stats['skills']}",
        f"  Conversations: {stats['conversations']}",
        f"  Tags:          {stats['tags']}",
        f"  FTS indexed:   {stats['fts_indexed']}",
    ]
    return "\n".join(lines)


TOOL_HANDLERS = {
    "memory_search": handle_memory_search,
    "memory_recent": handle_memory_recent,
    "memory_add_mistake": handle_memory_add_mistake,
    "memory_add_pattern": handle_memory_add_pattern,
    "memory_add_skill": handle_memory_add_skill,
    "memory_add_conversation": handle_memory_add_conversation,
    "memory_add_prompt": handle_memory_add_prompt,
    "memory_list": handle_memory_list,
    "memory_stats": handle_memory_stats,
}


# ── JSON-RPC / MCP Protocol Handler ─────────────────────────────────


def make_response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_request(msg):
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params", {})

    # Initialize
    if method == "initialize":
        return make_response(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    # Initialized notification (no response needed)
    if method == "notifications/initialized":
        return None

    # List tools
    if method == "tools/list":
        return make_response(req_id, {"tools": TOOLS})

    # Call tool
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            return make_error(req_id, -32601, f"Unknown tool: {tool_name}")

        try:
            result_text = handler(tool_args)
            return make_response(req_id, {"content": [{"type": "text", "text": result_text}]})
        except Exception as e:
            tb = traceback.format_exc()
            print(f"Error in {tool_name}: {tb}", file=sys.stderr)
            return make_response(
                req_id,
                {
                    "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                    "isError": True,
                },
            )

    # Ping
    if method == "ping":
        return make_response(req_id, {})

    # Unknown method
    if req_id is not None:
        return make_error(req_id, -32601, f"Method not found: {method}")

    return None


def run_stdio_server():
    """Run the MCP server over stdio (stdin/stdout)."""
    # Initialize DB on startup
    init_db()
    print(f"antigravity-memory MCP server started (pid={os.getpid()})", file=sys.stderr)

    # Read JSON-RPC messages from stdin, write responses to stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}", file=sys.stderr)
            continue

        response = handle_request(msg)
        if response is not None:
            out = json.dumps(response)
            sys.stdout.write(out + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
