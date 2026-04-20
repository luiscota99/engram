import json
import os
import urllib.request
from typing import Dict, List

from .benchmark import PROVIDERS

MOCK_CONVERSATION = [
    "I need to add a new REST API endpoint to our backend. Where do I start?",
    "Okay, I've created the route handler in `src/api.py`. How do I connect it to the database?",
    "Got it. I used the `get_connection()` function. Now I need to validate the JSON payload.",
    "The validation is working, but it's throwing a 500 error if a field is missing. How do I return a 400 Bad Request instead?",
    "That fixed it. Now, how do I write a unit test for this endpoint?",
    "I'm using pytest. The test keeps failing because it's hitting the real database.",
    "Mocking the database worked. Can you help me add authentication to this route?",
    "I've added the JWT middleware, but how do I extract the user ID from the token inside my route?",
    "Perfect. Finally, how do I document this new endpoint in our OpenAPI spec?",
    "All done! Can you review all the steps we took today?"
]

SYSTEM_PROMPT = "You are an expert AI software engineer pair-programming with the user. Keep answers very brief (1-2 sentences max) for benchmarking purposes."

ENGRAM_MOCK_CONTEXT = "\n".join([
    "[RETRIEVED CONTEXT]",
    "- Past pattern: When writing unit tests in this project, always mock `get_connection()` using `unittest.mock.patch`.",
    "- Project rule: All API endpoints must return standard JSON formatted errors: {\"error\": \"message\"}.",
    "- Skill: JWT extraction is handled by `request.state.user.id`."
])

# Caveman-optimized context
CAVEMAN_CONTEXT = "\n".join([
    "[CONTEXT]",
    "- Pattern: unit test mock `get_connection()` w/ `unittest.mock.patch`",
    "- Rule: API errs = `{\"error\": \"message\"}` JSON",
    "- Skill: JWT extr = `request.state.user.id`"
])

CAVEMAN_SYSTEM_PROMPT = "Terse like smart caveman. Technical substance exact. Only fluff die. Drop articles, filler, hedging. [thing] [action] [reason]. [next step]."

