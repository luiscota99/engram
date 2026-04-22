---
name: Engram Adaptive Workflow
description: >
  Adaptive Engram memory engagement for Antigravity. Starts in LIGHT mode
  (one quick CLI search, no ceremony) and escalates to FULL Committee workflow
  only when complexity signals are detected. Use this instead of the always-on
  committee workflow for the recommended balanced experience.
---

# Engram Adaptive Workflow (Antigravity)

Engram engagement is **adaptive**: minimal by default, escalating to the full Committee workflow only when the task warrants it. This prevents every trivial fix from becoming a multi-step committee session.

> **Interface**: Antigravity uses the Engram **CLI** (`python3 -m src.cli ...`), not MCP tools.

---

## Engagement Levels

| Level | What happens | When |
|-------|-------------|------|
| **OFF** | No Engram CLI calls | Trivial questions, user says "quick question" or "no engram" |
| **LIGHT** | One `engram search` at start, no session/transcripts | Default for most tasks |
| **FULL** | Session init + Committee workflow + transcripts + decisions | Complex tasks (see escalation triggers) |

---

## Default Behavior: LIGHT Mode

For most tasks, at the start of the session:

```bash
python3 -m src.cli search "keywords from task" -n 3
```

1. Extract 2-4 keywords from the user's request
2. Run the search above
3. If a relevant **skill** or **pattern** is returned, mention it briefly before proceeding
4. Proceed with the task normally — **no** session init, **no** transcripts, **no** retrospective

If no relevant result is found, proceed silently without mentioning Engram.

---

## Escalation Triggers — Switch to FULL Mode

Escalate if **any** of the following are true:

### Signal 1: Complexity Keywords
Request contains any of:
- `debug`, `investigate`, `why is`, `how does this work`, `trace`
- `refactor`, `architecture`, `redesign`, `migrate`, `migration`
- `performance`, `slow`, `bottleneck`, `optimize`
- `security`, `vulnerability`, `audit`, `review`
- `this keeps happening`, `intermittent`, `not sure why`

### Signal 2: Scope Signal
- Task spans multiple files, modules, or systems
- User says "across the codebase", "project-wide", "all modules", "everywhere"

### Signal 3: Uncertainty / Repetition
- User expresses uncertainty: "I'm not sure why", "it keeps failing", "something's off"
- Same issue has appeared more than once in the conversation

### Signal 4: Multi-session Work
- Task is too large to complete in one session
- User says "continue from where we left off"

### Signal 5: Explicit Request
- User says: "use engram", "check memory", "full workflow", "use committee"

---

## When Escalating to FULL Mode

Announce once: *"This task looks complex — switching to full Engram Committee workflow."*

Then follow these steps:

### Step 1: Search Memory

```bash
python3 -m src.cli search "task keywords" -n 5
python3 -m src.cli recent -n 3
```

Check for relevant past mistakes, patterns, or skills before starting.

### Step 2: Initialize Session

```bash
python3 -m src.cli add session \
  --id "YYYY-MM-DD__TaskName" \
  --title "Descriptive task title" \
  --date "YYYY-MM-DD" \
  --domain "engineering"
```

### Step 3: Route to Committee

Assume the **Facilitator** persona. Delegate to virtual subagents in order:

| Role | Responsibility |
|------|---------------|
| **Analyst** | Define constraints, problem framing, success criteria |
| **Researcher** | Identify missing context, files to read, unknowns |
| **Skeptic** | Challenge the approach, identify risks and edge cases |
| **Archivist** | Prepare the implementation plan or diff |

To get a role's full charter:
```bash
python3 -m src.cli get-role Analyst
```

### Step 4: Persist Transcripts

As each subagent completes their reasoning:
```bash
python3 -m src.cli add transcript \
  --session-id "YYYY-MM-DD__TaskName" \
  --role "Analyst" \
  --content "Problem framing: ..."
```

### Step 5: Log Decisions

When a critical technical decision is reached:
```bash
python3 -m src.cli add decision \
  --session-id "YYYY-MM-DD__TaskName" \
  --decision "Decided to use X instead of Y because..."
```

### Step 6: Deliver Facilitator Summary

Present the final output in this format:
- **Executive Summary**
- **Committee Recommendation**
- **Evidence & Assumptions**
- **Risks & Mitigations**
- **Next Steps**

### Step 7: End-of-Session Retrospective (FULL mode only)

After completing significant work, propose the following for user approval:

```bash
# Log a mistake if one occurred
python3 -m src.cli add mistake \
  --date "YYYY-MM-DD" \
  --context "What we were doing" \
  --mistake "What went wrong" \
  --root-cause "Why it happened" \
  --fix "How it was resolved" \
  --prevention "How to avoid next time"

# Log a pattern if a recurring issue was identified
python3 -m src.cli add pattern \
  --name "Pattern Name" \
  --symptoms "What it looks like" \
  --root-cause "Why it happens" \
  --fix "Standard resolution"

# Log a skill if a repeatable workflow was used
python3 -m src.cli add skill \
  --name "Skill Name" \
  --domain "engineering" \
  --trigger "When to use this" \
  --workflow "Step-by-step instructions"
```

**Always draft these in a markdown block and ask the user for approval before running.**

---

## Context Reset Protocol

When the session context is getting long or a major milestone is complete:

1. Run the retrospective (Step 7 above)
2. Log a final decision summarizing next steps
3. Issue this alert:

> [!WARNING]
> **SYSTEM ALERT: CONTEXT RESET REQUIRED**
> All progress, patterns, and decisions have been saved to Engram memory.
>
> **Action Required:**
> 1. Open a new terminal/chat session.
> 2. Resume with:
> ```text
> [Continuing from previous session] Resume work. Start by running:
> python3 -m src.cli get-session --id '<session_id>'
> ```

---

## User Override Phrases

| User says | Action |
|-----------|--------|
| "quick question" / "no engram" | OFF mode — skip all Engram calls |
| "use engram" / "check memory" | FULL mode — run search + init session if needed |
| "simple fix" | Stay in LIGHT mode |
| "continue from last session" | FULL mode — start with `get-session` |

---

## Dependencies

- Engram CLI: `python3 -m src.cli` (from the engram project root)
- Python 3.9+
- `~/.engram/memory.db` must exist (run `engram init` if not)
