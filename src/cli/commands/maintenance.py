"""Maintenance commands: gc, doctor, backup, health, stats, reembed, migrate."""
from __future__ import annotations

from ...backup import run_backup
from ...database import (
    get_embedding_stats,
    mark_embeddings_stale,
    migrate_embeddings_to_model,
    reembed_stale,
)
from ...doctor import run_diagnostics
from ...maintenance import merge_projects, run_gc, run_health_check, run_sleep
from ..fmt import fmt_bold, fmt_dim, fmt_header, fmt_type


def cmd_gc(args):
    result = run_gc(mode=args.mode, days_unused=args.days, db_path=None)
    if result.get("blocked"):
        print(fmt_header("GC blocked by safety guardrail"))
        print(result.get("reason", ""))
        return
    candidates = result["candidates"]
    if not candidates:
        print(fmt_header("No GC candidates found."))
        print(fmt_dim(f"  (threshold: never used and older than {args.days} days, or last used more than {args.days} days ago)"))
        return
    print(fmt_header(f"GC Candidates ({len(candidates)}):\n"))
    for c in candidates:
        print(f"  {fmt_type(c['item_type'])} ID:{c['item_id']}  created:{c['created_at'] or 'unknown'}")
    if args.mode == "dry-run":
        print(fmt_dim("\nDry-run complete. Run with --archive or --delete to act."))
    else:
        print(f"\n{fmt_bold('✓')} {args.mode.capitalize()}d {result['processed']} of {len(candidates)} items.")


def cmd_doctor(args):
    run_diagnostics(repair=args.repair)


def cmd_backup(args):
    run_backup(git_sync=args.git)


def cmd_health(args):
    report = run_health_check()
    print(fmt_header("Engram Health Report\n"))
    print(fmt_bold("Memory Items:"))
    for itype, stats in report["items"].items():
        total = stats["total"]
        if total == 0:
            continue
        gc = stats["unused_180_plus_days"]
        print(f"  {fmt_type(itype):30s} total:{total:4d}  new(30d):{stats['added_last_30_days']:3d}  gc-candidates:{gc:3d}")
    print()

    emb = report["embeddings"]
    total_emb = emb.get("total", 0)
    print(fmt_bold(f"Embeddings (model: {emb.get('model', 'unknown')}):"))
    if total_emb > 0:
        def pct(n): return f"{100*n/total_emb:.1f}%"
        print(f"  Ready:   {emb['ready']:4d} ({pct(emb['ready'])})")
        if emb["stale"] > 0:
            print(f"  Stale:   {emb['stale']:4d} ({pct(emb['stale'])})  ← regeneration needed")
        if emb["pending"] > 0:
            print(f"  Pending: {emb['pending']:4d} ({pct(emb['pending'])})")
        if emb["failed"] > 0:
            print(f"  Failed:  {emb['failed']:4d} ({pct(emb['failed'])})")
    else:
        print(fmt_dim("  No embeddings tracked yet."))
    print()

    print(fmt_bold("Index Health:"))
    print(f"  FTS entries:        {report['fts_total']}")
    print(f"  Vector entries:     {report['vec_total']}")
    drift = report["vec_drift"]
    if drift > 0:
        print(f"  {fmt_dim(f'⚠ Vector drift:     {drift} FTS entries missing vectors')}")
    else:
        print(fmt_dim("  ✓ No vector drift"))
    print(f"  Orphaned tags:      {report['orphaned_tags']}")
    print(f"  GC candidates:      {report['gc_candidates']}")
    print(f"  Archived memories:  {report['archived_memories']}")
    print()

    if report["recommendations"]:
        print(fmt_bold("Recommendations:"))
        for rec in report["recommendations"]:
            print(f"  • {rec}")
    else:
        print(fmt_dim("✓ No issues detected."))


