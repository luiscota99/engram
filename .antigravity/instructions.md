# Engram Project Instructions

Engagement mode: **ADAPTIVE** — LIGHT by default, escalates automatically when complexity is detected (recommended)

You are operating in a project backed by the **Engram Persistent Memory System**.

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

> **Interface**: Antigravity uses the Engram **CLI** (`python3 -m src.cli ...`), not MCP tools. Run commands from the **engram project root** (or with `PYTHONPATH` set) so `python3 -m src.cli` resolves.

---

## Engagement Levels

| Level | What happens | When |
|-------|-------------|------|
| **OFF** | No Engram CLI calls | Trivial questions, user says "quick question" or "no engram" |
| **LIGHT** | One `search` at start; **at most one** `suggest-capture` at **session end** only if **significance** holds (see below). No session init, no committee, no transcripts | Default for most tasks |
| **FULL** | Session init + Committee workflow + transcripts + decisions; **mandatory** `suggest-capture` before persisting memory at retrospective | Complex tasks (see escalation triggers) |

---

## Default Behavior: LIGHT Mode

For most tasks, at the **start** of the session:

```bash
python3 -m src.cli search "keywords from task" -n 3
```

1. Extract 2-4 keywords from the user's request
2. Run the search above
3. If a relevant **skill** or **pattern** is returned, mention it briefly before proceeding
4. Proceed with the task normally — **no** session init, **no** transcripts, **no** committee

If no relevant result is found, proceed silently without mentioning Engram.

### LIGHT — optional end-of-session capture (proactive, bounded)

**Do not** run capture after every message. **Do not** use bare closings (`thanks`, `done`, `bye`, `ok`) *alone* as triggers—that causes noise.

At **natural session end** (user signals they are done **and** you have finished substantive work), you **may** run **at most one** capture check if **significance** is met:

- The session was **escalated to FULL** at any point, **or**
- The task touched **multiple files** or many turns, **or**
- A **non-trivial** fix or investigation was completed (refactor, debug, architecture, migration, etc.), **or**
- The combined **task + outcome** plausibly merits a mistake, pattern, or skill

If uncertain, run `suggest-capture` **once** and **only** surface the result to the user if the suggestion is **non-empty** (see `suggested_types` in `--json` output) **or** the default (non-JSON) text is not the "no strong signals" message. If the heuristic would produce nothing to save, skip the prompt or a single short "Nothing to capture—ok to close?"

**Command (same heuristics as Cursor `session-capture` hooks):**

```bash
python3 -m src.cli suggest-capture --task "one-line task summary" --outcome "what was delivered or learned" --json
```

Use `--errors "..."` and `--files "a.py,b.py"` when applicable. Get **explicit user approval** before any `add mistake` / `add pattern` / `add skill` commands.

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

### Step 7: End-of-Session Retrospective (capture-first; FULL mode)

After significant work, **do not** hand-draft long `add` blocks as the primary path. **Always start** with the heuristic engine (same as Cursor `suggest-capture` / `memory_suggest_capture`):

```bash
python3 -m src.cli suggest-capture \
  --task "Concise task description" \
  --outcome "What was completed and key learnings" \
  --errors "Optional: errors, stack traces, wrong turns" \
  --files "optional,comma,separated,paths" \
  --json
```

1. Run the command above (use `--json` for structured review, or omit `--json` for human-readable output).
2. Present the result to the user. **Get explicit approval** before writing to memory.
3. **After approval**, run the appropriate `add` subcommands with the approved fields, for example:

```bash
python3 -m src.cli add mistake --date "YYYY-MM-DD" --context "..." --mistake "..." --root-cause "..." --fix "..." --prevention "..."
python3 -m src.cli add pattern --name "..." --symptoms "..." --root-cause "..." --fix "..."
python3 -m src.cli add skill --name "..." --domain "engineering" --trigger "..." --workflow "..."
```

**Escape hatch:** If the heuristics miss something important, the user can ask for a **manual** `add` for a specific type; fill fields carefully and still ask for approval.

---

## Context Reset Protocol

When the session context is getting long or a major milestone is complete:

1. **First:** run the **capture check** (LIGHT optional rules or FULL Step 7): `suggest-capture` → user approval (if any entries) → `add` commands as needed. If there is **nothing to save**, proceed without blocking.
2. If in FULL: log a final **decision** summarizing next steps (when a session is active)
3. **Then** issue the context reset alert (below)

> [!WARNING]
> **SYSTEM ALERT: CONTEXT RESET REQUIRED**
> Capture has been **attempted**; approved items are saved to Engram memory. If there was nothing to capture, you may still reset.
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

## Measuring impact & explicit disclosure

- **`suggest-capture`** output ends with **Engram influence (0–3)** — have the model answer briefly after non-trivial work. In FULL / committee flows, the same idea applies when using session review patterns. See Engram repo **`docs/MEASURING_FIT_AND_HELP.md`**.
- **Optional:** `python3 -m src.cli session-help --score 0-3 --note "..."` appends to `~/.engram/session-help.jsonl` (override with `ENGRAM_SESSION_HELP_LOG`).
- **When acting on Engram memory**, one short line is enough (not every `search`). **Public repos:** use titles/slugs only in commits — **no numeric IDs**. **Private:** `Engram-Refs: skill:12` is OK. Set `ENGRAM_DISCLOSURE=public` when unsure.

---

## Dependencies

- Engram CLI: `python3 -m src.cli` (from the engram project root, or with `PYTHONPATH` including the project)
- Python 3.9+
- `~/.engram/memory.db` must exist (run `engram init` if not)
