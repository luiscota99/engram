"""
Merge module — LLM-assisted synthesis of duplicate memory entries.

When two near-duplicate memories are found during insertion, this module
can call the local Ollama instance to produce a single, richer entry that
preserves insights from both originals.
"""

from __future__ import annotations


import json
import os
import urllib.request


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


def _call_ollama_generate(prompt: str, model: str | None = None) -> str | None:
    """Call Ollama generate endpoint and return the response text."""
    base_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    ollama_model = model or os.environ.get("ENGRAM_LLM_MODEL", "llama3.2")
    url = f"{base_url}/api/generate"
    payload = json.dumps({
        "model": ollama_model,
        "prompt": prompt,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            return result.get("response", "").strip()
    except Exception:
        return None


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

    raw_response = _call_ollama_generate(prompt, model=model)
    if not raw_response:
        return None

    # Extract JSON from response (LLM may wrap it in markdown fences)
    json_str = raw_response
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0].strip()
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0].strip()

    try:
        merged = json.loads(json_str)
        # Preserve original item_type if LLM accidentally dropped it
        if "item_type" not in merged and "item_type" in entry_a:
            merged["item_type"] = entry_a["item_type"]
        # Strip id — the merged entry will get a new one on insert
        merged.pop("id", None)
        return merged
    except (json.JSONDecodeError, ValueError):
        return None


def merge_available() -> bool:
    """Return True if Ollama is reachable and can perform merges."""
    base_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False
