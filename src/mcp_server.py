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
from __future__ import annotations

import json
import os
import sys
import traceback

# Allow running as `python3 src/mcp_server.py` in addition to `python3 -m src.mcp_server`
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "src"

from src.database import (
    delete_item,
    find_similar,
    get_connection,
    get_embedding_stats,
    get_item,
    get_or_create_project,
    get_session_details,
    get_tags_for_item,
    index_in_fts,
    init_db,
    link_tags,
    record_usage,
)
from src.maintenance import find_consolidation_candidates, run_gc, run_health_check
from src.merge import merge_available, merge_entries
from src.search import get_recent, get_stats
from src.search import search as memory_search
from src.workflow import (
    WorkflowViolationError,
    advance_phase,
    check_decision_allowed,
    get_session_state,
    init_session_state,
    record_role_contribution,
)

# ── MCP Protocol Constants ──────────────────────────────────────────

SERVER_NAME = "engram"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"


# ── Tool Definitions ────────────────────────────────────────────────

TOOLS = [
    {
        "name": "memory_record_usage",
        "description": "Increment the usage count for a memory item (skill, pattern, mistake). You MUST call this tool immediately after successfully utilizing a memory item to help the system mathematically boost its future search rank.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_type": {
                    "type": "string",
                    "description": "Type of item (e.g., 'skill')",
                },
                "item_id": {
                    "type": "integer",
                    "description": "ID of the item",
                },
                "success": {
                    "type": "boolean",
                    "description": "Whether the application of the item was successful",
                },
            },
            "required": ["item_type", "item_id"],
        },
    },
    {
        "name": "memory_read_item",
        "description": "Fetch the deep structured content (e.g. full workflow, exact mistake context) of a specific memory item. Use this when a memory_search returns an item that is relevant to your current task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_type": {
                    "type": "string",
                    "description": "Type of item (e.g., 'skill', 'mistake')",
                },
                "item_id": {
                    "type": "integer",
                    "description": "ID of the item to fetch",
                },
            },
            "required": ["item_type", "item_id"],
        },
    },
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
                "project_path": {
                    "type": "string",
                    "description": "Optional: current project working directory for context-aware ranking",
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
        "description": "Log a mistake with root cause analysis. CRITICAL: Before invoking this tool, you MUST draft the memory payload in a markdown block and ask the user for explicit approval. Never write to the database autonomously. SECURITY: Never store raw source code or untrusted input; summarize conceptually.",
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
        "description": "Log a recurring problem pattern. CRITICAL: Before invoking this tool, you MUST draft the memory payload in a markdown block and ask the user for explicit approval. Never write to the database autonomously. SECURITY: Never store raw source code or untrusted input; summarize conceptually. Use when you notice the same type of problem appearing across sessions.",
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
        "description": "Log a proven, reusable workflow or skill. CRITICAL: Before invoking this tool, you MUST draft the memory payload in a markdown block and ask the user for explicit approval. Never write to the database autonomously. SECURITY: Never store raw source code or untrusted input; summarize conceptually. Use when a repeatable multi-step process has been successfully executed.",
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
                "tags": {"type": "string"},
            },
            "required": ["name", "domain", "trigger_desc", "workflow"],
        },
    },
    {
        "name": "memory_consolidate_skills",
        "description": "Consolidate multiple redundant/overlapping skills into a single master skill. Use this to clean up the database when you notice bloat. You MUST draft the new master skill in a markdown block and ask for user approval before invoking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_ids_to_delete": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of old skill IDs to delete",
                },
                "new_skill_name": {
                    "type": "string",
                    "description": "Name of the new consolidated skill",
                },
                "new_skill_domain": {"type": "string"},
                "new_skill_trigger_desc": {
                    "type": "string",
                    "description": "When to use this skill",
                },
                "new_skill_workflow": {
                    "type": "string",
                    "description": "Step-by-step instructions",
                },
                "new_skill_pitfalls": {
                    "type": "string",
                    "description": "Known pitfalls or edge cases",
                },
                "new_skill_key_files": {"type": "string", "description": "Typical files modified"},
                "new_skill_dependencies": {
                    "type": "string",
                    "description": "External tools/skills required",
                },
                "new_skill_tags": {"type": "string", "description": "Comma-separated tags"},
            },
            "required": [
                "skill_ids_to_delete",
                "new_skill_name",
                "new_skill_domain",
                "new_skill_trigger_desc",
                "new_skill_workflow",
            ],
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
    {
        "name": "memory_session_review",
        "description": "MANDATORY: Call this tool at the END of every session. It returns a structured reflection checklist that forces you to reflect on what happened, what went wrong, and what was learned. You MUST draft the entries in a markdown block and present them to the user for approval before logging.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "The current conversation/session ID",
                },
                "project_path": {
                    "type": "string",
                    "description": "Current project working directory",
                },
                "tasks_completed": {
                    "type": "string",
                    "description": "What was accomplished this session",
                },
                "bugs_fixed": {
                    "type": "string",
                    "description": "Any bugs fixed — each should become a mistake entry",
                },
                "new_patterns_noticed": {
                    "type": "string",
                    "description": "Recurring issues noticed — each should become a pattern entry",
                },
                "workflows_used": {
                    "type": "string",
                    "description": "Multi-step workflows that worked — each should become a skill entry",
                },
            },
            "required": ["conversation_id", "tasks_completed"],
        },
    },
    {
        "name": "memory_init_session",
        "description": "Initialize a new Committee session. Call this at the start of any complex task to setup the session ledger.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Unique session ID (e.g., YYYY-MM-DD__NNNN)"},
                "title": {"type": "string", "description": "Descriptive title for the task"},
                "date": {"type": "string", "description": "Date (YYYY-MM-DD)"},
                "domain": {"type": "string", "description": "Primary domain (e.g., 'image-processing')"},
                "workflow_used": {"type": "string", "description": "Name of the workflow to use"}
            },
            "required": ["session_id", "title", "date", "domain"]
        }
    },
    {
        "name": "memory_add_transcript",
        "description": "Add a subagent output to the session transcript.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "role": {"type": "string", "description": "Subagent role (Facilitator, Analyst, Researcher, Skeptic, Archivist)"},
                "content": {"type": "string", "description": "The output content from the subagent"}
            },
            "required": ["session_id", "role", "content"]
        }
    },
    {
        "name": "memory_add_decision",
        "description": "Log a formal decision to the session ledger.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "decision": {"type": "string", "description": "The decision made, with tradeoffs and rationale"}
            },
            "required": ["session_id", "decision"]
        }
    },
    {
        "name": "memory_get_role",
        "description": "Retrieve the charter and heuristics for a specific subagent role.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the role (e.g., 'Analyst')"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "memory_get_session",
        "description": "Get full details of a session, including transcripts and decisions. Use this when continuing a previous session to securely load its context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The ID of the session to fetch"}
            },
            "required": ["session_id"]
        }
    },
    {
        "name": "memory_index_file",
        "description": "Index a specific file's knowledge (summary, exports, deps). CRITICAL: You must provide a concise conceptual summary of the file's purpose and logic. Use this after reading a file to persist your understanding for future sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "Project root path"},
                "file_path": {"type": "string", "description": "Relative path to the file"},
                "summary": {"type": "string", "description": "Conceptual summary of the file"},
                "exports": {"type": "string", "description": "JSON array of exported symbols"},
                "dependencies": {"type": "string", "description": "JSON array of imports/dependencies"}
            },
            "required": ["project_path", "file_path", "summary"]
        }
    },
    {
        "name": "memory_query_codebase",
        "description": "Query the persistent codebase knowledge for a project. Returns summaries of files matching the query. Use this to 'map' the project structure without re-reading all files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "Project root path"},
                "query": {"type": "string", "description": "Optional search term"}
            },
            "required": ["project_path"]
        }
    },
    {
        "name": "memory_get_stale_files",
        "description": "Find files in the project whose content has changed since they were last indexed. Returns a JSON list of stale files with their old summaries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "Project root path"}
            },
            "required": ["project_path"]
        }
    },
    {
        "name": "memory_check_workflow_state",
        "description": "Check the current phase and role requirements for a committee session. Returns which roles are still needed before advancing to the next phase.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session ID to check"}
            },
            "required": ["session_id"]
        }
    },
    {
        "name": "memory_advance_phase",
        "description": "Advance the session to the next workflow phase. Fails with an error if required roles have not yet contributed transcripts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session ID to advance"}
            },
            "required": ["session_id"]
        }
    },
    {
        "name": "memory_find_similar",
        "description": "Check if a piece of content is similar to existing memories before inserting. Use this to detect near-duplicates and decide whether to merge, skip, or add.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The content to check for similarity"},
                "item_type": {
                    "type": "string",
                    "enum": ["mistake", "pattern", "skill", "conversation", "prompt"],
                    "description": "Optional: restrict search to one type"
                },
                "threshold": {
                    "type": "number",
                    "description": "Similarity threshold 0-1 (default: 0.85)",
                    "default": 0.85
                }
            },
            "required": ["content"]
        }
    },
    {
        "name": "memory_merge_entries",
        "description": "Use an LLM to synthesize two similar memory entries into one richer entry. Present the result to the user for approval before storing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_type_a": {"type": "string", "description": "Type of entry A"},
                "item_id_a": {"type": "integer", "description": "ID of entry A"},
                "item_type_b": {"type": "string", "description": "Type of entry B"},
                "item_id_b": {"type": "integer", "description": "ID of entry B"}
            },
            "required": ["item_type_a", "item_id_a", "item_type_b", "item_id_b"]
        }
    },
    {
        "name": "memory_embedding_status",
        "description": "Get the current status of embeddings (ready, stale, pending, failed) and the active embedding model. Use to monitor model migration progress.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "memory_health",
        "description": "Get a comprehensive health report of the memory database including item stats, embedding health, index drift, GC candidates, and actionable recommendations.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "memory_suggest_consolidations",
        "description": "Find clusters of near-duplicate memories that could be merged using memory_consolidate_skills or memory_merge_entries. Returns grouped candidates by similarity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "number",
                    "description": "Similarity threshold 0-1 (default: 0.80)",
                    "default": 0.80
                },
                "item_type": {
                    "type": "string",
                    "enum": ["mistake", "pattern", "skill"],
                    "description": "Optional: restrict to one type"
                }
            }
        }
    },
    {
        "name": "memory_gc",
        "description": "Identify (dry-run) or archive unused memories older than a threshold. Always dry-run first and present results to the user before archiving.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["dry-run", "archive"],
                    "default": "dry-run",
                    "description": "dry-run reports candidates; archive soft-deletes them"
                },
                "days_unused": {
                    "type": "integer",
                    "default": 180,
                    "description": "Unused for this many days (default: 180)"
                }
            }
        }
    },
    {
        "name": "memory_export_skill",
        "description": "Export an Engram skill (or pattern) as a Cursor-compatible SKILL.md file on disk. Use when a proven workflow should become a permanent Cursor skill that persists across sessions. CRITICAL: Always ask the user for confirmation on the output path before invoking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "integer",
                    "description": "ID of the Engram skill to export"
                },
                "pattern_id": {
                    "type": "integer",
                    "description": "ID of an Engram pattern to export as a skill (use instead of skill_id)"
                },
                "output_path": {
                    "type": "string",
                    "description": "Directory to export into (default: ~/.cursor/skills/). Can also be a project path like .cursor/skills/"
                },
                "project_skill": {
                    "type": "boolean",
                    "description": "If true, export to .cursor/skills/ in the current working directory instead of the personal skills dir"
                }
            }
        }
    },
    {
        "name": "memory_sync_skills",
        "description": "Show a diff between Engram skills and a Cursor skills directory, then optionally sync them. Use to keep Engram and Cursor skills in alignment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skills_dir": {
                    "type": "string",
                    "description": "Cursor skills directory to sync with (default: ~/.cursor/skills/)"
                },
                "auto_sync": {
                    "type": "boolean",
                    "description": "If true, automatically export Engram-only skills and import Cursor-only skills"
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only show the diff without performing any writes (default: true)"
                }
            }
        }
    },
]


