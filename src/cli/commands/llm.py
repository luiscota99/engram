"""LLM commands: status, audit, gc."""
from __future__ import annotations

from ...llm import get_llm_status, is_llm_available
from ...maintenance import run_llm_consolidation_audit, run_llm_gc
from ..fmt import fmt_bold, fmt_dim, fmt_header, fmt_type


def cmd_llm_status(_args) -> None:
    status = get_llm_status()
    print(fmt_header("Engram LLM Status\n"))
    print(f"  {fmt_bold('Base URL:')}     {status['base_url']}")
    print(f"  {fmt_bold('Model:')}        {status['model']}")
    print(f"  {fmt_bold('Audit model:')}  {status['audit_model']}")
    print(f"  {fmt_bold('Extract model:')} {status['extract_model']}")
    print(f"  {fmt_bold('API key:')}      {'set' if status['api_key_set'] else 'not set'}")
    reachable = is_llm_available()
    if reachable:
        print(f"\n{fmt_bold('✓')} LLM backend is reachable.")
        if status["tasks_enabled"]:
            print(fmt_dim("  Enabled tasks: " + ", ".join(status["tasks_enabled"])))
    else:
        print(f"\n{fmt_bold('✗')} LLM backend is not reachable.")
        print(
            fmt_dim(
                "  Consolidation audit, GC scoring, auto-extract, and merge use regex/fallback paths."
            )
        )


def cmd_llm_audit(args) -> None:
    dry_run = not args.execute
    report = run_llm_consolidation_audit(
        threshold=args.threshold,
        dry_run=dry_run,
        force_rescan=args.force_rescan,
    )

    print(fmt_header("LLM Consolidation Audit\n"))
    if report.get("skip_reason") == "unchanged":
        print(fmt_dim("No changes since last consolidation scan (fingerprint unchanged)."))
        print(fmt_dim("Use --force-rescan to rescan."))
        return

    print(f"  Clusters found: {report['clusters_found']}")
    print(f"  LLM available:  {report['llm_available']}")

    if report.get("fallback"):
        print(fmt_dim(f"\n  {report['fallback']}"))
        for cluster in report.get("clusters", [])[:10]:
            print(
                f"\n  {fmt_type(cluster['item_type'])} cluster "
                f"(sim: {cluster['avg_similarity']}, size: {cluster['cluster_size']})"
            )
            for item in cluster["items"]:
                print(f"    ID:{item['item_id']}  {item['title']}")
        return

    decisions = report.get("decisions") or []
    if not decisions:
        print(fmt_dim("\nNo LLM decisions returned."))
        return

    print(f"\n  Decisions ({len(decisions)}):\n")
    for d in decisions:
        ids = ", ".join(str(i) for i in d.get("ids", []))
        print(
            f"  [{d.get('decision', '?').upper()}] "
            f"{fmt_type(d.get('item_type', ''))} IDs: {ids}"
        )
        if d.get("reason"):
            print(fmt_dim(f"    {d['reason']}"))

    if report.get("blocked"):
        print(fmt_header("\nAudit blocked"))
        print(report.get("reason", ""))
        return

    if dry_run:
        print(fmt_dim("\nDry-run complete. Re-run with --execute to apply auto_merge decisions."))
    else:
        applied = report.get("applied") or []
        ok = sum(1 for a in applied if a.get("applied"))
        print(f"\n{fmt_bold('✓')} Applied {ok} auto_merge operation(s).")
        for a in applied:
            if a.get("applied"):
                print(
                    f"  Merged {a['item_type']} → new ID {a['merged_id']} "
                    f"(archived {a['archived_ids']})"
                )
            elif a.get("reason"):
                print(fmt_dim(f"  Skipped: {a.get('reason')}"))


def cmd_llm_gc(args) -> None:
    dry_run = not args.archive
    report = run_llm_gc(
        dry_run=dry_run,
        days_unused=args.days,
    )

    print(fmt_header("LLM-Assisted GC\n"))
    print(f"  Candidates:   {len(report.get('candidates', []))}")
    print(f"  LLM available:  {report['llm_available']}")

    if report.get("fallback"):
        print(fmt_dim(f"\n  {report['fallback']}"))

    to_discard = report.get("to_discard") or []
    scored = report.get("scored") or []

    if scored:
        print(f"\n  LLM scored {len(scored)} item(s), {len(to_discard)} marked discard:\n")
        for s in scored[:30]:
            mark = "DISCARD" if s.get("decision") == "discard" else "keep"
            print(
                f"  [{mark}] {fmt_type(s.get('item_type', ''))} ID:{s.get('item_id')} "
                f"— {s.get('reason', '')[:80]}"
            )
    elif to_discard:
        print(f"\n  {len(to_discard)} candidate(s) for archive:\n")
        for c in to_discard[:30]:
            print(f"  {fmt_type(c['item_type'])} ID:{c['item_id']}")

    if report.get("blocked"):
        print(fmt_header("\nGC blocked"))
        print(report.get("reason", ""))
        return

    if dry_run:
        print(fmt_dim("\nDry-run complete. Re-run with --archive to archive LLM-confirmed discards."))
    else:
        print(f"\n{fmt_bold('✓')} Archived {report.get('processed', 0)} item(s).")


def cmd_llm(args) -> None:
    """Dispatch engram llm subcommands."""
    if args.llm_command == "status":
        cmd_llm_status(args)
    elif args.llm_command == "audit":
        cmd_llm_audit(args)
    elif args.llm_command == "gc":
        cmd_llm_gc(args)
    else:
        print("Usage: engram llm {status|audit|gc}")
