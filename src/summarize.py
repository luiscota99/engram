"""
Summarize module — LLM-powered file summarization for codebase knowledge indexing.

Uses the local Ollama instance to generate structured summaries of source files,
including purpose, key exports, dependencies, and complexity notes.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from . import config

SUMMARIZE_PROMPT = """Analyze this source file and return a JSON object with these exact keys:
- "summary": 1-2 sentence description of what this file does and its role in the project
- "exports": array of strings listing key functions, classes, or constants exported (max 10)
- "dependencies": array of strings listing the most important imports or dependencies (max 10)
- "complexity": one of "low", "medium", "high" based on logical complexity

File: {file_path}
Language: {language}

```
{content}
```

Output ONLY valid JSON, no explanation or markdown fences."""


def _detect_language(file_path: str) -> str:
    """Infer programming language from file extension."""
    ext_map = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".go": "Go", ".rs": "Rust", ".c": "C", ".cpp": "C++",
        ".h": "C/C++ header", ".java": "Java", ".rb": "Ruby",
        ".sh": "Shell", ".sql": "SQL", ".md": "Markdown",
        ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
        ".toml": "TOML",
    }
    _, ext = os.path.splitext(file_path.lower())
    return ext_map.get(ext, "text")


def _call_ollama(prompt: str, model: str) -> str | None:
    """Send a prompt to Ollama and return the raw response string."""
    base_url = config.ollama_host()
    url = f"{base_url}/api/generate"
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode())
            return result.get("response", "").strip()
    except Exception:
        return None


def _parse_llm_json(raw: str) -> dict | None:
    """Extract JSON from an LLM response that might include markdown fences."""
    if not raw:
        return None
    text = raw
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def summarize_file(
    file_path: str,
    project_root: str = "",
    model: str | None = None,
    max_chars: int = 8000,
) -> dict | None:
    """Generate a structured summary for a single file using Ollama.

    Parameters
    ----------
    file_path:
        Absolute path to the file.
    project_root:
        Project root path (used for display only in the prompt).
    model:
        Ollama model to use (default: ENGRAM_LLM_MODEL env var or 'llama3.2').
    max_chars:
        Maximum characters of file content to send (prevents huge prompts).

    Returns a dict with keys: summary, exports, dependencies, complexity.
    Returns None on failure (caller should use a fallback placeholder).
    """
    ollama_model = model or config.llm_model()
    language = _detect_language(file_path)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars)
    except OSError:
        return None

    if not content.strip():
        return None

    rel_path = os.path.relpath(file_path, project_root) if project_root else file_path
    prompt = SUMMARIZE_PROMPT.format(
        file_path=rel_path,
        language=language,
        content=content,
    )

    raw = _call_ollama(prompt, model=ollama_model)
    if raw is None:
        return None
    parsed = _parse_llm_json(raw)

    if not parsed or "summary" not in parsed:
        return None

    return {
        "summary": str(parsed.get("summary", "")).strip(),
        "exports": json.dumps(parsed.get("exports") or []),
        "dependencies": json.dumps(parsed.get("dependencies") or []),
        "complexity": parsed.get("complexity", "medium"),
    }


def summarize_files_batch(
    file_paths: list[str],
    project_root: str = "",
    model: str | None = None,
    rate_limit_seconds: float = 0.5,
    progress_callback=None,
) -> dict[str, dict | None]:
    """Summarize a list of files, returning a mapping of abs_path → summary dict.

    Parameters
    ----------
    file_paths:
        List of absolute file paths to summarize.
    project_root:
        Used for display paths in prompts.
    model:
        Ollama model override.
    rate_limit_seconds:
        Pause between calls to avoid overwhelming Ollama.
    progress_callback:
        Optional callable(current, total, file_path) for progress reporting.
    """
    results = {}
    total = len(file_paths)
    for i, fp in enumerate(file_paths):
        if progress_callback:
            progress_callback(i + 1, total, fp)
        result = summarize_file(fp, project_root=project_root, model=model)
        results[fp] = result
        if i < total - 1:
            time.sleep(rate_limit_seconds)
    return results


def ollama_available(model: str | None = None) -> bool:
    """Return True if Ollama is reachable."""
    base_url = config.ollama_host()
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False
