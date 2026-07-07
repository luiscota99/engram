---
name: engram-memory
description: >
  Persistent engineering memory via the Engram CLI. Use at the start of any
  non-trivial coding task to check for known mistakes, proven patterns, and
  existing workflows before re-deriving them — and at the end of a session to
  capture new lessons. Triggers: debugging something that feels familiar,
  starting work in a known codebase, repeated failed attempts, or the user
  mentioning "engram", "memory", or "have we seen this before".
---

# Engram Memory

Engram is a local, queryable memory database (`~/.engram/memory.db`) shared
across all projects and sessions. It stores **mistakes** (what went wrong and
the fix), **patterns** (recurring problem → standard solution), **skills**
(proven multi-step workflows), and codebase knowledge, retrieved with hybrid
lexical + semantic search.

Interface: the `engram` CLI on PATH. If it is missing, skip this skill
silently — do not install anything without being asked.

## When to search (LIGHT default)

At the start of a non-trivial task, run one search with 2–4 keywords from the
request:

```bash
engram search "fts5 sqlite ranking" -n 3
```

- If a relevant **skill** or **pattern** comes back, follow it instead of
  re-deriving the approach, and say so in one line.
- If a relevant **mistake** comes back, avoid repeating it.
- If nothing relevant returns, proceed silently. Do not narrate empty searches.

Useful variants:

```bash
engram search "query" --type mistake     # only past mistakes
engram search "query" --no-project      # ignore project-affinity ranking
engram recent -n 5                       # what was recently learned
```

## When to capture

Capture at most 1–2 entries per session, only for things that would genuinely
help a future session (non-obvious root causes, hard-won fixes, reusable
workflows). Ask the user before capturing if in doubt.

```bash
engram add mistake --date 2026-07-06 --context "pytest + sqlean" \
  --mistake "assumed stdlib sqlite3 API" --fix "use sqlean.dbapi2 connect" \
  --root-cause "sqlean replaces the module globally" --tags "python,sqlite"

engram add pattern --name "API Parameter Mismatch" \
  --symptoms "404 on valid-looking id" --root-cause "id from wrong endpoint" \
  --fix "look up the id from the listing endpoint first"

engram add skill --name "Alembic squash" --domain database \
  --trigger "migration chain too long" --workflow "1. ... 2. ... 3. ..."
```

Write-time dedup will block near-duplicates; trust it rather than forcing
(`--force`) unless the user confirms the entry really is new.

## Escalate only on signal

Stay in LIGHT mode by default. Escalate to a deeper Engram session (session
init, decision transcripts — see `engram --help`) only when there are 3+
failed attempts on the same problem, or the user explicitly asks for the full
workflow.
