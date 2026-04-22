---
name: Adaptive Engram Bootstrapping
overview: Create a new Cursor skill for Adaptive Engram Bootstrapping and formalize the Signal-Based Escalation pattern, integrating with your existing Engram CLI and adaptive workflow infrastructure.
todos:
  - id: create-skill-dir
    content: Create ~/.cursor/skills/engram-bootstrapping/SKILL.md with frontmatter and workflow instructions
    status: pending
  - id: log-pattern
    content: Run engram add pattern to log Signal-Based Escalation to Engram database
    status: pending
  - id: verify-integration
    content: Run engram sync-skills --dry-run to verify skill is discoverable
    status: pending
isProject: false
---

# Adaptive Engram Bootstrapping Skill and Signal-Based Escalation Pattern

## Current State Analysis

Your Engram project already has substantial infrastructure supporting this:

- **CLI**: `engram bootstrap --mode adaptive|full|minimal` already exists ([cli.py](engram/src/cli.py) lines 965-1050)
- **Adaptive Rule**: `engram-adaptive.mdc` already defines LIGHT → FULL escalation triggers ([cursor-rules/engram-adaptive.mdc](engram/cursor-rules/engram-adaptive.mdc))
- **Commands exist**: `index-project`, `sync-skills` are implemented

What's missing is a **formal Cursor Skill** that teaches agents how to bootstrap Engram-backed projects, and a **logged pattern** for signal-based escalation.

---

## Part 1: Create Adaptive Engram Bootstrapping Skill

### Location

`~/.cursor/skills/engram-bootstrapping/SKILL.md`

This should be a personal skill (not project-level) since it applies across all projects you want to Engram-enable.

### Skill Structure

```
engram-bootstrapping/
├── SKILL.md           # Main instructions
└── reference.md       # Detailed command reference (optional)
```

### SKILL.md Content

```yaml
---
name: engram-bootstrapping
description: >-
  Bootstrap a project for Engram persistent memory. Use when initializing a new 
  project for Engram, optimizing an existing project's memory configuration, or 
  when the user mentions engram bootstrap, engram setup, or memory initialization.
---
```

**Body includes:**
1. When to trigger (new project, existing project optimization, explicit request)
2. Prerequisites check (Engram installed, MCP configured)
3. Three-step workflow:
   - `engram bootstrap --mode adaptive` 
   - `engram index-project`
   - `engram sync-skills`
4. Mode selection guidance (adaptive vs full vs minimal)
5. Verification steps

---

## Part 2: Log the Signal-Based Escalation Pattern

### Add to Engram Database

Log this as a **pattern** (not a skill) since it describes symptoms → root cause → fix:

```bash
engram add pattern \
  --name "Signal-Based Escalation" \
  --symptoms "Agentic systems feeling heavy or slow for simple tasks; high token overhead for trivial fixes; always-on complex protocols applied to low-complexity requests" \
  --root-cause "Full engagement mode (session init, search, retrospective) applied uniformly regardless of task complexity" \
  --fix "Use Adaptive engagement: start with LIGHT mode (one memory_search), escalate to FULL mode only when complexity signals are detected (error loops, architecture keywords, >10 turns, explicit user request)" \
  --tags "engram,adaptive,performance,token-efficiency"
```

### Update engram-adaptive.mdc

The current `engram-adaptive.mdc` already implements this pattern well. Minor refinements could include:
- Adding a "Signal 6: Token budget awareness" trigger
- Documenting the pattern name explicitly in the rule header

---

## Part 3: Implementation Tasks

### Task 1: Create the Skill Directory and SKILL.md

Write `~/.cursor/skills/engram-bootstrapping/SKILL.md` with:
- YAML frontmatter (name, description)
- Prerequisites section
- Three-step workflow
- Mode selection guidance
- Troubleshooting tips

### Task 2: Log the Pattern to Engram

Run the `engram add pattern` command to persist "Signal-Based Escalation" in your memory database.

### Task 3: (Optional) Create reference.md

Add detailed command reference if the SKILL.md exceeds ~200 lines.

### Task 4: Verify Integration

- Run `engram sync-skills --dry-run` to confirm the new skill shows up
- Test the skill triggers by asking "bootstrap this project for Engram"

---

## Design Decisions

| Decision | Recommendation | Rationale |
|----------|---------------|-----------|
| Skill vs Pattern | Both — skill for the workflow, pattern for the anti-pattern | Skill teaches *how*, pattern documents *why* |
| Skill location | Personal (`~/.cursor/skills/`) | Cross-project applicability |
| Include scripts? | No | CLI commands suffice; no fragile shell scripts needed |
| Reference file? | Optional | Only if SKILL.md gets verbose |

---

## Files to Create/Modify

| Action | Path |
|--------|------|
| Create | `~/.cursor/skills/engram-bootstrapping/SKILL.md` |
| Create (optional) | `~/.cursor/skills/engram-bootstrapping/reference.md` |
| Run command | `engram add pattern --name "Signal-Based Escalation" ...` |
