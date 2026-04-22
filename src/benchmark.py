import json
import os
import time
import urllib.request
from typing import Any, Dict, List

PROVIDERS = {
    "Groq": {
        "env_var": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
    },
    "Cerebras": {
        "env_var": "CEREBRAS_API_KEY",
        "base_url": "https://api.cerebras.ai/v1/chat/completions",
        "model": "llama-3.3-70b",
    },
    "GitHub Models": {
        "env_var": "GITHUB_TOKEN",
        "base_url": "https://models.inference.ai.azure.com/chat/completions",
        "model": "gpt-4o",
    },
    "OpenRouter": {
        "env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "openrouter/free",
    },
    "NVIDIA NIM": {
        "env_var": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "model": "meta/llama-3.3-70b-instruct",
    },
    "DeepSeek": {
        "env_var": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-reasoner",
    }
}

TEST_TASKS = [
    {
        "name": "Summarization",
        "messages": [
            {"role": "system", "content": "You are an expert engineer. Summarize the error in 1 sentence."},
            {"role": "user", "content": "Summarize this error log: `Traceback (most recent call last): File \"main.py\", line 4, in <module> print(1 / 0) ZeroDivisionError: division by zero`"}
        ]
    },
    {
        "name": "JSON Extraction",
        "messages": [
            {"role": "system", "content": "Extract the user intent and confidence score from the query. Output pure JSON with keys 'intent' and 'confidence'."},
            {"role": "user", "content": "I want to add a feature to sort the memory table by date instead of relevance."}
        ]
    }
]

def make_request(provider_name: str, config: Dict[str, str], messages: List[Dict[str, str]]) -> Dict[str, Any]:
    api_key = os.environ.get(config["env_var"])
    if not api_key:
        return {"error": f"Missing {config['env_var']}"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "EngramBenchmark/1.0 (Python/urllib)"
    }

    # OpenRouter recommends passing HTTP-Referer
    if provider_name == "OpenRouter":
        headers["HTTP-Referer"] = "https://github.com/engram-memory/engram"
        headers["X-Title"] = "Engram Benchmark"

    data = {
        "model": config["model"],
        "messages": messages,
        "temperature": 0.0
    }

    req = urllib.request.Request(
        config["base_url"],
        data=json.dumps(data).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    start_time = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            latency = time.time() - start_time
            result = json.loads(response.read().decode("utf-8"))

            # Extract basic usage if available
            usage = result.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)

            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

            return {
                "latency": latency,
                "content": content,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens
            }
    except Exception as e:
        latency = time.time() - start_time
        return {"error": str(e), "latency": latency}

def run_benchmark():
    active_providers = {name: conf for name, conf in PROVIDERS.items() if os.environ.get(conf["env_var"])}

    if not active_providers:
        print("No provider API keys found in environment.")
        print("Please set at least one of the following environment variables to run benchmarks:")
        for name, conf in PROVIDERS.items():
            print(f"  - {conf['env_var']} ({name})")
        return

    print(f"Starting Engram Benchmark with {len(active_providers)} active provider(s): {', '.join(active_providers.keys())}\n")

    results = []

    for task in TEST_TASKS:
        print(f"Running Task: {task['name']}...")
        for provider_name, config in active_providers.items():
            print(f"  -> Testing {provider_name} ({config['model']})", end="", flush=True)
            res = make_request(provider_name, config, task["messages"])
            if "error" in res:
                print(f" [FAILED: {res['error']}]")
            else:
                print(f" [{res['latency']:.2f}s]")

            results.append({
                "provider": provider_name,
                "model": config["model"],
                "task": task["name"],
                "latency": res.get("latency", 0),
                "total_tokens": res.get("total_tokens", 0),
                "error": res.get("error", None)
            })

    # Print Markdown Table
    print("\n### Benchmark Results\n")
    print("| Provider | Model | Task | Latency (s) | Tokens | Status |")
    print("|---|---|---|---|---|---|")

    for r in results:
        status = "❌ " + r["error"] if r["error"] else "✅ OK"
        latency_str = f"{r['latency']:.2f}s" if r.get('latency') else "N/A"
        tokens_str = str(r['total_tokens']) if r.get('total_tokens') else "N/A"
        print(f"| {r['provider']} | {r['model']} | {r['task']} | {latency_str} | {tokens_str} | {status} |")
    print("\nDone.")

if __name__ == "__main__":
    run_benchmark()