# ── Tool Handlers ───────────────────────────────────────────────────


def format_and_truncate_results(results):
    if not results:
        return "No results found."

    max_chars = int(os.environ.get("ENGRAM_MAX_CONTEXT_CHARS", 8000))
    lines = [
        "Found results. NOTE: These are truncated summaries to save context tokens.\n",
        "If an item looks relevant, you MUST use `memory_read_item(item_type, item_id)` to read the full context.\n\n",
    ]
    total_length = len(lines[0]) + len(lines[1])
    truncated = False

    for r in results:
        block = f"[{r['item_type'].upper()} ID: {r['item_id']}] {r['title']}\n"
        if r.get("snippet"):
            snippet = r["snippet"].replace("\n", " ")
            block += f"  Snippet: {snippet[:150]}...\n"
        if r.get("tags"):
            block += f"  Tags: {r['tags']}\n"
        block += "\n"

        if total_length + len(block) > max_chars and len(lines) > 2:
            truncated = True
            break

        lines.append(block)
        total_length += len(block)

    output = "".join(lines)
    if truncated:
        output += f"[WARNING: Truncated at {max_chars} chars. Use memory_read_item on the IDs above to read more.]\n"

    return output


def handle_memory_record_usage(args):
    item_type = args.get("item_type")
    item_id = args.get("item_id")
    success = args.get("success", True)

    if not item_type or not item_id:
        return "Error: item_type and item_id are required."

    result = record_usage(item_type, item_id, success)
    if result:
        return f"Successfully recorded usage for {item_type} ID {item_id}. Its search rank has been boosted."
    return f"Failed to record usage for {item_type} ID {item_id}."