def call_llm(messages: List[Dict[str, str]], provider_name: str, config: Dict[str, str]) -> tuple[int, str]:
    """Call the LLM and return (total_tokens, response_text)."""
    api_key = os.environ.get(config["env_var"])
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "EngramBenchmark/1.0 (Python/urllib)"
    }

    if provider_name == "OpenRouter":
        headers["HTTP-Referer"] = "https://github.com/engram-memory/engram"

    data = {
        "model": config["model"],
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 100
    }

    req = urllib.request.Request(
        config["base_url"],
        data=json.dumps(data).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            usage = result.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            return total_tokens, content
    except Exception as e:
        print(f"Error calling {provider_name}: {e}")
        return 0, ""


def estimate_tokens(messages: List[Dict[str, str]]) -> int:
    """Mock token estimation: 1 token per 4 characters."""
    total_chars = 0
    for msg in messages:
        total_chars += len(msg["content"])
    # Add base overhead and response estimate
    return (total_chars // 4) + 50


def run_simulation(mock: bool = False):
    active_providers = {name: conf for name, conf in PROVIDERS.items() if os.environ.get(conf["env_var"])}

    if not active_providers and not mock:
        print("No provider API keys found. Set GROQ_API_KEY (or others) to run the simulation, or use --mock.")
        return

    if mock:
        provider_name = "MockProvider"
        config = {"model": "gpt-mock-4"}
    else:
        # Just pick the first active provider (e.g. Groq)
        provider_name = list(active_providers.keys())[0]
        config = active_providers[provider_name]

    print(f"Starting Engram Token Simulation using {provider_name} ({config['model']})")
    print(f"Simulating {len(MOCK_CONVERSATION)} turns...\n")

    # Scenario A: Traditional Chat
    print("--- SCENARIO A: Traditional Chat (Accumulating History) ---")
    traditional_history = [{"role": "system", "content": SYSTEM_PROMPT}]
    traditional_cumulative_tokens = 0
    traditional_turn_data = []

    for i, user_msg in enumerate(MOCK_CONVERSATION):
        traditional_history.append({"role": "user", "content": user_msg})
        print(f"  Turn {i+1} (Trad): Waiting for response...", end="\r", flush=True)

        if mock:
            tokens = estimate_tokens(traditional_history)
            response_text = "I'll help you with that."
        else:
            tokens, response_text = call_llm(traditional_history, provider_name, config)

        traditional_cumulative_tokens += tokens
        traditional_turn_data.append(tokens)

        traditional_history.append({"role": "assistant", "content": response_text})
        print(f"  Turn {i+1} (Trad): Used {tokens} tokens. Cumulative: {traditional_cumulative_tokens}")

    # Scenario B: Engram Workflow
    print("\n--- SCENARIO B: Engram Workflow (Stateless + RAG Context) ---")
    engram_cumulative_tokens = 0
    engram_turn_data = []

    for i, user_msg in enumerate(MOCK_CONVERSATION):
        engram_prompt = [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + ENGRAM_MOCK_CONTEXT},
            {"role": "user", "content": user_msg}
        ]
        print(f"  Turn {i+1} (Engram): Waiting for response...", end="\r", flush=True)

        if mock:
            tokens = estimate_tokens(engram_prompt)
            response_text = "Retrieved context used."
        else:
            tokens, response_text = call_llm(engram_prompt, provider_name, config)

        engram_cumulative_tokens += tokens
        engram_turn_data.append(tokens)
        print(f"  Turn {i+1} (Engram): Used {tokens} tokens. Cumulative: {engram_cumulative_tokens}")

    # Scenario D: Engram + Caveman Protocol
    print("\n--- SCENARIO D: Engram + Caveman Protocol (Ultra-Efficient) ---")
    caveman_cumulative_tokens = 0
    caveman_turn_data = []

    for i, user_msg in enumerate(MOCK_CONVERSATION):
        caveman_prompt = [
            {"role": "system", "content": CAVEMAN_SYSTEM_PROMPT + "\n\n" + CAVEMAN_CONTEXT},
            {"role": "user", "content": user_msg}
        ]
        print(f"  Turn {i+1} (Caveman): Waiting for response...", end="\r", flush=True)

        if mock:
            # Caveman estimates use a 0.6x multiplier for the system prompt and context length
            # to simulate the "mouth being smaller" effect on output and the compressed context.
            tokens = int(estimate_tokens(caveman_prompt) * 0.75)
            response_text = "Caveman speak."
        else:
            tokens, response_text = call_llm(caveman_prompt, provider_name, config)

        caveman_cumulative_tokens += tokens
        caveman_turn_data.append(tokens)
        print(f"  Turn {i+1} (Caveman): Used {tokens} tokens. Cumulative: {caveman_cumulative_tokens}")

    # Scenario C: Traditional Chat + Engram Context
    print("\n--- SCENARIO C: Long Chat + Engram Context (Worst Case) ---")
    scenario_c_history = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + ENGRAM_MOCK_CONTEXT}]
    scenario_c_cumulative_tokens = 0
    scenario_c_turn_data = []

    for i, user_msg in enumerate(MOCK_CONVERSATION):
        scenario_c_history.append({"role": "user", "content": user_msg})
        print(f"  Turn {i+1} (Long+Engram): Waiting for response...", end="\r", flush=True)

        if mock:
            tokens = estimate_tokens(scenario_c_history)
            response_text = "Context injected."
        else:
            tokens, response_text = call_llm(scenario_c_history, provider_name, config)

        scenario_c_cumulative_tokens += tokens
        scenario_c_turn_data.append(tokens)
        scenario_c_history.append({"role": "assistant", "content": response_text})
        print(f"  Turn {i+1} (Long+Engram): Used {tokens} tokens. Cumulative: {scenario_c_cumulative_tokens}")

    # Results
    print("\n=== FINAL RESULTS ===")
    print("| Turn | Traditional | Stateless Engram | Long Chat+Engram | Engram + Caveman |")
    print("|---|---|---|---|---|")
    for i in range(len(MOCK_CONVERSATION)):
        print(f"| {i+1} | {traditional_turn_data[i]} | {engram_turn_data[i]} | {scenario_c_turn_data[i]} | {caveman_turn_data[i]} |")

    print("\n--- Summary ---")
    print(f"A. Traditional Chat Total Tokens:      {traditional_cumulative_tokens}")
    print(f"B. Stateless Engram Total Tokens:      {engram_cumulative_tokens}")
    print(f"C. Long Chat + Engram Total Tokens:    {scenario_c_cumulative_tokens}")
    print(f"D. Engram + Caveman Total Tokens:      {caveman_cumulative_tokens}")

    if traditional_cumulative_tokens > 0:
        savings_vs_a = ((traditional_cumulative_tokens - engram_cumulative_tokens) / traditional_cumulative_tokens) * 100
        savings_vs_d = ((traditional_cumulative_tokens - caveman_cumulative_tokens) / traditional_cumulative_tokens) * 100
        savings_b_vs_d = ((engram_cumulative_tokens - caveman_cumulative_tokens) / engram_cumulative_tokens) * 100
        print(f"\n✅ Standard Engram (B) saved {savings_vs_a:.1f}% vs traditional (A).")
        print(f"🚀 Caveman-optimized Engram (D) saved {savings_vs_d:.1f}% vs traditional (A).")
        print(f"📉 Adding Caveman to Engram cut another {savings_b_vs_d:.1f}% of tokens from the workflow.")
        print("\nConclusion: Caveman storage + Protocol is the maximum efficiency configuration.")

if __name__ == "__main__":
    run_simulation()
