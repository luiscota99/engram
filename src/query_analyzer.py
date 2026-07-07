"""
Query Analyzer — auto-detect tags and technology names from a query string.

Used to automatically boost search results that match recognized technology
tags, even when the user doesn't pass ``--tags`` explicitly.

Design principles:
- Zero network calls, zero LLM dependency — pure regex + dict lookup.
- False positives are harmless (boost is additive, not a filter).
- Checked against the DB's tag vocabulary at search time so we only boost
  tags that actually exist in the user's memory.
"""
from __future__ import annotations

import re

# ── Known technology / domain vocabulary ─────────────────────────────────────
#
# These are the tags most commonly added by Engram's seed data and capture
# heuristics.  The key is the canonical tag name; values are surface forms
# that should trigger it (case-insensitive substring match).

KNOWN_TECH_TAGS: dict[str, list[str]] = {
    # Languages / runtimes
    "python": ["python", "py3", "pip", "django", "flask", "fastapi", "pydantic"],
    "javascript": ["javascript", "js ", "node.js", "nodejs", "npm", "yarn", "webpack", "babel"],
    "typescript": ["typescript", "ts ", ".tsx", ".ts"],
    "react": ["react", "jsx", "tsx", "useeffect", "usestate", "usememo", "usecallback", "hook"],
    "vue": ["vue", "nuxt"],
    "angular": ["angular", "ngmodule"],
    "rust": ["rust ", "cargo", "tokio", "rustc"],
    "go": [" golang", "gofmt", "goroutine", " go "],
    "java": [" java ", "spring", "maven", "gradle", "jvm"],
    "kotlin": ["kotlin", "coroutine"],
    "swift": ["swift ", "swiftui", "xcode"],
    # Databases
    "database": ["database", "db ", "sql", "query", "index", "schema", "table", "migration"],
    "postgresql": ["postgres", "postgresql", "psql", "pg_"],
    "mysql": ["mysql", "mariadb"],
    "sqlite": ["sqlite"],
    "mongodb": ["mongodb", "mongo", "bson"],
    "redis": ["redis", "cache", "caching"],
    "elasticsearch": ["elasticsearch", "elastic", "kibana"],
    # Infrastructure / DevOps
    "docker": ["docker", "dockerfile", "container", "image", "compose"],
    "kubernetes": ["kubernetes", "kubectl", "helm", "pod", "k8s"],
    "terraform": ["terraform", "tfstate", "hcl"],
    "aws": ["aws", "s3", "ec2", "lambda", "cloudwatch", "iam", "cloudformation"],
    "gcp": ["gcp", "google cloud", "bigquery", "gke", "pubsub"],
    "azure": ["azure", "cosmos db"],
    "ci-cd": ["ci/cd", "github actions", "gitlab ci", "jenkins", "circleci", "pipeline"],
    "git": ["git ", "github", "gitlab", "branch", "commit", "merge", "rebase", "pull request", "pr "],
    # Frontend
    "frontend": ["frontend", "browser", "ui ", "css", "html", "dom", "component"],
    "css": ["css", "scss", "tailwind", "bootstrap", "flexbox", "grid"],
    # Backend
    "backend": ["backend", "api ", "rest", "grpc", "graphql", "endpoint", "server"],
    "api": ["api ", "endpoint", "route", "request", "response", "payload", "webhook"],
    # Performance
    "performance": ["performance", "latency", "throughput", "bottleneck", "optimize", "slow"],
    "n-plus-one": ["n+1", "n plus one", "n-plus-one", "lazy load", "loop query"],
    # Testing
    "testing": ["test", "unittest", "pytest", "jest", "mock", "fixture", "coverage"],
    # Security
    "security": ["security", "vulnerability", "auth", "oauth", "jwt", "cve", "injection", "xss", "csrf"],
    "authentication": ["authentication", "login", "logout", "session", "token", "password", "oauth"],
    # AI / ML
    "ai-assistant": ["ai assistant", "llm", "agent", "copilot", "cursor", "prompt", "openai", "claude"],
    "machine-learning": ["machine learning", " ml ", "training", "inference", "model", "embedding"],
    # Architecture
    "architecture": ["architecture", "design pattern", "solid", "clean", "microservice", "monolith"],
    "refactoring": ["refactor", "cleanup", "extract", "rename", "restructure", "debt"],
    # Workflow / process
    "workflow": ["workflow", "process", "steps", "procedure", "checklist"],
    "debugging": ["debug", "stack trace", "breakpoint", "trace", "diagnose"],
    # Communication
    "caveman": ["caveman", "terse", "token-efficient", "ultra-compressed"],
}

# ── Capitalized-word extraction ───────────────────────────────────────────────
# Grab things that look like proper-noun technology names from the query.
_CAPITALIZED_PATTERN = re.compile(r"\b([A-Z][a-zA-Z0-9+#._-]{1,30})\b")

