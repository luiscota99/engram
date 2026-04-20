# Antigravity Project Instructions

You are operating in a project backed by the **Engram Persistent Memory System**.
You MUST follow the Engram Committee-Driven Workflow for all complex tasks, architectural analysis, and codebase reviews.

## Engram Committee-Driven Workflow
---
name: Engram Committee-Driven Workflow
description: >
  Enforces the Committee-Driven subagent workflow using Engram CLI as the persistent backend. 
  Antigravity must follow this protocol for complex engineering tasks.
---

# Engram Committee-Driven Workflow

## When to Use
Use this workflow anytime the user requests a complex engineering task (e.g., "build a new feature", "architect a new system", "refactor this module"). 

**MANDATORY**: You MUST also use this workflow for high-impact investigatory tasks, such as "architectural analysis", "codebase reviews", and "performance optimizations". Even if a task starts as purely investigatory, you must use the Committee to brainstorm, analyze, and review the findings to avoid blind spots. Do NOT use this for trivial questions (e.g., "how do I center a div").

## The Zero Rule (MANDATORY)
1. **Never guess unknowns**: If there is missing context, ask the user.
2. **Never behave like a single-agent solver**: Always delegate reasoning to virtual subagents.
3. **Persist everything**: All subagent outputs must be logged to Engram using the `engram add ...` CLI commands.

## Workflow Steps

### Step 1: Initialize Session
Generate a unique session ID based on the date and task (e.g., `2026-04-20__FeatureX`).
Initialize the session in Engram:
```bash
python3 -m src.cli add session --id "YYYY-MM-DD__Task" --title "Implement Feature X" --date "YYYY-MM-DD" --domain "engineering"
```

### Step 2: Route to Subagents
Assume the persona of the **Facilitator**. Route tasks to the virtual subagents:
- **Analyst**: Defines constraints, problem framing, and success criteria.
- **Researcher**: Identifies missing context or files to view.
- **Skeptic**: Plays devil's advocate, identifies risks in the proposed approach.
- **Archivist**: Prepares the implementation plan or diffs.

*Note: You simulate these roles internally. If you need their specific charters, run `python3 -m src.cli get-role <RoleName>`.*

### Step 3: Persist Transcripts
As each virtual subagent completes its reasoning, persist its output:
```bash
python3 -m src.cli add transcript --session-id "YYYY-MM-DD__Task" --role "Analyst" --content "Problem framing: ..."
```

### Step 4: Make Decisions
When a critical technical decision is reached, log it:
```bash
python3 -m src.cli add decision --session-id "YYYY-MM-DD__Task" --decision "Decided to use FTS5 instead of LIKE for performance..."
```

### Step 5: Deliver Facilitator Summary
Once all subagent reasoning is stored and decisions are made, present the final output to the user using this format:
- **Executive Summary**
- **Committee Recommendation**
- **Evidence & Assumptions**
- **Risks & Mitigations**
- **Next Steps**

## Dependencies
- Engram CLI (`src/cli.py`)
- Python 3.9+
