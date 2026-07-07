"""Engram CLI — argument parser and entry point."""
from __future__ import annotations

import argparse

from ..database import init_db
from .commands.bootstrap import (
    cmd_antigravity_global,
    cmd_bootstrap,
    cmd_claude_skill,
    cmd_import_claude_memories,
    cmd_init,
    cmd_install,
    cmd_seed,
)
from .commands.codebase import (
    cmd_clean_codebase,
    cmd_graph,
    cmd_index_project,
    cmd_query_codebase,
)
from .commands.llm import cmd_llm
from .commands.maintenance import (
    cmd_backup,
    cmd_doctor,
    cmd_efficiency,
    cmd_gc,
    cmd_health,
    cmd_merge_projects,
    cmd_migrate,
    cmd_migrate_embeddings,
    cmd_reembed,
    cmd_sleep,
)
from .commands.memory import (
    cmd_add,
    cmd_consolidate,
    cmd_link_pattern,
    cmd_list,
    cmd_recent,
    cmd_search,
    cmd_session_help,
    cmd_stats,
    cmd_suggest,
    cmd_suggest_capture,
    cmd_suggest_consolidate,
)
from .commands.reflex import cmd_promote, cmd_reflex, cmd_route
from .commands.session import (
    cmd_get_role,
    cmd_get_session,
    cmd_import_session_summary,
    cmd_session_review,
)
from .commands.sync import (
    cmd_export_skills,
    cmd_import_cursor_skills,
    cmd_import_skills,
    cmd_sync_skills,
)
from .commands.tools import (
    cmd_benchmark,
    cmd_browse,
    cmd_retrieval_benchmark,
    cmd_run,
    cmd_simulate,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engram",
        description="Engram — persistent memory for AI-assisted development",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── Memory ──────────────────────────────────────────────────────
    p_search = sub.add_parser("search", help="Search all memory")
    p_search.add_argument("query", nargs="*", help="Search query")
    p_search.add_argument("-t", "--type", choices=["mistake", "pattern", "skill", "conversation", "prompt"])
    p_search.add_argument("--tags", help="Comma-separated tags")
    p_search.add_argument("-n", "--limit", type=int, default=20)
    p_search.add_argument(
        "--project",
        metavar="DIR",
        default=None,
        help="Project directory for affinity ranking (default: current working directory)",
    )
    p_search.add_argument(
        "--no-project",
        action="store_true",
        help="Disable project-scoped affinity (search global memory only)",
    )
    p_search.add_argument(
        "--include-superseded",
        action="store_true",
        help="Include superseded/invalidated memories in results",
    )
    p_search.set_defaults(func=cmd_search)

    p_recent = sub.add_parser("recent", help="Show recent entries")
    p_recent.add_argument("-n", type=int, default=10)
    p_recent.add_argument("-t", "--type", choices=["mistake", "pattern", "skill", "conversation", "prompt"])
    p_recent.set_defaults(func=cmd_recent)

    p_add = sub.add_parser("add", help="Add a new entry")
    add_sub = p_add.add_subparsers(dest="kind")

    p_am = add_sub.add_parser("mistake", help="Log a mistake")
    p_am.add_argument("--date", required=True)
    p_am.add_argument("--context", required=True)
    p_am.add_argument("--mistake", required=True)
    p_am.add_argument("--root-cause")
    p_am.add_argument("--fix", required=True)
    p_am.add_argument("--prevention")
    p_am.add_argument("--conversation")
    p_am.add_argument("--tags")
    p_am.add_argument("--force", action="store_true", help="Skip duplicate check")

    p_ap = add_sub.add_parser("pattern", help="Log a pattern")
    p_ap.add_argument("--name", required=True)
    p_ap.add_argument("--symptoms", required=True)
    p_ap.add_argument("--root-cause", required=True)
    p_ap.add_argument("--fix", required=True)
    p_ap.add_argument("--tags")
    p_ap.add_argument("--force", action="store_true", help="Skip duplicate check")

    p_as = add_sub.add_parser("skill", help="Log a skill")
    p_as.add_argument("--name", required=True)
    p_as.add_argument("--domain", required=True)
    p_as.add_argument("--trigger", required=True)
    p_as.add_argument("--workflow", required=True)
    p_as.add_argument("--pitfalls")
    p_as.add_argument("--files")
    p_as.add_argument("--dependencies")
    p_as.add_argument("--tags")
    p_as.add_argument("--force", action="store_true", help="Skip duplicate check")

    p_ac = add_sub.add_parser("conversation", help="Log a conversation")
    p_ac.add_argument("--id", required=True)
    p_ac.add_argument("--title", required=True)
    p_ac.add_argument("--date", required=True)
    p_ac.add_argument("--domain", required=True)
    p_ac.add_argument("--tasks")
    p_ac.add_argument("--decisions")
    p_ac.add_argument("--mistakes")
    p_ac.add_argument("--skills")
    p_ac.add_argument("--tags")

    p_asess = add_sub.add_parser("session", help="Initialize a session")
    p_asess.add_argument("--id", required=True)
    p_asess.add_argument("--title", required=True)
    p_asess.add_argument("--date", required=True)
    p_asess.add_argument("--domain", required=True)
    p_asess.add_argument("--workflow-used")

    p_at = add_sub.add_parser("transcript", help="Add transcript entry")
    p_at.add_argument("--session-id", required=True)
    p_at.add_argument("--role", required=True)
    p_at.add_argument("--content", required=True)

    p_ad = add_sub.add_parser("decision", help="Add decision to session")
    p_ad.add_argument("--session-id", required=True)
    p_ad.add_argument("--decision", required=True)
    p_ad.add_argument(
        "--force-bypass",
        action="store_true",
        help="Bypass committee workflow gate (logged)",
    )

    p_apr = add_sub.add_parser("prompt", help="Store an LLM prompt")
    p_apr.add_argument("--name", required=True)
    p_apr.add_argument("--role", required=True)
    p_apr.add_argument("--domain", required=True)
    p_apr.add_argument("--description", required=True)
    p_apr.add_argument("--prompt-text")
    p_apr.add_argument("--file", help="Path to prompt file")
    p_apr.add_argument("--best-for")
    p_apr.add_argument("--tags")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="List entries by type")
    p_list.add_argument("kind", choices=["mistakes", "patterns", "skills", "conversations", "prompts", "sessions"])
    p_list.set_defaults(func=cmd_list)

    p_suggest = sub.add_parser("suggest", help="Suggest a prompt or skill for a task")
    p_suggest.add_argument("query", nargs="*", help="Task description")
    p_suggest.add_argument("-t", "--type", choices=["prompt", "skill", "mistake"], default="prompt")
    p_suggest.add_argument("-n", "--limit", type=int, default=3)
    p_suggest.set_defaults(func=cmd_suggest)

    p_stats = sub.add_parser("stats", help="Show database statistics")
    p_stats.set_defaults(func=cmd_stats)

    p_link = sub.add_parser("link-pattern", help="Link pattern to a conversation")
    p_link.add_argument("name", help="Pattern name")
    p_link.add_argument("--conversation", required=True)
    p_link.add_argument("--date")
    p_link.add_argument("--notes")
    p_link.set_defaults(func=cmd_link_pattern)

    p_cons = sub.add_parser("consolidate", help="Consolidate multiple skills into one")
    p_cons.add_argument("--delete-ids", required=True)
    p_cons.add_argument("--name", required=True)
    p_cons.add_argument("--domain", required=True)
    p_cons.add_argument("--trigger", required=True)
    p_cons.add_argument("--workflow", required=True)
    p_cons.add_argument("--pitfalls")
    p_cons.add_argument("--key-files")
    p_cons.add_argument("--deps")
    p_cons.add_argument("--tags")
    p_cons.set_defaults(func=cmd_consolidate)

    p_sc = sub.add_parser("suggest-consolidate", help="Find near-duplicate memories to consolidate")
    p_sc.add_argument("--threshold", type=float, default=0.80)
    p_sc.add_argument("--type", choices=["mistake", "pattern", "skill"])
    p_sc.add_argument("-n", "--limit", type=int, default=20)
    p_sc.set_defaults(func=cmd_suggest_consolidate)

    p_sc2 = sub.add_parser("suggest-capture", help="Analyze a task and draft memory entries for review")
    p_sc2.add_argument("--task", required=True)
    p_sc2.add_argument("--outcome", required=True)
    p_sc2.add_argument("--errors")
    p_sc2.add_argument("--files", help="Comma-separated list of files changed")
    p_sc2.add_argument(
        "--json",
        action="store_true",
        help="Print raw suggestion dict as JSON (for scripts and agents)",
    )
    p_sc2.set_defaults(func=cmd_suggest_capture)

    p_sh = sub.add_parser(
        "session-help",
        help="Log Session Help Score (0–3) to a local JSONL file for measuring Engram impact",
    )
    p_sh.add_argument(
        "--score",
        type=int,
        required=True,
        help="0–3: how much Engram memories influenced this session (see docs/MEASURING_FIT_AND_HELP.md)",
    )
    p_sh.add_argument("--note", help="Optional one-sentence justification")
    p_sh.add_argument("--task", help="Optional short task label")
    p_sh.set_defaults(func=cmd_session_help)

    # ── Codebase ─────────────────────────────────────────────────────
    p_idx = sub.add_parser("index-project", help="Index project codebase knowledge")
    p_idx.add_argument("--path")
    p_idx.add_argument("--file")
    p_idx.add_argument("--summary")
    p_idx.add_argument("--exports")
    p_idx.add_argument("--deps")
    p_idx.add_argument("--force", action="store_true")
    p_idx.add_argument("--check", action="store_true")
    p_idx.add_argument("--caveman", action="store_true")
    p_idx.add_argument("--caveman-level", choices=["lite", "full", "ultra"], default="full")
    p_idx.add_argument("--llm-summarize", action="store_true")
    p_idx.add_argument("--verbose", action="store_true")
    p_idx.set_defaults(func=cmd_index_project)

    p_qc = sub.add_parser("query-codebase", help="Query indexed codebase knowledge")
    p_qc.add_argument("query", nargs="*")
    p_qc.add_argument("--path")
    p_qc.add_argument("--caveman", action="store_true")
    p_qc.add_argument("--caveman-level", choices=["lite", "full", "ultra"], default="full")
    p_qc.set_defaults(func=cmd_query_codebase)

    p_clean = sub.add_parser("clean-codebase", help="Remove stale entries from codebase knowledge")
    p_clean.add_argument("--path")
    p_clean.set_defaults(func=cmd_clean_codebase)

    p_graph = sub.add_parser("graph", help="Build and visualize file dependency graph")
    p_graph.add_argument("--path")
    p_graph.add_argument("--file")
    p_graph.add_argument("--direction", choices=["outgoing", "incoming", "both"], default="both")
    p_graph.add_argument("--format", choices=["mermaid", "dot", "json"], default="mermaid")
    p_graph.add_argument("--output")
    p_graph.add_argument("--no-index", action="store_true")
    p_graph.set_defaults(func=cmd_graph)

    # ── Skill Sync ───────────────────────────────────────────────────
    p_export = sub.add_parser("export-skills", help="Export Engram skills as Cursor SKILL.md files")
    p_export.add_argument("--output", default="~/.cursor/skills")
    p_export.add_argument("--project-skills", action="store_true")
    p_export.add_argument("--ids")
    p_export.add_argument("--domain")
    p_export.add_argument("--min-usage", type=int, default=0)
    p_export.add_argument("--from-patterns", action="store_true")
    p_export.add_argument("--dry-run", action="store_true")
    p_export.set_defaults(func=cmd_export_skills)

    p_import_cursor = sub.add_parser("import-cursor-skills", help="Import Cursor skills into Engram")
    p_import_cursor.add_argument("path")
    p_import_cursor.add_argument("--dry-run", action="store_true")
    p_import_cursor.set_defaults(func=cmd_import_cursor_skills)

    p_sync = sub.add_parser("sync-skills", help="Bidirectional sync between Engram and Cursor skills")
    p_sync.add_argument("--path", default="~/.cursor/skills")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.add_argument("--auto", action="store_true")
    p_sync.add_argument("--export-missing", action="store_true")
    p_sync.add_argument("--import-missing", action="store_true")
    p_sync.set_defaults(func=cmd_sync_skills)

    p_import = sub.add_parser("import-skills", help="Import skills from orchestrator SKILL.md files")
    p_import.add_argument("path")
    p_import.set_defaults(func=cmd_import_skills)

    # ── Bootstrap & Maintenance ──────────────────────────────────────
    p_bootstrap = sub.add_parser("bootstrap", help="Bootstrap agent rules for the current project")
    p_bootstrap.add_argument("--mode", choices=["adaptive", "full", "minimal"], default=None)
    p_bootstrap.add_argument(
        "--omit-project-integration",
        action="store_true",
        dest="omit_project_integration",
        help="Skip Cursor rules + .antigravity/instructions.md (also if .omit-agent-integration exists)",
    )
    mcp_group = p_bootstrap.add_mutually_exclusive_group()
    mcp_group.add_argument("--setup-mcp", dest="setup_mcp", action="store_true", default=None)
    mcp_group.add_argument("--no-mcp", dest="setup_mcp", action="store_false")
    p_bootstrap.add_argument(
        "--global-antigravity",
        action="store_true",
        dest="global_antigravity",
        help="Also write/update the Engram snippet in ~/.gemini/AGENTS.md (applies in every Antigravity workspace)",
    )
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_ag = sub.add_parser(
        "antigravity-global",
        help="Install or refresh the global Engram block in ~/.gemini/AGENTS.md (all Antigravity workspaces)",
    )
    p_ag.set_defaults(func=cmd_antigravity_global)

    p_cs = sub.add_parser(
        "claude-skill",
        help="Install or refresh the Engram skill for Claude Code (~/.claude/skills/engram-memory)",
    )
    p_cs.set_defaults(func=cmd_claude_skill)

    p_install = sub.add_parser(
        "install",
        help="One-shot setup: detect Cursor/Claude Code/Antigravity and wire Engram into all of them",
    )
    p_install.add_argument("--all", action="store_true", help="Set up every integration even if not detected")
    p_install.set_defaults(func=cmd_install)

    p_icm = sub.add_parser(
        "import-claude-memories",
        help="Import Claude Code's file-based memories (~/.claude/**/memory/*.md) into Engram",
    )
    p_icm.add_argument("--dir", help="Claude home to scan (default: ~/.claude)")
    p_icm.set_defaults(func=cmd_import_claude_memories)

    p_eff = sub.add_parser("efficiency", help="Action-Ladder efficiency report (reflex runs, reuse, tokens avoided)")
    p_eff.set_defaults(func=cmd_efficiency)

    p_route = sub.add_parser("route", help="Action-ladder lookup: reflex / recall / reason for a task")
    p_route.add_argument("task", nargs="+", help="Task description")
    p_route.set_defaults(func=cmd_route)

    # ── Reflexes (proven skills → executable, approved scripts) ──────
    p_promote = sub.add_parser("promote", help="Draft a reflex script from a proven skill")
    p_promote.add_argument("skill_id", help="Skill id to promote")
    p_promote.set_defaults(func=cmd_promote)

    p_reflex = sub.add_parser("reflex", help="Manage reflexes (list / approve / run)")
    reflex_sub = p_reflex.add_subparsers(dest="action", required=True)
    reflex_sub.add_parser("list", help="List reflexes and their approval/run state")
    p_rapp = reflex_sub.add_parser("approve", help="Approve a drafted reflex (pins its hash)")
    p_rapp.add_argument("id", help="Reflex id")
    p_rrun = reflex_sub.add_parser("run", help="Run an approved reflex")
    p_rrun.add_argument("id", help="Reflex id")
    p_rrun.add_argument("--param", action="append", help="key=value (exported as PARAM_KEY)")
    p_reflex.set_defaults(func=cmd_reflex)

    p_seed = sub.add_parser("seed", help="Seed with historical data")
    p_seed.set_defaults(func=cmd_seed)

    p_init = sub.add_parser("init", help="Initialize the database")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="Run database diagnostics and repair")
    fix_group = p_doctor.add_mutually_exclusive_group()
    fix_group.add_argument("--repair", action="store_true")
    fix_group.add_argument("--fix", action="store_true", dest="repair")
    p_doctor.set_defaults(func=cmd_doctor)

    p_backup = sub.add_parser("backup", help="Export database to JSON format")
    p_backup.add_argument("--git", action="store_true")
    p_backup.set_defaults(func=cmd_backup)

    p_gc = sub.add_parser("gc", help="Garbage collect unused memories")
    p_gc.add_argument("--mode", choices=["dry-run", "archive", "delete"], default="dry-run")
    p_gc.add_argument("--days", type=int, default=180)
    p_gc.set_defaults(func=cmd_gc)

    p_llm = sub.add_parser("llm", help="LLM status, consolidation audit, and assisted GC")
    p_llm.set_defaults(func=cmd_llm)
    llm_sub = p_llm.add_subparsers(dest="llm_command", help="LLM subcommands")

    p_llm_status = llm_sub.add_parser("status", help="Show LLM provider, models, and reachability")
    p_llm_status.set_defaults(llm_command="status")

    p_llm_audit = llm_sub.add_parser("audit", help="Run LLM consolidation audit (dry-run by default)")
    p_llm_audit.add_argument("--threshold", type=float, default=0.80)
    p_llm_audit.add_argument("--execute", action="store_true", help="Apply auto_merge decisions")
    p_llm_audit.add_argument("--force-rescan", action="store_true", help="Ignore consolidation fingerprint")
    p_llm_audit.set_defaults(llm_command="audit")

    p_llm_gc = llm_sub.add_parser("gc", help="Run LLM-assisted GC scoring (dry-run by default)")
    p_llm_gc.add_argument("--days", type=int, default=180)
    p_llm_gc.add_argument("--archive", action="store_true", help="Archive LLM-confirmed discards")
    p_llm_gc.set_defaults(llm_command="gc")

    p_health = sub.add_parser("health", help="Show a health report for the memory database")
    p_health.set_defaults(func=cmd_health)

    p_merge_proj = sub.add_parser(
        "merge-projects",
        help="Merge one project record into another (codebase rows, graph, item links); deletes the source project",
    )
    p_merge_proj.add_argument(
        "--from",
        dest="merge_from",
        required=True,
        metavar="ID|PATH|NAME",
        help="Source project: numeric id, path as stored in DB, or project name",
    )
    p_merge_proj.add_argument(
        "--into",
        dest="merge_into",
        required=True,
        metavar="ID|PATH|NAME",
        help="Target project to keep (id, path, or name)",
    )
    p_merge_proj.add_argument(
        "--execute",
        action="store_true",
        help="Apply changes (default: dry-run)",
    )
    p_merge_proj.set_defaults(func=cmd_merge_projects)

    p_reembed = sub.add_parser("reembed", help="Re-generate embeddings for stale/pending items")
    p_reembed.add_argument("--batch-size", type=int, default=50)
    p_reembed.set_defaults(func=cmd_reembed)

    p_migrate = sub.add_parser("migrate", help="Database migration utilities")
    p_migrate.add_argument("--rollback", action="store_true")
    p_migrate.add_argument("--mark-stale", action="store_true")
    p_migrate.set_defaults(func=cmd_migrate)

    p_migrate_emb = sub.add_parser(
        "migrate-embeddings",
        help="Switch embedding model: mark stale, update schema_meta, reembed",
    )
    p_migrate_emb.add_argument("--target-model", required=True, help="Ollama embedding model name")
    p_migrate_emb.set_defaults(func=cmd_migrate_embeddings)

    p_sleep = sub.add_parser(
        "sleep",
        help="Sleep-time consolidation: merge duplicates, archive stale memories",
    )
    p_sleep.add_argument("--threshold", type=float, default=0.85)
    p_sleep.add_argument("--days", type=int, default=30, help="Archive unused items older than N days")
    p_sleep.add_argument("--dry-run", action="store_true")
    p_sleep.add_argument("--quiet", action="store_true", help="No stdout (for hooks)")
    p_sleep.set_defaults(func=cmd_sleep)

    # ── Session ──────────────────────────────────────────────────────
    p_session = sub.add_parser("get-session", help="Get full details of a session")
    p_session.add_argument("--id", required=True)
    p_session.set_defaults(func=cmd_get_session)

    p_role = sub.add_parser("get-role", help="Get a subagent role profile")
    p_role.add_argument("name")
    p_role.set_defaults(func=cmd_get_role)

    p_srev = sub.add_parser(
        "session-review",
        help="Print the session retrospective checklist (same output as MCP memory_session_review)",
    )
    p_srev.add_argument("--conversation-id", default="cli", help="Label for this retrospective")
    p_srev.add_argument(
        "--project",
        metavar="DIR",
        default=None,
        help="Project directory for duplicate search + affinity (default: current working directory)",
    )
    p_srev.add_argument("--no-project", action="store_true", help="Do not pass a project path")
    p_srev.add_argument("--tasks", default="", help="Tasks completed this session")
    p_srev.add_argument("--bugs-fixed", default="", dest="bugs_fixed")
    p_srev.add_argument("--new-patterns", default="", dest="new_patterns")
    p_srev.add_argument("--workflows-used", default="", dest="workflows_used")
    p_srev.set_defaults(func=cmd_session_review)

    p_iss = sub.add_parser(
        "import-session-summary",
        help="Ingest session_summary.md (or given file) into global memory as a conversation entry",
    )
    p_iss.add_argument(
        "--file",
        "-f",
        default="session_summary.md",
        help="Markdown file to import (default: ./session_summary.md)",
    )
    p_iss.add_argument(
        "--project",
        metavar="DIR",
        default=None,
        help="Associate with this project path (default: current working directory)",
    )
    p_iss.add_argument("--force", action="store_true", help="Insert even if the same content was imported before")
    p_iss.set_defaults(func=cmd_import_session_summary)

    # ── Tools ────────────────────────────────────────────────────────
    p_browse = sub.add_parser("browse", help="Interactive TUI browser for memory entries")
    p_browse.set_defaults(func=cmd_browse)

    p_benchmark = sub.add_parser("benchmark", help="Run LLM benchmark suite")
    p_benchmark.set_defaults(func=cmd_benchmark)

    p_rebench = sub.add_parser(
        "retrieval-benchmark",
        help="Run R@k / MRR / NDCG retrieval quality benchmark (see benchmarks/BENCHMARKS.md)",
    )
    p_rebench.add_argument(
        "bench_args",
        nargs=argparse.REMAINDER,
        help="Pass-through args (e.g. -- --mode compare). Prefix with -- if needed.",
    )
    p_rebench.set_defaults(func=cmd_retrieval_benchmark)

    p_simulate = sub.add_parser("simulate", help="Simulate token usage of Engram vs Traditional")
    p_simulate.add_argument("--mock", action="store_true")
    p_simulate.set_defaults(func=cmd_simulate)

    p_run = sub.add_parser("run", help="Run a prompt using Claw-Code execution engine")
    p_run.add_argument("prompt", nargs="+")
    p_run.add_argument("--role")
    p_run.add_argument("--model")
    p_run.add_argument("--session-id")
    p_run.add_argument("--claw-path")
    p_run.set_defaults(func=cmd_run)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        import sys
        sys.exit(0)

    if args.command not in ("init",):
        init_db()
        if args.command not in ("migrate", "migrate-embeddings", "doctor", "backup"):
            from ..database import verify_embedding_schema_match

            dim_err = verify_embedding_schema_match()
            if dim_err:
                import sys
                print(f"Warning: {dim_err}", file=sys.stderr)

    args.func(args)
