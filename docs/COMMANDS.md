# Engram — Command Reference

> Auto-generated from the CLI parser by `scripts/gen_docs.py`. Do not edit by hand; run `python3 -m scripts.gen_docs` after changing commands.

All commands are invoked as `engram <command>`. 55 commands.

### `engram add`

Add a new entry

  - `<kind>` — one of `mistake, pattern, skill, conversation, session, transcript, decision, prompt`

- **`engram add conversation`**
- **`engram add decision`**
- **`engram add mistake`**
- **`engram add pattern`**
- **`engram add prompt`**
- **`engram add session`**
- **`engram add skill`**
- **`engram add transcript`**

### `engram antigravity-global`

Install or refresh the global Engram block in ~/.gemini/AGENTS.md (all Antigravity workspaces)

### `engram backup`

Export database to JSON format

  - `--git`

### `engram benchmark`

Run LLM benchmark suite

### `engram bootstrap`

Bootstrap agent rules for the current project

  - `--mode` — one of `adaptive, full, minimal`
  - `--omit-project-integration` — Skip Cursor rules + .antigravity/instructions.md (also if .omit-agent-integration exists)
  - `--setup-mcp`
  - `--no-mcp`
  - `--global-antigravity` — Also write/update the Engram snippet in ~/.gemini/AGENTS.md (applies in every Antigravity workspace)

### `engram browse`

Interactive TUI browser for memory entries

### `engram claude-skill`

Install or refresh the Engram skill for Claude Code (~/.claude/skills/engram-memory)

### `engram clean-codebase`

Remove stale entries from codebase knowledge

  - `--path`

### `engram consolidate`

Consolidate multiple skills into one

  - `--delete-ids` *(required)*
  - `--name` *(required)*
  - `--domain` *(required)*
  - `--trigger` *(required)*
  - `--workflow` *(required)*
  - `--pitfalls`
  - `--key-files`
  - `--deps`
  - `--tags`

### `engram decide`

Resolver un item del inbox

  - `<id>`
  - `--approve`
  - `--reject`
  - `--ack`
  - `--run` — Con --approve: ejecuta el reflex propuesto

### `engram doctor`

Run database diagnostics and repair

  - `--repair`
  - `--fix`

### `engram efficiency`

Action-Ladder efficiency report (reflex runs, reuse, tokens avoided)

### `engram export-skills`

Export Engram skills as Cursor SKILL.md files

  - `--output`
  - `--project-skills`
  - `--ids`
  - `--domain`
  - `--min-usage`
  - `--from-patterns`
  - `--dry-run`

### `engram gc`

Garbage collect unused memories

  - `--mode` — one of `dry-run, archive, delete`
  - `--days`

### `engram get-role`

Get a subagent role profile

  - `<name>`

### `engram get-session`

Get full details of a session

  - `--id` *(required)*

### `engram graph`

Build and visualize file dependency graph

  - `--path`
  - `--file`
  - `--direction` — one of `outgoing, incoming, both`
  - `--format` — one of `mermaid, dot, json`
  - `--output`
  - `--no-index`

### `engram health`

Show a health report for the memory database

### `engram import-claude-memories`

Import Claude Code's file-based memories (~/.claude/**/memory/*.md) into Engram

  - `--dir` — Claude home to scan (default: ~/.claude)

### `engram import-cursor-skills`

Import Cursor skills into Engram

  - `<path>`
  - `--dry-run`

### `engram import-session-summary`

Ingest session_summary.md (or given file) into global memory as a conversation entry

  - `--file, -f` — Markdown file to import (default: ./session_summary.md)
  - `--project` — Associate with this project path (default: current working directory)
  - `--force` — Insert even if the same content was imported before

### `engram import-skills`

Import skills from orchestrator SKILL.md files

  - `<path>`

### `engram inbox`

Alertas y decisiones pendientes (agentes proponen, tú decides)

  - `--status`

### `engram index-project`

Index project codebase knowledge

  - `--path`
  - `--file`
  - `--summary`
  - `--exports`
  - `--deps`
  - `--force`
  - `--check`
  - `--caveman`
  - `--caveman-level` — one of `lite, full, ultra`
  - `--llm-summarize`
  - `--verbose`

### `engram init`

Initialize the database

### `engram install`

One-shot setup: detect Cursor/Claude Code/Antigravity and wire Engram into all of them

  - `--all` — Set up every integration even if not detected

### `engram link-pattern`

Link pattern to a conversation

  - `<name>` — Pattern name
  - `--conversation` *(required)*
  - `--date`
  - `--notes`

### `engram list`

List entries by type

  - `<kind>` — one of `mistakes, patterns, skills, conversations, prompts, sessions`

### `engram llm`

LLM status, consolidation audit, and assisted GC

  - `<llm_command>` — LLM subcommands — one of `status, audit, gc`

