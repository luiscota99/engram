"""Bootstrap, seed, and init commands."""
from __future__ import annotations

import json
import os
import shutil
import sys

from ...database import get_db_path, init_db
from ...seed import seed_database
from ..fmt import fmt_bold, fmt_dim, fmt_header

_BOOTSTRAP_MODE_DESCRIPTIONS = {
    "adaptive": "LIGHT by default, escalates automatically when complexity is detected (recommended)",
    "full":     "Always-on — session init, memory search, and retrospective for every session",
    "minimal":  "Off by default — only activates when you explicitly say 'use engram' or 'check memory'",
}

_CURSOR_RULE_SOURCES = {
    "adaptive": "engram-adaptive.mdc",
    "full":     "engram-committee.mdc",
    "minimal":  "engram.mdc",
}

_AG_SKILL_SOURCES = {
    "adaptive": "engram-adaptive-workflow.md",
    "full":     "engram-committee-workflow.md",
    "minimal":  None,
}

# Antigravity loads global cross-tool rules from ~/.gemini/AGENTS.md (see Google Antigravity docs).
_GLOBAL_AGENTS_BEGIN = "<!-- engram-global:begin -->"
_GLOBAL_AGENTS_END = "<!-- engram-global:end -->"

# If present at project root (or bootstrap is run with --omit-project-integration),
# do not copy Cursor rules or write .antigravity/instructions.md.
_OMIT_PROJECT_INTEGRATION_SENTINEL = ".omit-agent-integration"


def _omit_project_integration_files(project_root: str, args) -> bool:
    if getattr(args, "omit_project_integration", False):
        return True
    return os.path.isfile(os.path.join(project_root, _OMIT_PROJECT_INTEGRATION_SENTINEL))


def _global_antigravity_agents_body() -> str:
    return """## Engram — global engineering memory (all workspaces)

- **CLI:** `engram` must be on your `PATH` (`pip install -e .` from the Engram repo, `pipx`, or `uv tool install`). Run commands from the **current project directory**. Default database: **`~/.engram/memory.db`** (set `ENGRAM_DB_PATH` only if you intentionally want a separate corpus).
- **Session start:** `engram search "<keywords>" -n 3` — search uses the current working directory for project affinity unless you pass `engram search --no-project`.
- **Session end (optional):** `engram import-session-summary` if you keep a `session_summary.md`; `engram suggest-capture` for heuristics; `engram session-review` for the full retrospective checklist.
- **Richer per-repo rules:** run `engram bootstrap` in a repository to add `.antigravity/instructions.md` and Cursor rules alongside this global hint.
- **Repository:** https://github.com/luismiguelcota/engram"""


def write_global_antigravity_agents_snippet(home: str | None = None) -> tuple[bool, str]:
    """Insert or update the Engram block in ~/.gemini/AGENTS.md (idempotent).

    Returns (success, path_written).
    """
    base = home or os.path.expanduser("~")
    gemini_dir = os.path.join(base, ".gemini")
    path = os.path.join(gemini_dir, "AGENTS.md")
    inner = _global_antigravity_agents_body().strip()
    block = f"{_GLOBAL_AGENTS_BEGIN}\n{inner}\n{_GLOBAL_AGENTS_END}\n"
    os.makedirs(gemini_dir, exist_ok=True)

    existing = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            existing = f.read()

    if _GLOBAL_AGENTS_BEGIN in existing and _GLOBAL_AGENTS_END in existing:
        before, _, tail = existing.partition(_GLOBAL_AGENTS_BEGIN)
        _, _, after = tail.partition(_GLOBAL_AGENTS_END)
        new_content = before.rstrip() + "\n\n" + block + "\n" + after.lstrip()
    else:
        sep = "\n\n" if existing.strip() else ""
        new_content = existing.rstrip() + sep + block

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return True, path


def cmd_antigravity_global(args):
    """Standalone: only write ~/.gemini/AGENTS.md snippet (no project files)."""
    _ = args
    if not os.path.exists(get_db_path()):
        init_db()
        print(f"✓ Initialized database at {get_db_path()}")
    ok, path = write_global_antigravity_agents_snippet()
    if ok:
        print(f"✓ Updated Antigravity global rules: {path}")
        print(
            fmt_dim(
                "Antigravity merges ~/.gemini/AGENTS.md in every workspace. "
                "If you use Gemini CLI too, note both ecosystems may share ~/.gemini/ — "
                "see Engram README (Global CLI section)."
            )
        )


