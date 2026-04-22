#!/bin/bash
# Engram Unified Setup Script
# Handles dependencies, database initialization, and semantic engine configuration.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${CYAN}${BOLD}🧠 Engram — Unified Setup${NC}"
echo -e "${BLUE}===========================${NC}\n"

# 1. Check Python version
echo -e "${BOLD}[1/5] Checking environment...${NC}"
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}Error: python3 is not installed or not on PATH.${NC}"
    exit 1
fi
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
    echo -e "${RED}Error: Engram requires Python 3.9 or higher. Found $PYTHON_VERSION${NC}"
    exit 1
fi
echo -e "✓ Python $PYTHON_VERSION found."

# 2. Install dependencies
echo -e "\n${BOLD}[2/5] Installing dependencies...${NC}"
pip install -r requirements.txt
pip install -e .
echo -e "✓ Dependencies installed."

# 3. Check for Ollama (Semantic Search)
echo -e "\n${BOLD}[3/5] Checking Semantic Engine (Ollama)...${NC}"
if command -v ollama >/dev/null 2>&1; then
    echo -e "✓ Ollama is installed."
    if ollama list | grep -q "nomic-embed-text"; then
        echo -e "✓ Model 'nomic-embed-text' is already pulled."
    else
        echo -e "${BLUE}Pulling 'nomic-embed-text' model for semantic search...${NC}"
        ollama pull nomic-embed-text
        echo -e "✓ Model pulled successfully."
    fi
else
    echo -e "${RED}Warning: Ollama not found.${NC}"
    echo -e "Semantic search (vector embeddings) will not work without Ollama."
    echo -e "Install it from https://ollama.ai if you want local semantic memory."
fi

# 4. Initialize Database
echo -e "\n${BOLD}[4/5] Initializing Engram Database...${NC}"
engram init
echo -e "✓ Database initialized at ~/.engram/memory.db"

# 5. Seed with Professional Patterns
echo -e "\n${BOLD}[5/5] Seeding memory with engineering patterns...${NC}"
engram seed
echo -e "✓ Memory seeded with initial skills and patterns."

echo -e "\n${GREEN}${BOLD}Setup Complete!${NC}"
echo -e "You can now use \`${BOLD}engram search${NC}\` to query your memory."
echo -e "Run \`${BOLD}engram bootstrap${NC}\` in any project to integrate Cursor and Antigravity."
