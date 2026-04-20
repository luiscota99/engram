def calculate_tokens(text):
    # Rough estimate: 1 token = 4 characters
    return len(text) // 4

def simulate():
    # Setup
    system_prompt = "You are a helpful coding assistant. " * 50 # ~400 chars, ~100 tokens
    project_context = "This project uses Python, SQLite, and MCP. Architecture guidelines... " * 100 # ~7000 chars, ~1750 tokens
    
    questions = [
        "How do I add a new file?",
        "What is the schema for the items table?",
        "Can you write a search query for that?",
        "Where is the MCP server initialized?",
        "How do I handle errors in the API?",
        "Add a new method to the database class.",
        "Update the search logic to include project_affinity.",
        "Write a test for the new search logic.",
        "What was that mistake I made earlier?",
        "Summarize what we did today."
    ]
    
    avg_response_len = 2000 # ~500 tokens per AI response
    
    print("=== SCENARIO A: Standard Chat (No Engram) ===")
    print("Context grows linearly with every message. We paste the project context at the beginning.")
    
    current_chat_history_tokens = calculate_tokens(system_prompt) + calculate_tokens(project_context)
    total_tokens_processed_A = 0
    
    for i, q in enumerate(questions):
        q_tokens = calculate_tokens(q)
        # In a standard chat, the API receives the ENTIRE history up to this point
        tokens_sent = current_chat_history_tokens + q_tokens
        
        # AI generates response
        response_tokens = avg_response_len // 4
        total_tokens_processed_A += tokens_sent + response_tokens
        
        # Add to history for next turn
        current_chat_history_tokens += q_tokens + response_tokens
        
        if i in [0, 4, 9]:
            print(f"Turn {i+1} ({q[:30]}...): Sent {tokens_sent} tokens. Running Total Cost: {total_tokens_processed_A} tokens.")

    print(f"\nTotal Tokens Processed (Cost) for 10 turns: {total_tokens_processed_A}")
    print("\n" + "="*50 + "\n")
    
    print("=== SCENARIO B: Engram (RAG + Session Resets) ===")
    print("Chat history is kept minimal or reset. Project context is retrieved via search only when needed.")
    
    total_tokens_processed_B = 0
    
    # Let's say chat history is just the last 2 interactions to keep it conversational,
    # or we do full session resets and rely entirely on RAG. Let's assume full RAG approach for task-based work.
    
    for i, q in enumerate(questions):
        q_tokens = calculate_tokens(q)
        
        # We query Engram. Let's assume it returns top 3 relevant chunks (e.g. ~300 tokens each)
        retrieved_context_tokens = 900 
        
        # The API receives: System Prompt + Retrieved Chunks + Current Question
        tokens_sent = calculate_tokens(system_prompt) + retrieved_context_tokens + q_tokens
        
        # AI generates response
        response_tokens = avg_response_len // 4
        total_tokens_processed_B += tokens_sent + response_tokens
        
        if i in [0, 4, 9]:
            print(f"Turn {i+1} ({q[:30]}...): Sent {tokens_sent} tokens. Running Total Cost: {total_tokens_processed_B} tokens.")

    print(f"\nTotal Tokens Processed (Cost) for 10 turns: {total_tokens_processed_B}")
    print("\n" + "="*50 + "\n")
    
    savings = total_tokens_processed_A - total_tokens_processed_B
    percent_savings = (savings / total_tokens_processed_A) * 100
    print(f"SUMMARY: Engram saved {savings} tokens over just 10 turns ({percent_savings:.1f}% reduction in API costs/processing time).")
    print("As the conversation goes to 50 or 100 turns, Scenario A scales exponentially, while Scenario B remains flat.")

if __name__ == '__main__':
    simulate()
