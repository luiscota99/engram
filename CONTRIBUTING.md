# Contributing to Engram

Thanks for your interest in contributing! Engram is a persistent memory system for AI-assisted development.

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Run `bash scripts/setup.sh` to set up the development environment
4. Make your changes
5. Test with `engram stats` and `engram search "test"` (after `pip install -e ".[dev]"` from the repo root)
6. Submit a pull request

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/engram.git
cd engram
bash scripts/setup.sh
```

Install the package in editable mode with dev extras (declared in [`pyproject.toml`](pyproject.toml)):

```bash
pip install -e ".[dev]"
```

Optionally enable the git hooks so lint/format run before each commit:

```bash
pre-commit install
```

### Key Dependencies

Declared in [`pyproject.toml`](pyproject.toml) (`[project]` dependencies and `[project.optional-dependencies] dev`):

- **sqlite-vec** — vector search
- **sqlean-py** — SQLite with FTS5 and extensions as used by Engram
- **Ollama** (runtime, not pip) — local embeddings; default model `nomic-embed-text`

## Architecture

```
src/
├── cli/                 # CLI entry: main.py (parser), commands/*.py
├── mcp/                 # MCP: protocol.py, handlers.py, tools_schema.py, constants.py
├── mcp_server.py        # Thin launcher; stdio MCP (see mcp/protocol.py)
├── database.py          # SQLite schema, migrations, FTS5, connections
├── search.py            # Hybrid FTS5 + semantic search, ranking hooks
├── embeddings.py        # Ollama embedding client
├── ranking.py           # Multi-factor and BM25 ranking
├── workflow.py          # Committee / session phase state
├── doctor.py            # Diagnostics and repairs
└── seed.py              # Initial seed data / OOBE helpers
```

CLI entry point: `engram` → `src.cli:main` ([`pyproject.toml`](pyproject.toml) `[project.scripts]`).

Developers without installing the package should run from the **repository root**:

```bash
PYTHONPATH=. python3 -m src.cli --help
```

## Guidelines

- **Python 3.9+** compatible.
- **Hybrid search** — New memory types with searchable text should be indexed in FTS5 and, where appropriate, the vector table (`vec_memory`); see [`database.py`](src/database.py) and [`search.py`](src/search.py).
- **MCP parity** — New user-facing memory operations should appear in both CLI (under [`src/cli/commands/`](src/cli/commands/)) and MCP ([`src/mcp/tools_schema.py`](src/mcp/tools_schema.py) + [`src/mcp/handlers.py`](src/mcp/handlers.py)).
- **Dependencies** — Prefer the standard library where possible. New Python dependencies belong in [`pyproject.toml`](pyproject.toml) `[project] dependencies` or `optional-dependencies`, not a separate `requirements.txt`.

## Adding a New Memory Type

1. Add the table in [`database.py`](src/database.py) (`SCHEMA_SQL`) and bump migrations in [`migrations.py`](src/migrations.py) if needed.
2. Add CLI commands or flags under [`src/cli/commands/`](src/cli/commands/) and wire them in [`src/cli/main.py`](src/cli/main.py).
3. Add MCP tool definitions and handlers in [`src/mcp/tools_schema.py`](src/mcp/tools_schema.py) and [`src/mcp/handlers.py`](src/mcp/handlers.py).
4. Update indexing in [`database.py`](src/database.py) (`index_in_fts`, vector rows if applicable).
5. Update [`README.md`](README.md) for users.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

CI runs the suite on Python 3.9–3.12 and enforces a **coverage gate of 92%**
(configured in [`pyproject.toml`](pyproject.toml) `[tool.coverage.report] fail_under`).
Run it the same way locally before opening a PR:

```bash
pytest --cov=src --cov-report=term-missing tests/
```

The only modules omitted from the gate are the curses TUI (`src/browse.py`) and
the `python -m` shim (`src/cli/__main__.py`) — everything else is expected to be
tested. CI also runs a retrieval benchmark with an **R@5 ≥ 0.90 gate**.

Manual checks:

```bash
engram doctor
engram search "alpha compositing"
```

### Generated docs

[`docs/COMMANDS.md`](docs/COMMANDS.md) is **generated** from the argparse parser —
do not edit it by hand. If you add or change a CLI command, regenerate it:

```bash
python scripts/gen_docs.py           # rewrite docs/COMMANDS.md
python scripts/gen_docs.py --check   # CI-style check: fails if out of date
```

## Linting and types

- **Ruff:** `ruff check src/ tests/ benchmarks/`
- **Pyright:** `pyright` (configured in [`pyproject.toml`](pyproject.toml); key modules under `src/mcp/`, `src/cli/`, core search/DB)

## Architecture Decision Records

Non-trivial design choices are recorded as ADRs under
[`docs/decisions/`](docs/decisions/) (numbered `NNNN-title.md`). If your change
alters a load-bearing behavior — storage backend, ranking, the action ladder,
the extraction/trust model — add a new ADR describing the context, the decision,
and the alternatives you rejected. Small changes don't need one.

## Before you open a PR

- [ ] `ruff check src/ tests/ benchmarks/` is clean
- [ ] `pyright` reports 0 errors
- [ ] `pytest --cov=src` passes and stays at/above the 92% gate
- [ ] If you touched the CLI, `python scripts/gen_docs.py --check` passes
- [ ] New user-facing operations exist in **both** the CLI and MCP surfaces
- [ ] User-facing changes are reflected in [`README.md`](README.md) and [`CHANGELOG.md`](CHANGELOG.md)

## Reporting Issues

Open a GitHub issue with:

- What you expected to happen.
- What actually happened.
- Your Python version (`python3 --version`).
- Your SQLite version (`python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`).
