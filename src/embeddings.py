import json
import os
import urllib.request


def embed_text(text, model="nomic-embed-text"):
    """Generate an embedding using local Ollama instance."""
    if not text:
        return None
    base_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    url = f"{base_url}/api/embeddings"
    data = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read().decode())
            return result.get("embedding")
    except Exception:
        return None  # Graceful fallback if Ollama isn't running
