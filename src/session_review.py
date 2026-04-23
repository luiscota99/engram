"""
Session retrospective prompt — shared by MCP `memory_session_review` and CLI `engram session-review`.
"""

from __future__ import annotations

from .capture import SESSION_INFLUENCE_PROMPT
from .database import get_or_create_project
from .search import search as memory_search


def build_session_review_prompt(
    conversation_id: str = "unknown",
    project_path: str | None = None,
    tasks_completed: str = "",
    bugs_fixed: str = "",
    new_patterns_noticed: str = "",
    workflows_used: str = "",
) -> str:
    """Build the same markdown retrospective as the MCP `memory_session_review` tool.

    project_path:
        If set, registers/uses project affinity and scopes duplicate search.
    """
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
    search_terms: list[str] = []
    if bugs_fixed:
        search_terms.append(bugs_fixed)
    if new_patterns_noticed:
        search_terms.append(new_patterns_noticed)
    if workflows_used:
        search_terms.append(workflows_used)

    if search_terms:
        combined_query = " ".join(search_terms)[:200]
        existing = memory_search(
            combined_query,
            limit=5,
            project_path=project_path,
            skip_audit=True,
        )
        if existing:
            similar_section = "\n\n## ⚠️ Similar Existing Entries (check for duplicates before logging):\n"
            for e in existing:
                similar_section += f"  [{e['item_type'].upper()} ID:{e['item_id']}] {e['title']}\n"

    influence_block = SESSION_INFLUENCE_PROMPT.rstrip()

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
{f'Patterns noticed: {new_patterns_noticed}' if new_patterns_noticed else 'No new patterns reported.'}
→ For each recurring issue, draft a `memory_add_pattern` call with: name, symptoms, root_cause, standard_fix, tags
→ Search existing patterns first to avoid duplicates.

### 3. Skills to Log
{f'Workflows used: {workflows_used}' if workflows_used else 'No workflows reported.'}
→ For each multi-step workflow that succeeded, draft a `memory_add_skill` call with: name, domain, trigger, workflow, pitfalls, tags
→ If the workflow had >3 steps and could be reused, it's a strong skill candidate.

### 4. Conversation Summary
→ Draft a `memory_add_conversation` call to log this session for cross-session continuity.
{similar_section}

{influence_block}

## Instructions
1. Draft ALL entries above in a markdown block.
2. Present them to the user for explicit approval.
3. Only after approval, call the respective memory_add_* tools.
4. Do NOT log anything without user confirmation."""

    return prompt
