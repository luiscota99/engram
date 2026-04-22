"""
Engram Auto-Capture — heuristic analysis for suggesting memory entries.

Analyzes a task description and outcome to produce draft memory entries
(mistakes, patterns, skills) that can be presented to the user for approval.

No LLM required — uses keyword heuristics and structural patterns.
"""
from __future__ import annotations

import re
from datetime import date

# ── Signal detection ─────────────────────────────────────────────────

_MISTAKE_SIGNALS = [
    r"\b(error|exception|traceback|failed|broke|broke[n]?|crash|bug|wrong|incorrect)\b",
    r"\b(fixed|resolved|solved|the (fix|solution|cause) was)\b",
    r"\b(root cause|turns out|it was|the problem was)\b",
]

_PATTERN_SIGNALS = [
    r"\b(again|recurring|keeps? happening|same (issue|problem|error|bug)|seen this before)\b",
    r"\b(pattern|anti-pattern|common (mistake|issue|pitfall))\b",
    r"\b(every time|whenever|always happens when)\b",
]

_SKILL_SIGNALS = [
    r"\b(workflow|process|steps?|procedure|how to|guide|recipe|checklist)\b",
    r"\b(successfully|worked|complete[d]?|done|finished|accomplished)\b",
    r"\b(reusable|repeatable|standard (approach|way|method))\b",
]


def _score_signals(text: str, patterns: list[str]) -> int:
    """Count how many signal patterns match in text (case-insensitive)."""
    text_lower = text.lower()
    return sum(1 for p in patterns if re.search(p, text_lower))


def _extract_keywords(text: str, n: int = 5) -> list[str]:
    """Extract the most significant keywords from text."""
    stop_words = {
        "the", "a", "an", "is", "it", "in", "on", "at", "to", "for", "of", "and",
        "or", "but", "was", "were", "be", "been", "being", "have", "has", "had",
        "do", "does", "did", "will", "would", "could", "should", "may", "might",
        "this", "that", "these", "those", "with", "from", "by", "as", "are",
    }
    words = re.findall(r"\b[a-zA-Z][a-zA-Z_-]{2,}\b", text)
    seen: dict[str, int] = {}
    for w in words:
        wl = w.lower()
        if wl not in stop_words:
            seen[wl] = seen.get(wl, 0) + 1
    return [w for w, _ in sorted(seen.items(), key=lambda x: -x[1])][:n]


def _infer_domain(text: str) -> str:
    """Heuristically infer the domain from task text."""
    text_lower = text.lower()
    if any(k in text_lower for k in ["react", "vue", "css", "html", "frontend", "ui", "component"]):
        return "frontend"
    if any(k in text_lower for k in ["api", "backend", "server", "database", "db", "sql", "endpoint"]):
        return "backend"
    if any(k in text_lower for k in ["docker", "kubernetes", "deploy", "ci", "cd", "pipeline", "infra"]):
        return "devops"
    if any(k in text_lower for k in ["test", "spec", "pytest", "jest", "mock", "coverage"]):
        return "testing"
    if any(k in text_lower for k in ["security", "auth", "oauth", "token", "vulnerability"]):
        return "security"
    if any(k in text_lower for k in ["performance", "slow", "memory", "cpu", "optimize", "cache"]):
        return "performance"
    if any(k in text_lower for k in ["debug", "error", "exception", "traceback", "crash"]):
        return "debugging"
    return "engineering"


# ── Main analysis function ───────────────────────────────────────────

