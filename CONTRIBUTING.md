# Contributing to Engram

Thanks for your interest in contributing! Engram is a persistent memory system for AI-assisted development.

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Run `bash scripts/setup.sh` to set up the development environment
4. Make your changes
5. Test with `engram stats` and `engram search "test"`
6. Submit a pull request

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/engram.git
cd engram
bash scripts/setup.sh
```

### Key Dependencies

Engram aims for a minimal footprint but relies on the following for advanced features:
- **sqlite-vec**: For vector search/embeddings.
- **sqlean-py**: For advanced SQLite extensions (FTS5).
- **Ollama**: For generating local text embeddings (`nomic-embed-text`).

## Architecture

```
src/
├── cli.py           # CLI entry point and argument parsing
├── mcp_server.py    # MCP server for Cursor/Claude Desktop
├── database.py      # SQLite schema, connections, FTS5
├── search.py        # Full-text search and hybrid ranking
├── embeddings.py    # Ollama integration for vector search
├── doctor.py        # Diagnostic and repair tools
└── seed.py          # Professional engineering patterns for OOBE
```

## Guidelines

- **Python 3.9+** compatible.
- **Hybrid Search** — Always ensure new memory types are indexed in both FTS5 and the vector table if they contain descriptive text.
- **Agent Focus** — Keep the MCP server (`mcp_server.py`) up to date with any new tools or memory types.
- **Zero Bloat** — Use standard library where possible. External dependencies must be justified and added to `requirements.txt`.

## Adding a New Memory Type

1. Add the table schema in `database.py` (`SCHEMA_SQL`).
2. Add the handler functions in `cli.py` (add, list).
3. Add the MCP tool definition and handler in `mcp_server.py`.
4. Update the indexing logic in `database.py` (`index_in_fts`).
5. Update `README.md` to reflect the new capability.

## Testing

Engram uses `pytest` for automated testing:

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest
```

Manual verification:
```bash
# Run diagnostics
engram doctor

# Test search
engram search "alpha compositing"
```

## Reporting Issues

Open a GitHub issue with:
- What you expected to happen.
- What actually happened.
- Your Python version (`python3 --version`).
- Your SQLite version (`python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`).
