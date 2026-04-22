import json
import os
import urllib.request

_MAX_EMBED_CHARS = 8000  # ~2000 tokens; keeps well within nomic-embed-text's 8192-token limit
_EMBED_TIMEOUT = 30  # seconds; longer texts can take a few seconds on CPU


def embed_text(text, model="nomic-embed-text"):
    """Generate an embedding using local Ollama instance."""
    if not text:
        return None
    # Truncate to avoid exceeding the model's context window
    if len(text) > _MAX_EMBED_CHARS:
        text = text[:_MAX_EMBED_CHARS]
    base_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    url = f"{base_url}/api/embeddings"
    data = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as response:
            result = json.loads(response.read().decode())
            return result.get("embedding")
    except Exception:
        return None  # Graceful fallback if Ollama isn't running
