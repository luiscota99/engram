# 🧠 Engram

**Persistent memory for AI-assisted development.** Tracks mistakes, patterns, skills, and conversation history across sessions with full-text search.

Engram gives AI coding assistants long-term memory — so they stop repeating the same mistakes, reuse proven workflows, and recognize familiar problems instantly.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-green.svg)](https://python.org)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero-orange.svg)](#architecture)

## Why Engram?

AI assistants forget everything between sessions. Engram fixes that by maintaining a **queryable memory database** that persists across conversations:

- **Mistakes** — "We tried flood-fill on alpha edges before, it doesn't work. Use the tinting approach."
- **Patterns** — "This looks like the API Parameter Mismatch pattern. Look up the ID from the listing endpoint first."
- **Skills** — "There's already a proven workflow for this. Follow steps 1-5 instead of figuring it out again."
- **Conversations** — "Last session we made these decisions. Here's where we left off."

## Quick Start

### Install locally (recommended)

```bash
git clone https://github.com/luismiguelcota/engram.git
cd engram
bash scripts/install.sh
source ~/.zshrc
```

### Try it out

```bash
engram stats                         # See what's in memory
engram search "alpha compositing"    # Full-text search
engram list skills                   # Browse learned workflows
engram recent -n 5                   # Last 5 entries
```

### Docker

```bash
docker compose build
docker compose run --rm engram stats
docker compose run --rm engram search "query"
```

## IDE Integration

### Cursor IDE

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "engram": {
      "command": "python3",
      "args": ["/absolute/path/to/engram/src/mcp_server.py"],
      "enabled": true,
      "timeout": 30
    }
  }
}
```

Then copy the Cursor rule for automatic agent integration:

```bash
cp cursor-rules/engram.mdc ~/.cursor/rules/
```

### Cursor via Docker

```json
{
  "mcpServers": {
    "engram": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "~/.engram:/data",
        "engram"
      ],
      "enabled": true,
      "timeout": 30
    }
  }
}
```

### Claude Desktop

Add the same config to your Claude Desktop MCP settings.

## CLI Reference

### Search

```bash
engram search "query"                    # Full-text search across all memory
engram search "query" -t mistake         # Filter by type
engram search --tags python,pillow       # Filter by tags
engram search "black edges" -n 3         # Limit results
```

### Browse

```bash
engram recent                            # Last 10 entries
engram recent -n 5 -t skill             # Last 5 skills
engram list mistakes                     # All mistakes
engram list patterns                     # All patterns
engram list skills                       # All skills
engram list conversations                # All conversations
engram list prompts                      # All system prompts
engram stats                             # Database overview
```

### Add Entries

```bash
# Log a mistake
engram add mistake \
  --date 2026-04-19 \
  --context "Building image pipeline" \
  --mistake "Forgot color bleed in unified script" \
  --root-cause "Feature loss during script unification" \
  --fix "Ported flood-fill logic from source script" \
  --prevention "Checklist all source features before unifying" \
  --tags "python,image-processing"

# Log a pattern
engram add pattern \
  --name "Alpha Compositing Edge Artifacts" \
  --symptoms "Dark fringes at transparent edges" \
  --root-cause "Anti-aliased pixels blend with wrong color" \
  --fix "Tint overlay using alpha mask" \
  --tags "image-processing,pillow"

# Log a skill
engram add skill \
  --name "Image Proxy Pipeline" \
  --domain "image-processing" \
  --trigger "User wants print-ready proxy cards" \
  --workflow "1. Fetch → 2. Upscale → 3. Bleed → 4. Frame → 5. Save" \
  --pitfalls "API IDs != display names; tint frame alpha edges" \
  --tags "python,pillow"

# Log a conversation
engram add conversation \
  --id "abc123-def456" \
  --title "Built Image Processing Pipeline" \
  --date 2026-04-19 \
  --domain "image-processing" \
  --tasks "Built proxy pipeline, fixed alpha compositing" \
  --tags "python,pillow"

# Log an LLM Prompt
engram add prompt \
  --name "Log Analyzer" \
  --role "Expert system/application log analyst" \
  --domain "debugging" \
  --description "Analyzes logs, traces, and detects errors" \
  --file "/path/to/Prompt.md" \
  --tags "debugging,logs"
```

### Advanced Operations

```bash
# Suggest a prompt for a task
engram suggest "need to write an optimized CV for a software job"

# Import skills from KS Cursor Orchestrator
engram import-skills ~/.cursor/skills/

# Link a pattern to a conversation
engram link-pattern "Alpha Compositing Edge Artifacts" \
  --conversation "abc123" \
  --date 2026-04-19 \
  --notes "Seen during frame overlay"
```

## MCP Tools

When connected via MCP, AI agents get these tools:

| Tool | Description |
|------|-------------|
| `memory_search` | Full-text search across all memory types |
| `memory_recent` | Get most recent entries |
| `memory_add_mistake` | Log a mistake with root cause analysis |
| `memory_add_pattern` | Log a recurring issue pattern |
| `memory_add_skill` | Log a reusable workflow |
| `memory_add_conversation` | Log a session summary |
| `memory_list` | List all entries of a type |
| `memory_stats` | Database statistics |

## Memory Types

| Type | Purpose | Example |
|------|---------|---------|
| **Mistakes** | Error instances with root cause and prevention | "Forgot color bleed when merging scripts" |
| **Patterns** | Recurring issue types with standard solutions | "Alpha edges cause fringes → tint with mask" |
| **Skills** | Reusable multi-step workflows | "Image proxy pipeline: fetch → upscale → bleed → frame" |
| **Conversations** | Structured session summaries | "Built proxy pipeline, made 3 key decisions" |
| **Prompts** | Reusable LLM system prompts | "Log Analyzer: extracts errors from raw log dumps" |
| **Tags** | Cross-cutting labels for filtering | `python`, `image-processing`, `api` |

## Architecture

```
engram/
├── src/
│   ├── cli.py           # CLI entry point (engram command)
│   ├── mcp_server.py    # MCP server (Cursor/Claude integration)
│   ├── database.py      # SQLite schema + FTS5
│   ├── search.py        # Full-text search logic
│   └── seed.py          # Sample data for bootstrapping
├── cursor-rules/
│   └── engram.mdc       # Cursor rule for auto-integration
├── data/                # Database volume (Docker)
├── scripts/
│   └── install.sh       # Local installation
├── Dockerfile
├── docker-compose.yml
├── LICENSE              # MIT
├── CONTRIBUTING.md
└── CHANGELOG.md
```

### Technical Details

- **Database:** SQLite with FTS5 full-text search
- **Dependencies:** Zero — Python 3.9+ standard library only
- **Storage:** Single `.db` file at `~/.engram/memory.db`
- **Transport:** JSON-RPC over stdio (MCP protocol)
- **Concurrency:** WAL mode for safe concurrent reads
- **Portability:** Copy the `.db` file to migrate all memory

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ENGRAM_DB_PATH` | `~/.engram/memory.db` | Database file location |

## Portability

The entire memory is a single SQLite file. To migrate to a new machine:

1. Copy `~/.engram/memory.db` to the new machine
2. Clone this repo
3. Run `bash scripts/install.sh`

Or with Docker — mount the database as a volume.

## License

[MIT](LICENSE) — Luis Miguel Cota
