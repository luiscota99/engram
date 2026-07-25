# Switch to Engram in one afternoon

Migrate memories from **Mem0**, **Zep/Graphiti**, or **OpenMemory** into a local
Engram `memory.db` without a cloud account.

## Prerequisites

```bash
pip install -e .
# or: pipx install /path/to/engram
export ENGRAM_DB_PATH=~/.engram/memory.db   # optional; this is the default
```

## Export from the other system

| Source | What to export | Tip |
|--------|----------------|-----|
| **Mem0** | JSON list of `{memory,text,...}` (SDK/export) | Flat facts become Engram skills tagged `imported,mem0` |
| **Zep / Graphiti** | JSON episodes / facts / edges | Mapped to patterns with validity hints in fields |
| **OpenMemory** | JSON sector memories | `episodic`→conversation, `procedural`→skill, else mistake |

Exact vendor export UIs change; Engram importers are **best-effort** on common JSON shapes.

## Import

```bash
engram import-mem0 /path/to/mem0_export.json
engram import-zep /path/to/zep_or_graphiti.json
engram import-openmemory /path/to/openmemory.json
```

Each command prints `{source, added, skipped, seen}`. Re-runs are safe when
content hashes collide with Engram write-time dedup (near-duplicates may still
need inbox triage via `engram self-check`).

## Wire your agents

```bash
engram install          # Cursor MCP + Claude skill + Antigravity snippet
engram bootstrap        # per-repo rules + hooks (recall / guard / checkpoint)
```

## Verify

```bash
engram search "a topic you know was imported" --explain
engram doctor
engram roi              # after enabling: engram audit on
```

## What does *not* transfer 1:1

- Cloud graph Pro features (Mem0) → Engram entities + typed `engram link` / `engram kg`
- Bi-temporal SaaS SLAs (Zep) → local `memory_facts` + `search --as-of`
- Full agent runtimes (Letta) → Engram stays a memory layer + Action Ladder

See [`COMPARISON.md`](COMPARISON.md) for the product fence (ADR-0009).
