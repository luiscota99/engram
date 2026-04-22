---
name: Antigravity proactive capture
overview: Update [engram/.antigravity/instructions.md](engram/.antigravity/instructions.md) so Antigravity uses the same heuristic `suggest-capture` path as Cursor hooks, with guarded LIGHT-mode session-end behavior and correct ordering before context reset. Optionally add `--json` to the CLI to match the stated verification plan.
todos:
  - id: doc-light-full
    content: Rewrite LIGHT/FULL and Step 7 + context reset in .antigravity/instructions.md (significance guardrails, suggest-capture first)
    status: pending
  - id: json-flag
    content: "Optional: add --json to suggest-capture + test; else adjust verification to current stdout"
    status: pending
  - id: verify
    content: Run test_capture/test_cli; manual session-end and reset-order checks
    status: pending
isProject: false
---

# Proactive memory capture (Antigravity)

## Goal

Align Antigravity with [engram/cursor-hooks/session-capture.js](engram/cursor-hooks/session-capture.js): when work finishes, the agent runs **`python3 -m src.cli suggest-capture`** (same engine as `memory_suggest_capture` in [engram/src/capture.py](engram/src/capture.py)), presents drafts, and only then runs `add` commands **after explicit user approval**—not hand-rolled `add` blocks in Step 7.

## Document changes ([engram/.antigravity/instructions.md](engram/.antigravity/instructions.md))

1. **Engagement table and LIGHT section**  
   - Resolve the current contradiction: LIGHT says “**no** retrospective” (lines 45–46) while you want capture in LIGHT.  
   - New rule: LIGHT stays **one** `search` at start; at **natural session end** only, allow **at most one** `suggest-capture` **if** a **significance** condition holds (define explicitly—see below). No committee, no session init, no transcripts in LIGHT.

2. **Significance and session-end guardrails (avoid “thanks” spam)**  
   - Do **not** rely on bare keywords (`thanks`, `done`, `bye`) alone.  
   - Instruct: run `suggest-capture` when the user **signals wrap-up** **and** at least one of: escalation was FULL at some point, multiple files/turns, non-trivial fix, or the combined task+outcome plausibly merits capture.  
   - If uncertain, the agent may run `suggest-capture` once and only surface results if `suggested_types` is non-empty or confidence is above a **stated** threshold (optional: “if the formatted output would be empty, skip or ask one short yes/no”).

3. **Replace Step 7 (FULL) retrospective**  
   - Remove the long template of manual `add mistake` / `add pattern` / `add skill` as the **primary** path.  
   - **Mandatory first step:**  
     `python3 -m src.cli suggest-capture --task "..." --outcome "..."`  
   - Optional: `--errors "..."` and `--files` (comma-separated) when known—matches [engram/src/cli/commands/memory.py](engram/src/cli/commands/memory.py) `cmd_suggest_capture`.  
   - After output: get **user approval**, then run the appropriate `add` subcommands (or keep a minimal “escape hatch” one-liner: user may request manual `add` if heuristics miss).

4. **Context reset protocol**  
   - Reorder: **(1)** run the capture check (`suggest-capture` + approval + `add` as needed) **(2)** then the context-reset warning and resume instructions.  
   - If capture produces nothing to save, still allow reset; state that clearly.

5. **Dependencies**  
   - Remind that working directory is Engram project root (or `PYTHONPATH`) so `python3 -m src.cli` works as today.

## Optional code change (if you want `--json` in verification)

The `suggest-capture` subparser in [engram/src/cli/main.py](engram/src/cli/main.py) has `--task`, `--outcome`, `--errors`, `--files` but **no `--json`**. The earlier verification idea used `--json`. Either:

- **A)** Add `--json` to `suggest-capture` that prints `json.dumps(suggestion, ...)` (ensure `suggestion` is JSON-serializable; `format_capture_suggestion` may not be needed for JSON), **or**  
- **B)** Keep verification to **stdout of `format_capture_suggestion`** (no code change).

Recommend **A** if agents or tests should parse output programmatically; otherwise **B** is enough for this milestone.

## Verification

- **Automated:** Run existing [engram/tests/test_capture.py](engram/tests/test_capture.py) and [engram/tests/test_cli_commands.py](engram/tests/test_cli_commands.py) after any CLI change. If `--json` is added, add one test that the flag emits valid JSON.  
- **Manual:** Short Antigravity-style scenario: end after a **non-trivial** task → agent runs `suggest-capture` → user approves → `add` runs; then trivial “thanks” only → no mandatory capture (per guardrails).  
- **Context reset:** Document path only—confirm order read correctly in `instructions.md`.

## Files to touch

| File | Action |
|------|--------|
| [engram/.antigravity/instructions.md](engram/.antigravity/instructions.md) | Main edit |
| [engram/src/cli/main.py](engram/src/cli/main.py) + [engram/src/cli/commands/memory.py](engram/src/cli/commands/memory.py) | Only if adding `--json` |
| [engram/tests/test_cli_commands.py](engram/tests/test_cli_commands.py) | If `--json` added |

## Definition of done

- Instructions no longer conflict on LIGHT + retrospective.  
- FULL Step 7 goes through `suggest-capture` first; approval flow preserved.  
- Context reset happens **after** capture attempt.  
- Session-end behavior is **proactive** but **not** noisy on trivial closes.  
- Optional: `--json` works and is tested, or docs use non-JSON verification explicitly.
