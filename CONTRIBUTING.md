# Contributing to Engram

Thanks for your interest in contributing! Engram is a persistent memory system for AI-assisted development.

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Run `bash scripts/install.sh` to set up the development environment
4. Make your changes
5. Test with `engram stats` and `engram search "test"`
6. Submit a pull request

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/engram.git
cd engram
python3 -m src.cli init
python3 -m src.cli seed  # optional: populate with sample data
```

No external dependencies required — Engram uses only Python 3 standard library.

## Architecture

```
src/
├── cli.py           # CLI entry point and argument parsing
├── mcp_server.py    # MCP server for Cursor/Claude Desktop
├── database.py      # SQLite schema, connections, FTS5
├── search.py        # Full-text search logic
└── seed.py          # Sample data for testing
```

## Guidelines

- **Zero dependencies** — only Python stdlib. No pip packages.
- **Python 3.9+** compatible
- **SQLite FTS5** for all search functionality
- Keep the CLI interface simple and Unix-like
- All output to `stdout`; debugging/logs to `stderr` (especially in MCP server)

## Adding a New Memory Type

1. Add the table schema in `database.py` (`SCHEMA_SQL`)
2. Add the handler functions in `cli.py` (add, list)
3. Add the MCP tool definition and handler in `mcp_server.py`
4. Update the FTS index in `database.py` (`index_in_fts`)
5. Update `README.md`

## Testing

```bash
# Initialize fresh database
python3 -m src.cli init

# Seed with sample data
python3 -m src.cli seed

# Verify everything works
python3 -m src.cli stats
python3 -m src.cli search "alpha compositing"
python3 -m src.cli list mistakes
python3 -m src.cli list patterns
python3 -m src.cli list skills

# Test MCP server
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python3 src/mcp_server.py
```

## Reporting Issues

Open a GitHub issue with:
- What you expected to happen
- What actually happened
- Your Python version (`python3 --version`)
- Your SQLite version (`python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`)