# Terms to exclude from capitalized extraction (common English words / noise)
_STOP_CAPS = frozenset({
    "I", "A", "The", "If", "In", "On", "At", "Be", "Do", "Go", "Is", "It",
    "My", "No", "Of", "Or", "So", "To", "Up", "We", "AI", "UI", "DB", "ID",
    "API", "URL", "SQL", "CSS", "HTTP", "HTTPS", "CLI", "IDE", "OS", "ENV",
    "JSON", "XML", "YAML", "CSV", "PDF", "HTML", "DOM", "JWT", "KV", "MCP",
})


def extract_tags(query: str) -> list[str]:
    """Return a deduplicated list of tag strings detected in *query*.

    Combines:
    1. Known-vocabulary lookup against KNOWN_TECH_TAGS
    2. Capitalized word extraction (e.g. "PostgreSQL", "React", "Kubernetes")

    The returned tags are lowercase canonical names matching Engram's tag
    vocabulary.  They may include tags that don't exist in the user's DB —
    the caller is responsible for filtering against existing tags.
    """
    query_lower = query.lower()
    detected: set[str] = set()

    for tag, triggers in KNOWN_TECH_TAGS.items():
        for trigger in triggers:
            if trigger.lower() in query_lower:
                detected.add(tag)
                break

    # Extract capitalized words and normalize to lowercase for matching
    for match in _CAPITALIZED_PATTERN.finditer(query):
        word = match.group(1)
        if word in _STOP_CAPS:
            continue
        normalized = word.lower()
        # Only add if it's at least 3 chars and not already detected via vocabulary
        if len(normalized) >= 3 and normalized not in detected:
            detected.add(normalized)

    return sorted(detected)


def filter_to_existing_tags(
    candidate_tags: list[str], db_path: str | None = None, conn=None
) -> list[str]:
    """Filter candidate_tags to only those present in the tags table.

    Keeps the detected tag list relevant to the user's actual memory, avoiding
    boosts for technologies that have no entries in Engram.
    """
    if not candidate_tags:
        return []
    from .database import connection_scope
    try:
        with connection_scope(conn, db_path) as conn:
            placeholders = ",".join("?" * len(candidate_tags))
            rows = conn.execute(
                f"SELECT LOWER(name) as name FROM tags WHERE LOWER(name) IN ({placeholders})",
                [t.lower() for t in candidate_tags],
            ).fetchall()
            return [r["name"] for r in rows]
    except Exception:
        return []


def detect_query_tags(query: str, db_path: str | None = None, conn=None) -> list[str]:
    """Full pipeline: extract candidates from query, then filter to existing DB tags.

    This is the main entry point used by ``src/search.py``.
    Returns a list of existing tag names that should boost results.
    """
    candidates = extract_tags(query)
    return filter_to_existing_tags(candidates, db_path=db_path, conn=conn)


# ── Temporal intent ──────────────────────────────────────────────────

_EARLIEST_CUES = (
    "first", "earliest", "initial", "initially", "originally", "original",
    "at the beginning", "to begin with", "started with",
)
_LATEST_CUES = (
    "last", "latest", "most recent", "recently", "newest", "final",
    "currently", "right now", "these days",
)

_ISO_DATE_RE = re.compile(r"\b(20\d{2})(?:[-/](\d{1,2})(?:[-/](\d{1,2}))?)?\b")
_MONTH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(20\d{2})\b",
    re.IGNORECASE,
)
_MONTH_NUM = {
    m: i + 1
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"]
    )
}


def detect_temporal_intent(query: str) -> dict:
    """Detect temporal cues in a query for date-aware ranking.

    Returns ``{"direction": "earliest"|"latest"|None, "dates": [ISO prefixes],
    "has_temporal": bool}``. Direction cues are ordinal words ("first issue",
    "most recent"); dates are explicit mentions ("May 2023", "2024-03") mapped
    to ISO prefixes for startswith-matching against item dates.

    Ranking only consumes ``dates`` — a direction-based boost was benchmarked
    on LongMemEval and removed (no R@5 effect, slightly negative MRR).
    ``direction`` remains available for display/agent use.
    """
    q = (query or "").lower()
    direction = None
    if any(cue in q for cue in _EARLIEST_CUES):
        direction = "earliest"
    elif any(cue in q for cue in _LATEST_CUES):
        direction = "latest"

    dates: list[str] = []
    for month, year in _MONTH_RE.findall(q):
        dates.append(f"{year}-{_MONTH_NUM[month.lower()]:02d}")
    for y, m, _d in _ISO_DATE_RE.findall(q):
        prefix = f"{y}-{int(m):02d}" if m else y
        if prefix not in dates:
            dates.append(prefix)

    return {
        "direction": direction,
        "dates": dates,
        "has_temporal": bool(direction or dates),
    }