def handle_memory_read_item(args):
    item_type = args.get("item_type")
    item_id = args.get("item_id")
    if not item_type or not item_id:
        return "Error: item_type and item_id are required."

    item = get_item(item_type, item_id)
    if not item:
        return f"Error: Could not find {item_type} with ID {item_id}."

    # Auto-track: reading the full item means you're using it
    record_usage(item_type, item_id)

    return json.dumps(item, indent=2).strip()


def handle_memory_search(args):
    query = args.get("query", "")
    item_type = args.get("type")
    tags = args.get("tags", "").split(",") if args.get("tags") else None
    limit = args.get("limit", 10)
    project_path = args.get("project_path")
    results = memory_search(query, item_type=item_type, tags=tags, limit=limit, project_path=project_path)
    if not results:
        return "No results found."
    return format_and_truncate_results(results)


def handle_memory_recent(args):
    count = args.get("count", 5)
    item_type = args.get("type")
    results = get_recent(limit=count, item_type=item_type)
    if not results:
        return "No entries yet."
    return format_and_truncate_results(results)


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


def handle_memory_consolidate_skills(args):
    with get_connection() as conn:
        # Create the new skill
        cursor = conn.execute(
            """INSERT INTO skills (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                args["new_skill_name"],
                args["new_skill_domain"],
                args["new_skill_trigger_desc"],
                args["new_skill_workflow"],
                args.get("new_skill_pitfalls"),
                args.get("new_skill_key_files"),
                args.get("new_skill_dependencies"),
            ),
        )
        sid = cursor.lastrowid
        tags = [t.strip() for t in args.get("new_skill_tags", "").split(",") if t.strip()]
        link_tags(conn, "skill", sid, tags)
        content = f"{args['new_skill_trigger_desc']} | {args['new_skill_workflow']} | {args.get('new_skill_pitfalls', '')}"
        index_in_fts(conn, "skill", sid, args["new_skill_name"], content, tags)

        # Delete old skills
        for old_id in args["skill_ids_to_delete"]:
            delete_item(conn, "skill", old_id)

    return f"Consolidated into Skill #{sid} '{args['new_skill_name']}' and deleted {len(args['skill_ids_to_delete'])} old entries."


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


def handle_memory_get_session(args):
    session_id = args.get("session_id")
    if not session_id:
        return "Error: session_id is required."

    session = get_session_details(session_id)
    if not session:
        return f"Session '{session_id}' not found."

    lines = [f"Session: {session['title']} ({session['session_id']})", f"Date: {session['date']}", f"Domain: {session['domain']}"]
    if session.get('workflow_used'):
        lines.append(f"Workflow: {session['workflow_used']}")
    lines.append("")

    if session.get('key_decisions'):
        lines.append("Key Decisions:")
        lines.append(session['key_decisions'])
        lines.append("")

    if session.get('transcripts'):
        lines.append("Transcripts:")
        for t in session['transcripts']:
            lines.append(f"[{t['role']}] {t['timestamp']}")
            lines.append(t['content'])
            lines.append("")

    return "\n".join(lines)


def handle_memory_init_session(args):
    session_id = args["session_id"]
    workflow_used = args.get("workflow_used")

    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO sessions (session_id, title, date, domain, workflow_used)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, args["title"], args["date"], args["domain"], workflow_used),
        )
        sid = cursor.lastrowid
        content = f"{args['title']} | {workflow_used or ''}"
        index_in_fts(conn, "session", sid, session_id, content, [])

    # Initialize workflow state machine for this session
    state = init_session_state(session_id, workflow_name=workflow_used)
    phase_info = ""
    if state and state.get("current_phase"):
        required = ", ".join(state["required_roles"]) or "none"
        phase_info = f" Starting phase: '{state['current_phase']}' (required roles: {required})."

    return f"Session '{session_id}' initialized successfully.{phase_info}"


def handle_memory_add_transcript(args):
    session_id = args["session_id"]
    role = args["role"]
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO session_transcripts (session_id, role, content)
               VALUES (?, ?, ?)""",
            (session_id, role, args["content"]),
        )
    # Update workflow state for this role's contribution
    state = record_role_contribution(session_id, role)
    status = ""
    if state["current_phase"]:
        if state["can_proceed"]:
            status = " All required roles have contributed. Ready to advance phase or add a decision."
        else:
            remaining = ", ".join(state["missing_roles"])
            status = f" Still waiting for: {remaining}."
    return f"Transcript entry for '{role}' added to session '{session_id}'.{status}"


