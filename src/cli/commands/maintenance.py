"""Maintenance commands: gc, doctor, backup, health, stats, reembed, migrate."""
from __future__ import annotations

from ...backup import run_backup
from ...database import get_embedding_stats, mark_embeddings_stale, reembed_stale
from ...doctor import run_diagnostics
from ...maintenance import run_gc, run_health_check
from ..fmt import fmt_bold, fmt_dim, fmt_header, fmt_type


def cmd_gc(args):
    result = run_gc(mode=args.mode, days_unused=args.days, db_path=None)
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

    from ...database import DB_PATH

    if args.rollback:
        backup_dir = os.path.join(os.path.dirname(DB_PATH), "backups")
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
        shutil.copy2(latest, DB_PATH)
        print(f"✓ Rolled back to {latest}")
        return

    if args.mark_stale:
        count = mark_embeddings_stale()
        print(f"✓ Marked {count} embeddings as stale (model changed).")
        print("  Run `engram reembed` to regenerate.")
        return

    print("Use --rollback to restore from backup, or --mark-stale after changing embedding model.")
