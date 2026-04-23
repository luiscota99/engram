#!/usr/bin/env bash
# Engram CLI launcher: prefer a globally installed `engram` on PATH; else run
# from a local checkout if ENGRAM_ROOT is set to the engram repository root.
set -e
if command -v engram >/dev/null 2>&1; then
  exec engram "$@"
fi
if [ -n "${ENGRAM_ROOT:-}" ] && [ -d "$ENGRAM_ROOT" ] && [ -f "$ENGRAM_ROOT/src/cli/__init__.py" ]; then
  (cd "$ENGRAM_ROOT" && exec python3 -m src.cli "$@")
  exit $?
fi
echo "engram: command not found. Install: pipx install engram, uv tool install engram, or from a clone: pip install -e ." >&2
echo "  (Developers can set ENGRAM_ROOT to a checkout and re-run this script.)" >&2
exit 127
