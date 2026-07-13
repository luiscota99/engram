<!-- Thanks for contributing to Engram! Keep this description short and specific. -->

## What & why

<!-- What does this change do, and what problem does it solve? Link any related issue. -->

## Checklist

- [ ] `ruff check src/ tests/ benchmarks/` is clean
- [ ] `pyright` reports 0 errors
- [ ] `pytest --cov=src` passes and stays at/above the 92% coverage gate
- [ ] If the CLI changed, `python scripts/gen_docs.py --check` passes
- [ ] New user-facing operations exist in **both** the CLI and MCP surfaces
- [ ] User-facing changes are reflected in `README.md` / `CHANGELOG.md`
- [ ] A load-bearing design change is recorded as an ADR under `docs/decisions/`

## Notes for reviewers

<!-- Anything non-obvious: trade-offs, follow-ups, areas you're unsure about. -->
