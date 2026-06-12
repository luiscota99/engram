"""
Merge module — LLM-assisted synthesis of duplicate memory entries.

When two near-duplicate memories are found during insertion, this module
can call the local Ollama instance to produce a single, richer entry that
preserves insights from both originals.
"""

from __future__ import annotations

from .llm import call_ollama_generate, is_llm_available, parse_json_from_llm

MERGE_PROMPT_TEMPLATE = """You are an expert knowledge curator. You have two similar memory entries that were found to be near-duplicates. Your task is to synthesize them into a single, more robust entry.

Entry A:
{entry_a}

Entry B:
{entry_b}

Produce a merged entry as a JSON object with the same fields as the originals. The merged entry should:
- Preserve ALL unique information from both entries
- Note explicitly (in the relevant field) that this was observed in multiple contexts
- Combine prevention strategies, workflow steps, or fix instructions where applicable
- Be more comprehensive than either original alone
- Keep the same JSON structure/keys as the input entries

Output ONLY valid JSON, no explanation."""


def _entry_to_text(entry: dict) -> str:
    """Format a memory entry dict as readable text for the LLM prompt."""
    lines = []
    for k, v in entry.items():
        if v is not None and k not in ("id", "created_at", "updated_at", "last_used_at", "usage_count", "tags"):
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def merge_entries(entry_a: dict, entry_b: dict, model: str | None = None) -> dict | None:
    """Use a local LLM to synthesize two similar memory entries into one.

    Returns the merged entry as a dict, or None if the merge fails
    (in which case callers should fall back to keeping entry_a or entry_b).
    """
    text_a = _entry_to_text(entry_a)
    text_b = _entry_to_text(entry_b)
    prompt = MERGE_PROMPT_TEMPLATE.format(entry_a=text_a, entry_b=text_b)

    raw_response = call_ollama_generate(prompt, model=model, task="merge")
    if not raw_response:
        return None

    merged = parse_json_from_llm(raw_response)
    if not isinstance(merged, dict):
        return None

    if "item_type" not in merged and "item_type" in entry_a:
        merged["item_type"] = entry_a["item_type"]
    merged.pop("id", None)
    return merged


def merge_available() -> bool:
    """Return True if Ollama is reachable and can perform merges."""
    return is_llm_available()
