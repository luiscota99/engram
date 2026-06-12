"""
Auto-extraction — LLM + regex fallback for durable facts from agent turns.

Try LLM extraction when available, always merge regex fallback results.
"""

from __future__ import annotations

import re

from .capture import suggest_capture
from .llm import call_chat_completion, is_llm_available, parse_json_from_llm

EXTRACT_SYSTEM_PROMPT = """You are a memory extraction assistant for an engineering knowledge base.
Analyze the conversation and extract ONLY durable facts useful across future sessions.

Good examples: recurring bugs, standard fixes, workflows, project constraints, strong preferences.
Bad examples: temporary tasks, one-off questions, assistant boilerplate.

Rules:
- MAX 2 entries per conversation
- Each entry must have: type (mistake|pattern|skill|fact), title, summary
- Return [] if nothing durable was revealed
- Return ONLY valid JSON array, no markdown fences

Example:
[{"type": "pattern", "title": "SQLite WAL locks", "summary": "Use WAL mode for concurrent reads."}]
"""


def _clean_value(value: str, max_len: int = 120) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" .,!?:;\"'`")
    if not value or len(value) > max_len:
        return ""
    if re.search(r"https?://|@|[{}<>]", value):
        return ""
    return value


def _regex_extract(messages: list[dict]) -> list[dict]:
    """Regex fallback for obvious durable statements when LLM extraction is unavailable."""
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(entry_type: str, title: str, summary: str):
        key = summary.lower()
        if not summary or key in seen:
            return
        seen.add(key)
        candidates.append({"type": entry_type, "title": title, "summary": summary})

    for msg in messages:
        role = str(msg.get("role") or "").lower()
        if role not in ("user", "assistant"):
            continue
        text = str(msg.get("content") or "")
        if not text:
            continue

        m = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", text, re.I)
        if m:
            name = _clean_value(m.group(1), 50)
            if name:
                add("fact", "User identity", f"User's name is {name}.")

        m = re.search(r"\bi (?:prefer|like|love|hate|do not like|don't like)\s+([^.!?\n]{4,100})", text, re.I)
        if m:
            pref = _clean_value(m.group(1), 100)
            if pref:
                add("fact", "User preference", f"User prefers {pref}.")

        m = re.search(r"\b(?:always|never)\s+([^.!?\n]{4,100})", text, re.I)
        if m:
            rule = _clean_value(m.group(1), 100)
            if rule:
                add("fact", "Project constraint", f"Always/never rule: {rule}.")

    return candidates[:2]


def _llm_extract(messages: list[dict]) -> list[dict]:
    if not is_llm_available():
        return []
    recent = messages[-6:] if len(messages) > 6 else messages
    chat_messages = [{"role": "system", "content": EXTRACT_SYSTEM_PROMPT}] + [
        {"role": m.get("role", "user"), "content": str(m.get("content") or "")}
        for m in recent
        if m.get("content")
    ]
    raw = call_chat_completion(chat_messages, temperature=0.1, max_tokens=500, task="extract")
    if not raw:
        return []
    parsed = parse_json_from_llm(raw)
    if not isinstance(parsed, list):
        return []
    out: list[dict] = []
    for item in parsed:
        if isinstance(item, dict) and item.get("summary"):
            out.append({
                "type": str(item.get("type") or "fact"),
                "title": str(item.get("title") or "Extracted fact"),
                "summary": str(item["summary"]),
            })
    return out[:2]


def extract_from_messages(messages: list[dict]) -> dict:
    """Extract memory candidates from chat messages (LLM + regex combined)."""
    llm_facts = _llm_extract(messages)
    regex_facts = _regex_extract(messages)
    combined = llm_facts + regex_facts
    return {
        "candidates": combined[:4],
        "llm_used": bool(llm_facts),
        "regex_used": bool(regex_facts),
        "llm_available": is_llm_available(),
    }


def extract_from_task(
    task_description: str,
    outcome: str,
    *,
    errors_encountered: str = "",
    files_changed: list[str] | None = None,
) -> dict:
    """Combine engineering capture heuristics with message-style extraction."""
    capture = suggest_capture(
        task_description=task_description,
        outcome=outcome,
        errors_encountered=errors_encountered,
        files_changed=files_changed,
    )
    message_facts = extract_from_messages([
        {"role": "user", "content": task_description},
        {"role": "assistant", "content": outcome},
    ])
    return {
        "capture_suggestion": capture,
        "auto_extract": message_facts,
    }


def format_auto_extract_result(result: dict) -> str:
    lines = ["Auto-extract results:\n"]
    if not result.get("candidates"):
        lines.append("No durable facts detected.")
        lines.append(f"LLM available: {result.get('llm_available', False)}")
        return "\n".join(lines)
    for i, c in enumerate(result["candidates"], 1):
        lines.append(f"{i}. [{c.get('type', 'fact').upper()}] {c.get('title', 'Untitled')}")
        lines.append(f"   {c.get('summary', '')}")
    lines.append("")
    lines.append(
        "Present drafts to the user for approval before calling memory_add."
    )
    return "\n".join(lines)