- **`engram llm audit`**
- **`engram llm gc`**
- **`engram llm status`**

### `engram merge-projects`

Merge one project record into another (codebase rows, graph, item links); deletes the source project

  - `--from` *(required)* — Source project: numeric id, path as stored in DB, or project name
  - `--into` *(required)* — Target project to keep (id, path, or name)
  - `--execute` — Apply changes (default: dry-run)

### `engram migrate`

Database migration utilities

  - `--rollback`
  - `--mark-stale`

### `engram migrate-embeddings`

Switch embedding model: mark stale, update schema_meta, reembed

  - `--target-model` *(required)* — Ollama embedding model name

### `engram notify-init`

Crear el reflex 'notify' (borrador, osascript por default)

### `engram promote`

Draft a reflex script from a proven skill

  - `<skill_id>` — Skill id to promote

### `engram query-codebase`

Query indexed codebase knowledge

  - `<query>`
  - `--path`
  - `--caveman`
  - `--caveman-level` — one of `lite, full, ultra`

### `engram recent`

Show recent entries

  - `-n`
  - `-t, --type` — one of `mistake, pattern, skill, conversation, prompt`

### `engram reembed`

Re-generate embeddings for stale/pending items

  - `--batch-size`

### `engram reflex`

Manage reflexes (list / approve / run)

  - `<action>` — one of `list, approve, run`

- **`engram reflex approve`**
- **`engram reflex list`**
- **`engram reflex run`**

### `engram retrieval-benchmark`

Run R@k / MRR / NDCG retrieval quality benchmark (see benchmarks/BENCHMARKS.md)

  - `<bench_args>` — Pass-through args (e.g. -- --mode compare). Prefix with -- if needed.

### `engram route`

Action-ladder lookup: reflex / recall / reason for a task

  - `<task>` — Task description

### `engram run`

Run a prompt using Claw-Code execution engine

  - `<prompt>`
  - `--role`
  - `--model`
  - `--session-id`
  - `--claw-path`

### `engram schedule`

Programar en cron: un reflex por id, o 'self-check'

  - `<what>` — id de reflex o 'self-check'
  - `<cron>` — expresión cron (default 9am diario)
  - `--remove`

### `engram search`

Search all memory

  - `<query>` — Search query
  - `-t, --type` — one of `mistake, pattern, skill, conversation, prompt`
  - `--tags` — Comma-separated tags
  - `-n, --limit`
  - `--project` — Project directory for affinity ranking (default: current working directory)
  - `--no-project` — Disable project-scoped affinity (search global memory only)
  - `--include-superseded` — Include superseded/invalidated memories in results

### `engram seed`

Seed with historical data

### `engram self-check`

Barrido de auto-mantenimiento → inbox (idempotente)

### `engram session-help`

Log Session Help Score (0–3) to a local JSONL file for measuring Engram impact

  - `--score` *(required)* — 0–3: how much Engram memories influenced this session (see docs/MEASURING_FIT_AND_HELP.md)
  - `--note` — Optional one-sentence justification
  - `--task` — Optional short task label

### `engram session-review`

Print the session retrospective checklist (same output as MCP memory_session_review)

  - `--conversation-id` — Label for this retrospective
  - `--project` — Project directory for duplicate search + affinity (default: current working directory)
  - `--no-project` — Do not pass a project path
  - `--tasks` — Tasks completed this session
  - `--bugs-fixed`
  - `--new-patterns`
  - `--workflows-used`

### `engram simulate`

Simulate token usage of Engram vs Traditional

  - `--mock`

### `engram sleep`

Sleep-time consolidation: merge duplicates, archive stale memories

  - `--threshold`
  - `--days` — Archive unused items older than N days
  - `--dry-run`
  - `--quiet` — No stdout (for hooks)

### `engram stats`

Show database statistics

### `engram suggest`

Suggest a prompt or skill for a task

  - `<query>` — Task description
  - `-t, --type` — one of `prompt, skill, mistake`
  - `-n, --limit`

### `engram suggest-capture`

Analyze a task and draft memory entries for review

  - `--task` *(required)*
  - `--outcome` *(required)*
  - `--errors`
  - `--files` — Comma-separated list of files changed
  - `--json` — Print raw suggestion dict as JSON (for scripts and agents)

### `engram suggest-consolidate`

Find near-duplicate memories to consolidate

  - `--threshold`
  - `--type` — one of `mistake, pattern, skill`
  - `-n, --limit`

### `engram sync-skills`

Bidirectional sync between Engram and Cursor skills

  - `--path`
  - `--dry-run`
  - `--auto`
  - `--export-missing`
  - `--import-missing`

### `engram validate`

Prove a memory changes behavior (baseline-fails/treatment-passes)

  - `<vaction>` — one of `add, run`

- **`engram validate add`**
- **`engram validate run`**