def _prompt_bootstrap_mode() -> str:
    print(fmt_header("\nChoose Engram engagement mode:\n"))
    options = [
        ("adaptive", "Adaptive (recommended)"),
        ("full",     "Full — always-on"),
        ("minimal",  "Minimal — manual triggers only"),
    ]
    for i, (key, label) in enumerate(options, 1):
        desc = _BOOTSTRAP_MODE_DESCRIPTIONS[key]
        print(f"  {fmt_bold(str(i))}. {fmt_bold(label)}")
        print(fmt_dim(f"     {desc}"))
        print()
    while True:
        try:
            raw = input("Enter choice [1/2/3] (default: 1): ").strip()
            if raw in ("", "1"):
                return "adaptive"
            elif raw == "2":
                return "full"
            elif raw == "3":
                return "minimal"
            else:
                print(fmt_dim("  Please enter 1, 2, or 3."))
        except (EOFError, KeyboardInterrupt):
            print(fmt_dim("\n  Non-interactive environment detected. Defaulting to adaptive mode."))
            return "adaptive"


def _setup_mcp_config(engram_root: str) -> tuple[bool, str]:
    cursor_dir = os.path.expanduser("~/.cursor")
    mcp_path = os.path.join(cursor_dir, "mcp.json")
    server_script = os.path.join(engram_root, "src", "mcp_server.py")
    new_entry = {"command": "python3", "args": [server_script], "enabled": True, "timeout": 30}
    try:
        os.makedirs(cursor_dir, exist_ok=True)
        config = {}
        if os.path.exists(mcp_path):
            with open(mcp_path, "r") as f:
                config = json.load(f)
        config.setdefault("mcpServers", {})
        if "engram" in config["mcpServers"]:
            return True, f"MCP config already has 'engram' entry in {mcp_path} (skipped)"
        config["mcpServers"]["engram"] = new_entry
        with open(mcp_path, "w") as f:
            json.dump(config, f, indent=2)
        return True, f"✓ Added Engram MCP server to {mcp_path}"
    except Exception as e:
        return False, f"Warning: Could not update MCP config: {e}"