def suggest_capture(
    task_description: str,
    outcome: str,
    errors_encountered: str = "",
    files_changed: list[str] | None = None,
) -> dict:
    """
    Analyze a completed task and suggest memory entries to capture.

    Args:
        task_description: What the task was about.
        outcome: What was accomplished / how it was resolved.
        errors_encountered: Any errors or wrong turns hit along the way.
        files_changed: List of files that were modified.

    Returns a dict with:
        - suggested_types: list of suggested memory types
        - draft_mistake: dict (or None)
        - draft_pattern: dict (or None)
        - draft_skill: dict (or None)
        - confidence: dict of confidence scores per type
        - keywords: extracted keywords
        - domain: inferred domain
    """
    combined = f"{task_description}\n{outcome}\n{errors_encountered}"
    domain = _infer_domain(combined)
    keywords = _extract_keywords(combined)
    today = date.today().isoformat()

    mistake_score = _score_signals(combined, _MISTAKE_SIGNALS)
    pattern_score = _score_signals(combined, _PATTERN_SIGNALS)
    skill_score = _score_signals(combined, _SKILL_SIGNALS)

    # Boost pattern score if there were errors (likely a known anti-pattern)
    if errors_encountered:
        mistake_score += 1
        if pattern_score > 0:
            pattern_score += 1

    # Boost skill score if files were changed (implies a real workflow was followed)
    if files_changed:
        skill_score += min(len(files_changed), 3)

    suggested_types = []
    confidence = {}

    if mistake_score >= 2 and errors_encountered:
        suggested_types.append("mistake")
        confidence["mistake"] = min(1.0, mistake_score / 4)

    if pattern_score >= 2:
        suggested_types.append("pattern")
        confidence["pattern"] = min(1.0, pattern_score / 3)

    if skill_score >= 2:
        suggested_types.append("skill")
        confidence["skill"] = min(1.0, skill_score / 5)

    # Always suggest a skill if the task was completed successfully with no suggestions yet
    if not suggested_types and outcome:
        suggested_types.append("skill")
        confidence["skill"] = 0.4

    # Build drafts
    draft_mistake = None
    if "mistake" in suggested_types and errors_encountered:
        draft_mistake = {
            "date": today,
            "context": task_description[:200],
            "mistake": errors_encountered[:200],
            "root_cause": "(fill in root cause)",
            "fix": outcome[:200],
            "prevention": "(fill in prevention strategy)",
            "tags": ", ".join(keywords[:3]),
        }

    draft_pattern = None
    if "pattern" in suggested_types and errors_encountered:
        kw = keywords[0].title() if keywords else "Issue"
        draft_pattern = {
            "name": f"{kw} Pattern",
            "symptoms": errors_encountered[:200],
            "root_cause": "(fill in root cause)",
            "standard_fix": outcome[:200],
            "tags": ", ".join(keywords[:3]),
        }

    draft_skill = None
    if "skill" in suggested_types:
        kw = keywords[0].title() if keywords else "Task"
        draft_skill = {
            "name": f"{kw} Workflow",
            "domain": domain,
            "trigger": task_description[:200],
            "workflow": outcome[:500],
            "pitfalls": errors_encountered[:200] if errors_encountered else None,
            "key_files": ", ".join((files_changed or [])[:5]),
            "tags": ", ".join(keywords[:3]),
        }

    return {
        "suggested_types": suggested_types,
        "draft_mistake": draft_mistake,
        "draft_pattern": draft_pattern,
        "draft_skill": draft_skill,
        "confidence": confidence,
        "keywords": keywords,
        "domain": domain,
    }


def format_capture_suggestion(suggestion: dict) -> str:
    """Format a capture suggestion as a readable markdown block for agent output."""
    lines = ["## Engram Memory Capture Suggestion\n"]
    lines.append(f"Domain: `{suggestion['domain']}`  Keywords: {', '.join(suggestion['keywords'][:4])}\n")

    if not suggestion["suggested_types"]:
        lines.append("_No strong signals detected for memory capture._")
        return "\n".join(lines)

    lines.append("Suggested entries (requires your approval before saving):\n")

    if suggestion["draft_mistake"]:
        conf = suggestion["confidence"].get("mistake", 0)
        lines.append(f"### Mistake  _(confidence: {conf:.0%})_")
        d = suggestion["draft_mistake"]
        lines.append(f"- **Context:** {d['context']}")
        lines.append(f"- **Mistake:** {d['mistake']}")
        lines.append(f"- **Root cause:** {d['root_cause']}")
        lines.append(f"- **Fix:** {d['fix']}")
        lines.append(f"- **Prevention:** {d['prevention']}")
        lines.append(f"- **Tags:** {d['tags']}\n")

    if suggestion["draft_pattern"]:
        conf = suggestion["confidence"].get("pattern", 0)
        lines.append(f"### Pattern  _(confidence: {conf:.0%})_")
        d = suggestion["draft_pattern"]
        lines.append(f"- **Name:** {d['name']}")
        lines.append(f"- **Symptoms:** {d['symptoms']}")
        lines.append(f"- **Root cause:** {d['root_cause']}")
        lines.append(f"- **Standard fix:** {d['standard_fix']}")
        lines.append(f"- **Tags:** {d['tags']}\n")

    if suggestion["draft_skill"]:
        conf = suggestion["confidence"].get("skill", 0)
        lines.append(f"### Skill  _(confidence: {conf:.0%})_")
        d = suggestion["draft_skill"]
        lines.append(f"- **Name:** {d['name']}")
        lines.append(f"- **Domain:** {d['domain']}")
        lines.append(f"- **Trigger:** {d['trigger']}")
        lines.append(f"- **Workflow:** {d['workflow']}")
        if d.get("pitfalls"):
            lines.append(f"- **Pitfalls:** {d['pitfalls']}")
        if d.get("key_files"):
            lines.append(f"- **Key files:** {d['key_files']}")
        lines.append(f"- **Tags:** {d['tags']}\n")

    lines.append("_Reply 'save' to log approved entries, or edit fields above before approving._")
    return "\n".join(lines)