def handle_memory_add_decision(args):
    session_id = args["session_id"]
    # Enforce workflow gate: all required roles must have contributed
    try:
        check_decision_allowed(session_id)
    except WorkflowViolationError as e:
        return f"WorkflowViolation: {e}"

    with get_connection() as conn:
        conn.execute(
            """UPDATE sessions SET key_decisions = IFNULL(key_decisions, '') || char(10) || ?
               WHERE session_id = ?""",
            (args["decision"], session_id),
        )
    return f"Decision added to session '{session_id}'."


def handle_memory_get_role(args):
    with get_connection() as conn:
        row = conn.execute("SELECT charter, heuristics FROM roles WHERE name = ?", (args["name"],)).fetchone()
        if not row:
            return f"Role '{args['name']}' not found in database."
        return f"Charter:\n{row['charter']}\n\nHeuristics:\n{row['heuristics']}"


def handle_memory_index_file(args):
    project_path = args["project_path"]
    file_path = args["file_path"]
    summary = args["summary"]
    exports = args.get("exports")
    deps = args.get("dependencies")

    # Use CLI logic via subprocess to ensure consistency and handle hashing
    import subprocess
    cmd = [
        "python3", "-m", "src.cli", "index-project",
        "--path", project_path,
        "--file", file_path,
        "--summary", summary
    ]
    if exports:
        cmd += ["--exports", exports]
    if deps:
        cmd += ["--deps", deps]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f"Error indexing file: {e.stderr}"


