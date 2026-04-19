#!/bin/bash
# Install Engram locally.
# Creates a shell function 'engram' that runs the CLI directly (no Docker overhead).

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SHELL_RC="$HOME/.zshrc"

# Detect shell
if [ -n "$BASH_VERSION" ]; then
    SHELL_RC="$HOME/.bashrc"
elif [ -n "$ZSH_VERSION" ] || [ "$SHELL" = "/bin/zsh" ]; then
    SHELL_RC="$HOME/.zshrc"
fi

FUNC_LINE="engram() { ( cd \"$SCRIPT_DIR\" && python3 -m src.cli \"\$@\" ) }"

echo "Installing Engram..."
echo ""

# Initialize the database
echo "→ Initializing database..."
(cd "$SCRIPT_DIR" && python3 -m src.cli init)

# Seed with historical data
echo "→ Seeding historical data..."
(cd "$SCRIPT_DIR" && python3 -m src.cli seed)

# Add shell function
if grep -q "# engram" "$SHELL_RC" 2>/dev/null; then
    echo "→ Shell function already exists in $SHELL_RC"
else
    echo "" >> "$SHELL_RC"
    echo "# engram" >> "$SHELL_RC"
    echo "$FUNC_LINE" >> "$SHELL_RC"
    echo "→ Added 'engram' function to $SHELL_RC"
fi

echo ""
echo "✓ Done! Run 'source $SHELL_RC' then try: engram stats"