def cmd_merge_projects(args):
    from ...database import get_connection

    def resolve(conn, spec: str) -> int:
        spec = (spec or "").strip()
        if not spec:
            raise SystemExit("Project id, path, or name is required.")
        if spec.isdigit():
            row = conn.execute("SELECT id FROM projects WHERE id = ?", (int(spec),)).fetchone()
            if not row:
                raise SystemExit(f"No project with id {spec}.")
            return int(row["id"])
        row = conn.execute(
            "SELECT id FROM projects WHERE path = ? OR name = ?",
            (spec, spec),
        ).fetchone()
        if row:
            return int(row["id"])
        raise SystemExit(f"No project with path or name matching {spec!r}.")

    with get_connection() as conn:
        src = resolve(conn, args.merge_from)
        dst = resolve(conn, args.merge_into)

    dry = not args.execute
    result = merge_projects(src, dst, dry_run=dry, db_path=None)

    print(fmt_header("Merge projects (Engram DB)\n"))
    print(
        f"  {fmt_bold('From:')} id={result['from_project_id']}  {result['from_name']!r}  {result['from_path']!r}"
    )
    print(
        f"  {fmt_bold('Into:')} id={result['to_project_id']}  {result['to_name']!r}  {result['to_path']!r}"
    )
    print()
    print(fmt_dim("Codebase knowledge:"))
    print(f"  Overlap rows dropped (source): {result['codebase_overlap_removed']}")
    print(f"  Rows reassigned to target:    {result['codebase_reassigned']}")
    print(fmt_dim("File relationships:"))
    print(f"  Overlap rows dropped:         {result['relationships_overlap_removed']}")
    print(f"  Rows reassigned:              {result['relationships_reassigned']}")
    print(fmt_dim("Item ↔ project links:"))
    print(f"  Overlap rows dropped:         {result['item_projects_overlap_removed']}")
    print(f"  Rows reassigned:              {result['item_projects_reassigned']}")
    print()
    if dry:
        print(fmt_dim("Dry-run only. Re-run with --execute to apply."))
    else:
        print(f"{fmt_bold('✓')} Source project removed; all scoped rows now use the target project.")


def cmd_reembed(args):
    stats_before = get_embedding_stats()
    stale = stats_before.get("stale", 0)
    pending = stats_before.get("pending", 0)
    total_pending = stale + pending

    if total_pending == 0:
        print(fmt_dim("✓ All embeddings are up to date."))
        return

    print(fmt_header(f"Re-embedding {total_pending} items (stale: {stale}, pending: {pending})...\n"))
    batch = args.batch_size or 50
    total_done = 0
    while True:
        result = reembed_stale(batch_size=batch)
        total_done += result["succeeded"]
        if result["failed"]:
            print(fmt_dim(f"  ⚠ {result['failed']} failed this batch"))
        remaining = result["remaining"]
        print(f"  ✓ {total_done} done, {remaining} remaining...")
        if remaining == 0 or result["processed"] == 0:
            break
    print(f"\n{fmt_bold('✓')} Re-embedding complete. {total_done} items updated.")


def cmd_migrate(args):
    import os
    import shutil

    from ...database import get_db_path

    db_path = get_db_path()

    if args.rollback:
        backup_dir = os.path.join(os.path.dirname(db_path), "backups")
        if not os.path.exists(backup_dir):
            print("No backups found.")
            return
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith("pre-migration")],
            reverse=True,
        )
        if not backups:
            print("No migration backups found.")
            return
        latest = os.path.join(backup_dir, backups[0])
        shutil.copy2(latest, db_path)
        print(f"✓ Rolled back to {latest}")
        return

    if args.mark_stale:
        count = mark_embeddings_stale()
        print(f"✓ Marked {count} embeddings as stale (model changed).")
        print("  Run `engram reembed` to regenerate.")
        return

    print("Use --rollback to restore from backup, or --mark-stale after changing embedding model.")


def cmd_migrate_embeddings(args):
    result = migrate_embeddings_to_model(args.target_model)
    if not result.get("ok"):
        print(f"Error: {result.get('error', 'migration failed')}")
        import sys
        sys.exit(1)
    print(f"✓ Migrated embeddings to {result['target_model']}")
    print(f"  Marked stale: {result.get('marked_stale', 0)}")
    reembed = result.get("reembed") or {}
    print(f"  Re-embedded: {reembed.get('succeeded', 0)} succeeded, {reembed.get('failed', 0)} failed")


def cmd_sleep(args):
    summary = run_sleep(
        threshold=args.threshold,
        days_unused=args.days,
        dry_run=args.dry_run,
    )
    if args.quiet:
        return
    print(fmt_header("Engram Sleep — consolidation report\n"))
    print(f"  Clusters found:     {summary.get('clusters_found', 0)}")
    print(f"  Items invalidated:  {summary.get('items_invalidated', 0)}")
    print(f"  Items archived:     {summary.get('items_archived', 0)}")
    if summary.get("dry_run"):
        print(fmt_dim(f"  GC candidates:      {summary.get('gc_candidates', 0)} (dry-run)"))
    print()