def handle_memory_query_codebase(args):
    project_path = args["project_path"]
    query = args.get("query", "")

    import subprocess
    cmd = ["python3", "-m", "src.cli", "query-codebase", "--path", project_path]
    if query:
        cmd.append(query)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f"Error querying codebase: {e.stderr}"


def handle_memory_get_stale_files(args):
    project_path = args["project_path"]

    import subprocess
    cmd = ["python3", "-m", "src.cli", "index-project", "--path", project_path, "--check"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f"Error checking for stale files: {e.stderr}"


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


def handle_memory_session_review(args):
    """Return a structured reflection prompt for end-of-session learning."""
    conversation_id = args.get("conversation_id", "unknown")
    project_path = args.get("project_path")
    tasks_completed = args.get("tasks_completed", "")
    bugs_fixed = args.get("bugs_fixed", "")
    new_patterns = args.get("new_patterns_noticed", "")
    workflows_used = args.get("workflows_used", "")

    # Register project if provided
    project_info = ""
    if project_path:
        try:
            project = get_or_create_project(project_path)
            project_info = f"\nProject: {project['name']} ({project['path']})"
        except Exception:
            pass

    # Search for similar existing entries to prevent duplicates
    similar_section = ""
    search_terms = []
    if bugs_fixed:
        search_terms.append(bugs_fixed)
    if new_patterns:
        search_terms.append(new_patterns)
    if workflows_used:
        search_terms.append(workflows_used)

    if search_terms:
        combined_query = " ".join(search_terms)[:200]
        existing = memory_search(combined_query, limit=5, project_path=project_path)
        if existing:
            similar_section = "\n\n## ⚠️ Similar Existing Entries (check for duplicates before logging):\n"
            for e in existing:
                similar_section += f"  [{e['item_type'].upper()} ID:{e['item_id']}] {e['title']}\n"

    # Build reflection prompt
    prompt = f"""# Session Retrospective — {conversation_id[:12]}
{project_info}

## Tasks Completed
{tasks_completed}

## Reflection Checklist

### 1. Mistakes to Log
{f'Bugs fixed this session: {bugs_fixed}' if bugs_fixed else 'No bugs reported.'}
→ For each bug fixed, draft a `memory_add_mistake` call with: date, context, mistake, root_cause, fix, prevention, tags
→ Present the draft to the user for approval before logging.

### 2. Patterns to Log
{f'Patterns noticed: {new_patterns}' if new_patterns else 'No new patterns reported.'}
→ For each recurring issue, draft a `memory_add_pattern` call with: name, symptoms, root_cause, standard_fix, tags
→ Search existing patterns first to avoid duplicates.

### 3. Skills to Log
{f'Workflows used: {workflows_used}' if workflows_used else 'No workflows reported.'}
→ For each multi-step workflow that succeeded, draft a `memory_add_skill` call with: name, domain, trigger, workflow, pitfalls, tags
→ If the workflow had >3 steps and could be reused, it's a strong skill candidate.

### 4. Conversation Summary
→ Draft a `memory_add_conversation` call to log this session for cross-session continuity.
{similar_section}

## Instructions
1. Draft ALL entries above in a markdown block.
2. Present them to the user for explicit approval.
3. Only after approval, call the respective memory_add_* tools.
4. Do NOT log anything without user confirmation."""

    return prompt


def handle_memory_check_workflow_state(args):
    session_id = args.get("session_id")
    if not session_id:
        return "Error: session_id is required."
    state = get_session_state(session_id)
    if not state["current_phase"]:
        return f"No workflow state found for session '{session_id}'. Call memory_init_session first."
    lines = [
        f"Session: {session_id}",
        f"Current phase: {state['current_phase']}",
        f"Required roles: {', '.join(state['required_roles']) or 'none'}",
        f"Completed roles: {', '.join(state['completed_roles']) or 'none'}",
        f"Can proceed: {'Yes' if state['can_proceed'] else 'No'}",
    ]
    if state["missing_roles"]:
        lines.append(f"⚠ Missing roles: {', '.join(state['missing_roles'])}")
    return "\n".join(lines)


def handle_memory_advance_phase(args):
    session_id = args.get("session_id")
    if not session_id:
        return "Error: session_id is required."
    try:
        state = advance_phase(session_id)
        return (
            f"Advanced to phase '{state['current_phase']}'.\n"
            f"Required roles for this phase: {', '.join(state['required_roles']) or 'none'}"
        )
    except WorkflowViolationError as e:
        return f"Workflow violation: {e}"


def handle_memory_find_similar(args):
    content = args.get("content", "")
    item_type = args.get("item_type")
    threshold = float(args.get("threshold", 0.85))
    if not content:
        return "Error: content is required."
    results = find_similar(content, item_type=item_type, threshold=threshold)
    if not results:
        return "No similar entries found above the threshold. Safe to add as new entry."
    lines = [f"Found {len(results)} similar entries (threshold: {threshold}):\n"]
    for r in results:
        lines.append(f"  [{r['item_type'].upper()} ID:{r['item_id']}] {r['title']} (similarity: {r['similarity']})")
        if r.get("snippet"):
            lines.append(f"    {r['snippet'][:120]}...")
    lines.append("\nOptions: skip | add_anyway | replace (delete old + add new) | merge (call memory_merge_entries)")
    return "\n".join(lines)


def handle_memory_merge_entries(args):
    item_type_a = args.get("item_type_a")
    item_id_a = args.get("item_id_a")
    item_type_b = args.get("item_type_b")
    item_id_b = args.get("item_id_b")

    if not all([item_type_a, item_id_a, item_type_b, item_id_b]):
        return "Error: item_type_a, item_id_a, item_type_b, item_id_b are all required."

    if not merge_available():
        return "Error: Ollama is not available. Cannot perform LLM-assisted merge."

    entry_a = get_item(item_type_a, item_id_a)
    entry_b = get_item(item_type_b, item_id_b)

    if not entry_a:
        return f"Error: {item_type_a} ID {item_id_a} not found."
    if not entry_b:
        return f"Error: {item_type_b} ID {item_id_b} not found."

    merged = merge_entries(entry_a, entry_b)
    if not merged:
        return "Merge failed: LLM did not return valid JSON. Try again or merge manually."

    return (
        f"Merged entry draft (present to user for approval before storing):\n\n"
        f"```json\n{json.dumps(merged, indent=2)}\n```\n\n"
        f"After approval:\n"
        f"1. Use the appropriate memory_add_* tool to store the merged entry\n"
        f"2. Delete old entries: memory_read_item was called — you can now delete IDs "
        f"{item_id_a} and {item_id_b} from the core tables via consolidate tool"
    )


def handle_memory_embedding_status(args):
    stats = get_embedding_stats()
    total = stats.get("total", 0)
    model = stats.get("model", "unknown")
    lines = [f"Embedding Status (model: {model}):"]
    if total > 0:
        def pct(n): return f"{100*n/total:.1f}%"
        lines.append(f"  Ready:   {stats['ready']:4d} ({pct(stats['ready'])})")
        if stats["stale"]:
            lines.append(f"  Stale:   {stats['stale']:4d} ({pct(stats['stale'])})  ← run engram reembed")
        if stats["pending"]:
            lines.append(f"  Pending: {stats['pending']:4d} ({pct(stats['pending'])})")
        if stats["failed"]:
            lines.append(f"  Failed:  {stats['failed']:4d} ({pct(stats['failed'])})")
    else:
        lines.append("  No embeddings tracked yet.")
    return "\n".join(lines)


def handle_memory_health(args):
    report = run_health_check()
    lines = ["Memory Health Report\n"]
    for itype, stats in report.get("items", {}).items():
        if stats["total"] > 0:
            lines.append(f"  {itype}: {stats['total']} total, {stats['unused_180_plus_days']} GC candidates")
    emb = report.get("embeddings", {})
    if emb:
        lines.append(f"\nEmbeddings ({emb.get('model','?')}): ready={emb.get('ready',0)}, stale={emb.get('stale',0)}, pending={emb.get('pending',0)}")
    lines.append(f"\nFTS: {report.get('fts_total',0)}, Vec: {report.get('vec_total',0)}, Drift: {report.get('vec_drift',0)}")
    lines.append(f"Orphaned tags: {report.get('orphaned_tags',0)}, GC candidates: {report.get('gc_candidates',0)}")
    recs = report.get("recommendations", [])
    if recs:
        lines.append("\nRecommendations:")
        for rec in recs:
            lines.append(f"  • {rec}")
    return "\n".join(lines)


def handle_memory_suggest_consolidations(args):
    threshold = float(args.get("threshold", 0.80))
    item_type = args.get("item_type")
    clusters = find_consolidation_candidates(
        threshold=threshold,
        item_types=[item_type] if item_type else None,
    )
    if not clusters:
        return f"No consolidation candidates found at similarity threshold {threshold}."
    lines = [f"Found {len(clusters)} consolidation candidate(s) (threshold: {threshold}):\n"]
    for i, cluster in enumerate(clusters[:20], 1):
        lines.append(f"Cluster {i} — {cluster['item_type']} (similarity: {cluster['similarity']})")
        for item in cluster["items"]:
            lines.append(f"  ID:{item['item_id']}  {item['title']}")
    return "\n".join(lines)


def handle_memory_gc(args):
    mode = args.get("mode", "dry-run")
    days = int(args.get("days_unused", 180))
    result = run_gc(mode=mode, days_unused=days)
    candidates = result["candidates"]
    if not candidates:
        return f"No GC candidates found (threshold: unused for {days}+ days)."
    lines = [f"GC {mode} — {len(candidates)} candidate(s) unused for {days}+ days:\n"]
    for c in candidates[:30]:
        lines.append(f"  [{c['item_type'].upper()} ID:{c['item_id']}] created: {c['created_at'] or 'unknown'}")
    if mode == "dry-run":
        lines.append("\nCall with mode='archive' to soft-delete these items.")
    else:
        lines.append(f"\nArchived {result['processed']} items.")
    return "\n".join(lines)


def handle_memory_export_skill(args):
    from src.export import (
        export_skills,
        render_pattern_as_skill_md,
        write_skill_file,
        slugify,
    )

    skill_id = args.get("skill_id")
    pattern_id = args.get("pattern_id")
    project_skill = args.get("project_skill", False)
    output_path = args.get("output_path")

    if output_path:
        output_dir = os.path.expanduser(output_path)
    elif project_skill:
        output_dir = os.path.join(os.getcwd(), ".cursor", "skills")
    else:
        output_dir = os.path.expanduser("~/.cursor/skills")

    if skill_id:
        results = export_skills(output_dir=output_dir, ids=[skill_id], dry_run=False)
        if not results:
            return f"Skill ID {skill_id} not found."
        r = results[0]
        if r["action"] == "created":
            return f"Skill '{r['name']}' exported to:\n  {r['path']}\n\nThe Cursor agent will discover this skill automatically on the next session."
        elif r["action"] == "skipped":
            return f"Skill '{r['name']}' already exists at:\n  {r['path']}\n\nNo changes made."
        return f"Export result: {r['action']} — {r['path']}"

    elif pattern_id:
        with get_connection() as conn:
            pattern = conn.execute(
                """SELECT p.id, p.name, p.symptoms, p.root_cause, p.standard_fix,
                          COUNT(po.id) as occurrences
                   FROM patterns p
                   LEFT JOIN pattern_occurrences po ON po.pattern_id = p.id
                   WHERE p.id = ?
                   GROUP BY p.id""",
                (pattern_id,)
            ).fetchone()

        if not pattern:
            return f"Pattern ID {pattern_id} not found."

        pattern = dict(pattern)
        from src.database import get_tags_for_item
        with get_connection() as conn:
            tags = get_tags_for_item(conn, "pattern", pattern["id"])

        from src.export import render_pattern_as_skill_md, write_skill_file, slugify
        content = render_pattern_as_skill_md(pattern, tags)
        slug = slugify(pattern["name"])
        path = write_skill_file(output_dir, slug, content)
        return f"Pattern '{pattern['name']}' exported as skill to:\n  {path}"

    return "Error: provide either skill_id or pattern_id."


def handle_memory_sync_skills(args):
    from src.export import compute_sync_diff, export_skills, import_cursor_skills_dir

    skills_dir = os.path.expanduser(args.get("skills_dir") or "~/.cursor/skills")
    dry_run = args.get("dry_run", True)
    auto_sync = args.get("auto_sync", False)

    diff = compute_sync_diff(skills_dir)
    only_engram = diff["only_in_engram"]
    only_cursor = diff["only_in_cursor"]
    in_both = diff["in_both"]

    lines = [
        f"Engram ↔ Cursor Skill Sync — {skills_dir}\n",
        f"  In both:        {len(in_both)}",
        f"  Only in Engram: {len(only_engram)}  (can export)",
        f"  Only in Cursor: {len(only_cursor)}  (can import)",
        "",
    ]

    if only_engram:
        lines.append("Skills in Engram not yet in Cursor (→ export):")
        for slug, skill in sorted(only_engram.items()):
            lines.append(f"  [SKILL ID:{skill['id']}] {skill['name']} [{skill['domain']}] usage:{skill.get('usage_count', 0)}")
        lines.append("")

    if only_cursor:
        lines.append("Skills in Cursor not yet in Engram (→ import):")
        for slug, path in sorted(only_cursor.items()):
            lines.append(f"  {slug}  ({path})")
        lines.append("")

    if auto_sync and not dry_run:
        # Export Engram-only skills
        if only_engram:
            engram_ids = [s["id"] for s in only_engram.values()]
            export_results = export_skills(output_dir=skills_dir, ids=engram_ids, dry_run=False)
            created = [r for r in export_results if r["action"] == "created"]
            lines.append(f"Exported {len(created)} skill(s) to Cursor:")
            for r in created:
                lines.append(f"  ✓ {r['name']} → {r['path']}")
            lines.append("")

        # Import Cursor-only skills
        if only_cursor:
            import_results = import_cursor_skills_dir(skills_dir, dry_run=False)
            imported = [r for r in import_results if r.get("action") == "imported"]
            lines.append(f"Imported {len(imported)} skill(s) into Engram:")
            for r in imported:
                lines.append(f"  ✓ {r['name']} (Skill #{r['id']})")
    elif dry_run:
        lines.append("Dry-run mode: no changes made. Set dry_run=false and auto_sync=true to sync.")

    return "\n".join(lines)


TOOL_HANDLERS = {
    "memory_record_usage": handle_memory_record_usage,
    "memory_read_item": handle_memory_read_item,
    "memory_search": handle_memory_search,
    "memory_recent": handle_memory_recent,
    "memory_add_mistake": handle_memory_add_mistake,
    "memory_add_pattern": handle_memory_add_pattern,
    "memory_add_skill": handle_memory_add_skill,
    "memory_consolidate_skills": handle_memory_consolidate_skills,
    "memory_add_conversation": handle_memory_add_conversation,
    "memory_add_prompt": handle_memory_add_prompt,
    "memory_list": handle_memory_list,
    "memory_stats": handle_memory_stats,
    "memory_session_review": handle_memory_session_review,
    "memory_init_session": handle_memory_init_session,
    "memory_add_transcript": handle_memory_add_transcript,
    "memory_add_decision": handle_memory_add_decision,
    "memory_get_role": handle_memory_get_role,
    "memory_get_session": handle_memory_get_session,
    "memory_index_file": handle_memory_index_file,
    "memory_query_codebase": handle_memory_query_codebase,
    "memory_get_stale_files": handle_memory_get_stale_files,
    "memory_check_workflow_state": handle_memory_check_workflow_state,
    "memory_advance_phase": handle_memory_advance_phase,
    "memory_find_similar": handle_memory_find_similar,
    "memory_merge_entries": handle_memory_merge_entries,
    "memory_embedding_status": handle_memory_embedding_status,
    "memory_health": handle_memory_health,
    "memory_suggest_consolidations": handle_memory_suggest_consolidations,
    "memory_gc": handle_memory_gc,
    "memory_export_skill": handle_memory_export_skill,
    "memory_sync_skills": handle_memory_sync_skills,
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