def cmd_bootstrap(args):
    import urllib.request as _urllib_req

    project_root = os.getcwd()
    # __file__ is src/cli/commands/bootstrap.py → package root is four levels up
    engram_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(fmt_header("Engram database not found. Initializing..."))
        init_db()
        print(f"✓ Created database at {db_path}")

    omit_project = _omit_project_integration_files(project_root, args)
    mode = getattr(args, "mode", None)
    if omit_project:
        mode = mode or "adaptive"
    elif not mode:
        mode = _prompt_bootstrap_mode()
    if mode not in _CURSOR_RULE_SOURCES:
        print(f"Unknown mode '{mode}'. Choose from: adaptive, full, minimal")
        sys.exit(1)

    if omit_project:
        print(
            fmt_header(
                "\nBootstrapping (database / MCP); "
                f"{fmt_bold(_OMIT_PROJECT_INTEGRATION_SENTINEL)} or --omit-project-integration prevents "
                "Cursor rules / Antigravity instructions in this workspace."
            )
        )
        print()
    else:
        print(fmt_header(f"\nBootstrapping in {fmt_bold(mode.upper())} mode"))
        print(fmt_dim(f"  {_BOOTSTRAP_MODE_DESCRIPTIONS[mode]}\n"))

    if not omit_project:
        # Cursor rule
        cursor_rules_dir = os.path.join(project_root, ".cursor", "rules")
        os.makedirs(cursor_rules_dir, exist_ok=True)
        source_cursor = os.path.join(engram_root, "cursor-rules", _CURSOR_RULE_SOURCES[mode])
        dest_cursor = os.path.join(cursor_rules_dir, "engram.mdc")
        if os.path.exists(source_cursor):
            shutil.copy2(source_cursor, dest_cursor)
            print(f"✓ Created {os.path.join('.cursor', 'rules', 'engram.mdc')}  [{mode} mode]")
        else:
            print(fmt_dim(f"Warning: Source rule not found: {source_cursor}"))

        # Antigravity instructions
        antigravity_dir = os.path.join(project_root, ".antigravity")
        os.makedirs(antigravity_dir, exist_ok=True)
        ag_instructions = os.path.join(antigravity_dir, "instructions.md")
        ag_skill_file = _AG_SKILL_SOURCES[mode]
        with open(ag_instructions, "w") as f:
            f.write("# Engram Project Instructions\n\n")
            f.write(f"Engagement mode: **{mode.upper()}** — {_BOOTSTRAP_MODE_DESCRIPTIONS[mode]}\n\n")
            f.write("You are operating in a project backed by the **Engram Persistent Memory System**.\n\n")
            if ag_skill_file:
                source_ag = os.path.join(engram_root, "antigravity-skills", ag_skill_file)
                if os.path.exists(source_ag):
                    with open(source_ag, "r") as src:
                        f.write(src.read())
                else:
                    print(fmt_dim(f"Warning: Antigravity skill file not found: {source_ag}"))
            else:
                f.write("## Engram Usage\n\nEngram is available but **off by default** for this project.\n\n")
                f.write("Activate by saying:\n- `use engram` — enables full memory search\n- `no engram` — keeps disabled\n")
        print(f"✓ Created {os.path.join('.antigravity', 'instructions.md')}  [{mode} mode]")

    # MCP config
    setup_mcp = getattr(args, "setup_mcp", None)
    if setup_mcp is None:
        try:
            if sys.stdin.isatty():
                answer = input("\n  Configure Engram MCP server in ~/.cursor/mcp.json? [Y/n] ").strip().lower()
                setup_mcp = answer in ("", "y", "yes")
            else:
                setup_mcp = True
        except Exception:
            setup_mcp = True

    if setup_mcp:
        ok, msg = _setup_mcp_config(engram_root)
        print(f"  {msg}")
    else:
        print(fmt_dim("  Skipped MCP setup. Add the Engram server to ~/.cursor/mcp.json manually."))
        print(fmt_dim("  See: https://github.com/luismiguelcota/engram#agent-integration"))

    # Ollama status
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    ollama_ok = False
    try:
        with _urllib_req.urlopen(_urllib_req.Request(ollama_host, method="GET"), timeout=2) as resp:
            ollama_ok = resp.status == 200
    except Exception:
        pass

    if ollama_ok:
        ollama_status = "✓ Ollama reachable — hybrid semantic+lexical search active"
    else:
        ollama_status = (
            "! Ollama not reachable — search will use lexical-only mode\n"
            "    Install Ollama (https://ollama.ai) and run: ollama pull nomic-embed-text"
        )

    print(fmt_header(f"\nProject successfully bootstrapped! ({mode} mode)"))
    if omit_project:
        print(
            fmt_dim(
                "  Per-repo Cursor / Antigravity workspace files omitted — see "
                f"{_OMIT_PROJECT_INTEGRATION_SENTINEL} or `--omit-project-integration`."
            )
        )
    elif mode == "adaptive":
        print("  Cursor and Antigravity will use LIGHT mode by default.")
    elif mode == "full":
        print("  Cursor and Antigravity will use the full Committee Workflow for every session.")
    else:
        print("  Memory is disabled by default. Say 'use engram' to activate it.")

    print(f"\n{fmt_bold('Status:')}")
    print(f"  ✓ DB:     {get_db_path()}")
    if omit_project:
        print("  ⊗ Rule:   (skipped) .cursor/rules/engram.mdc")
        print("  ⊗ Guide:  (skipped) .antigravity/instructions.md")
    else:
        print(f"  ✓ Rule:   .cursor/rules/engram.mdc  [{mode} mode]")
        print(f"  ✓ Guide:  .antigravity/instructions.md  [{mode} mode]")
    for line in ollama_status.splitlines():
        print(f"  {line}")

    print(f"\n{fmt_bold('Next Steps:')}")
    print(f"  1. Ensure `{fmt_bold('engram')}` is on your PATH (e.g. `pip install -e .` from this repo, or `pipx` / `uv tool install`) so Antigravity can run CLI commands from any project directory.")
    print(f"  2. Run `{fmt_bold('engram index-project')}` to map this codebase")
    print(f"  3. Run `{fmt_bold('engram sync-skills')}` to sync any existing Cursor skills into memory")
    if mode != "adaptive":
        print(fmt_dim("\n  Tip: Re-run `engram bootstrap --mode adaptive` to switch to adaptive mode."))

    if getattr(args, "global_antigravity", False):
        _, ag_path = write_global_antigravity_agents_snippet()
        print(f"\n{fmt_bold('Antigravity (all workspaces):')}")
        print(f"  ✓ Appended/updated Engram block in {ag_path}")
        print(
            fmt_dim(
                "  Use `engram antigravity-global` anytime to refresh this block without re-bootstrapping the project."
            )
        )


def cmd_seed(args):
    seed_database()


def cmd_init(args):
    init_db()
    print(f"✓ Database initialized at {get_db_path()}")
