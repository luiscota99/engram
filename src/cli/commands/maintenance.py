"""Maintenance commands: gc, doctor, backup, health, stats, reembed, migrate."""
from __future__ import annotations

import os

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


def cmd_restore(args):
    """Restore the memory DB from a backup: engram restore <file> [--yes].

    Validates the backup, snapshots the current DB first (a restore is itself
    reversible), then replaces via the SQLite backup API. Destructive to the
    current state — confirms interactively unless --yes.
    """
    import sys

    from ...backup import restore_database
    from ...database import get_db_path

    target = get_db_path()
    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            print("Refusing to restore without confirmation (pass --yes in non-interactive use).")
            sys.exit(1)
        answer = input(
            f"Replace {target} with the contents of {args.file}?\n"
            f"(The current DB is snapshotted first.) [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted — nothing changed.")
            return
    try:
        result = restore_database(args.file)
    except ValueError as e:
        print(f"Restore refused: {e}")
        sys.exit(1)
    print(f"✓ Restored {target} from {result['restored_from']}")
    print(f"  Backup schema version: v{result['backup_schema_version']} (migrations run forward on next use)")
    if result["pre_restore_snapshot"]:
        print(f"  Previous state saved to: {result['pre_restore_snapshot']}")


def cmd_efficiency(args):
    """Action-Ladder efficiency report: measured, no invented numbers."""
    _ = args
    from ...maintenance import get_efficiency_report

    r = get_efficiency_report()
    print(fmt_header("Action Ladder — efficiency report\n"))
    print(fmt_bold("Reflex rung (deterministic, ~50 tokens/call):"))
    print(f"  Approved reflexes:   {r['reflexes_approved']} (of {r['reflexes_total']} drafted)")
    print(f"  Total reflex runs:   {r['reflex_runs']}")
    print(f"  Auto-demotions:      {r['auto_demotions']}")
    success = r.get("reflex_success") or {}
    if success:
        total_runs = sum(v["runs"] for v in success.values())
        total_ok = sum(v["ok"] for v in success.values())
        if total_runs:
            print(f"  Success rate:        {total_ok}/{total_runs} ({total_ok/total_runs:.0%})")
    if r["tokens_avoided_floor"]:
        print(f"  Tokens avoided:      >= {r['tokens_avoided_floor']:,} (floor: workflow text not re-read; reasoning savings not claimed)")
    print()
    print(fmt_bold("Recall rung (capture -> reuse):"))
    for itype, st in r["reuse"].items():
        if st["eligible"]:
            rate = f"{st['rate']:.0%}" if st["rate"] is not None else "n/a"
            print(f"  {itype:<14} {st['reused']}/{st['eligible']} reused ({rate})")
    print()
    if r["promotion_candidates"]:
        print(fmt_bold("Ready to move down the ladder:"))
        for cand in r["promotion_candidates"][:5]:
            print(f"  engram promote {cand['id']}   # '{cand['name']}' used {cand['usage_count']}x")
    else:
        print(fmt_dim("No promotion candidates yet - skills earn reflex-hood at 5+ uses."))


def cmd_audit(args):
    """Turn search auditing on/off (persistent) or show its status.

    Auditing is what makes `engram roi` answer 'how much did Engram help?'.
    """
    from ... import config

    action = getattr(args, "action", "status")
    if action == "on":
        config.set_persistent("audit_enabled", True)
        print(fmt_header("Search auditing enabled."))
        print(f"  Logging searches to {fmt_dim(config.audit_log_path() or '')}")
        print(fmt_dim("  Stored locally only (query + top-5 result ids). Turn off: engram audit off"))
    elif action == "off":
        config.set_persistent("audit_enabled", False)
        print(fmt_header("Search auditing disabled."))
        print(fmt_dim("  Existing log left in place; delete it manually if you want it gone."))
    else:
        path = config.audit_log_path()
        env_forced = bool(os.environ.get("ENGRAM_AUDIT_LOG"))
        print(fmt_header("Search auditing status"))
        print(f"  Enabled: {fmt_bold('yes' if path else 'no')}")
        if path:
            src = "ENGRAM_AUDIT_LOG env" if env_forced else "engram audit on"
            print(f"  Source:  {fmt_dim(src)}")
            print(f"  Log:     {fmt_dim(path)}")
            from ...search_audit import summarize_audit_log

            s = summarize_audit_log(path)
            print(f"  Recorded searches: {s['searches']}")
        else:
            print(fmt_dim("  Enable with: engram audit on"))


def cmd_roi(args):
    """How much has Engram actually helped? Measured from local telemetry."""
    _ = args
    from ...maintenance import get_roi_report

    r = get_roi_report()
    a = r["audit"]
    print(fmt_header("Engram ROI — measured help\n"))

    print(fmt_bold("Search activity (audit log):"))
    if not a["enabled"]:
        print(fmt_dim("  Auditing is OFF — enable with `engram audit on` to measure this."))
    else:
        print(f"  Searches served:   {a['searches']}")
        if a["searches"]:
            print(f"  Returned a hit:    {a['with_hit']}/{a['searches']} ({int((a['hit_rate'] or 0) * 100)}%)")
            print(f"  Zero-result:       {a['zero_result']}")
            if a["by_source"]:
                by = ", ".join(f"{k}:{v}" for k, v in sorted(a["by_source"].items()))
                print(f"  By source:         {by}")
            if a["top_queries"]:
                print(fmt_dim("  Top queries: " + "; ".join(f"{q} ({n})" for q, n in a["top_queries"][:5])))
    print()

    inj = a.get("injection", {}) if a["enabled"] else {}
    if any(b.get("evals") for b in inj.values()):
        print(fmt_bold("Injection overhead (post-gate — what Engram actually adds):"))
        for kind in ("recall", "guard"):
            b = inj.get(kind, {})
            if not b.get("evals"):
                continue
            rate = int(100 * b["injected"] / b["evals"])
            avg = (b["tokens_est_total"] // b["injected"]) if b["injected"] else 0
            print(
                f"  {kind:7s} {b['injected']}/{b['evals']} fired ({rate}%), "
                f"~{b['tokens_est_total']} tokens total (~{avg}/injection)"
            )
        print(fmt_dim("  Suppressions are the relevance gate declining to inject noise."))
        print()

    lat = a.get("latency", {}) if a["enabled"] else {}
    if lat.get("samples"):
        em = lat.get("embed_ms", {})
        vm = lat.get("vec_search_ms", {})
        print(fmt_bold("Semantic latency (where the time goes):"))
        print(
            f"  embedding:      p50 {em.get('p50')}ms  p95 {em.get('p95')}ms  "
            f"max {em.get('max')}ms"
        )
        print(
            f"  vector KNN:     p50 {vm.get('p50')}ms  p95 {vm.get('p95')}ms  "
            f"max {vm.get('max')}ms   (over {lat['samples']} samples)"
        )
        p50_em = em.get("p50") or 0
        p50_vm = vm.get("p50") or 0
        if p50_vm and p50_em:
            ratio = p50_em / p50_vm
            if ratio >= 5:
                print(fmt_dim(
                    f"  → embedding dominates ({ratio:.0f}× the KNN); the vector index is not the "
                    "bottleneck. Latency work belongs on the embedder, not the DB."
                ))
            else:
                print(fmt_dim(
                    "  → vector KNN is now a material share of latency; if it keeps growing, "
                    "an ANN index (or store pruning) is the lever — revisit the vector-DB choice."
                ))
        print()

    print(fmt_bold("Realized reuse:"))
    print(f"  Memories ever used: {r['items_used']}/{r['items_total']}")
    fb = r.get("feedback_by_source") or {}
    if fb:
        parts = [
            f"{src}: +{st['helped']}/-{st['unhelpful']}" for src, st in sorted(fb.items())
        ]
        print(f"  Feedback (helped/unhelpful by source): {', '.join(parts)}")
        if "echo" in fb:
            print(fmt_dim("    echo = agent output cited an injected memory (automatic, weak-positive)"))
    for itype, st in r["used_by_type"].items():
        if st["total"]:
            print(f"    {itype:<14} {st['used']}/{st['total']}")
    print()

    print(fmt_bold("Reflex rung:"))
    print(f"  Approved reflexes:  {r['reflexes_approved']}")
    print(f"  Runs:               {r['reflex_runs']}")
    if r["tokens_avoided_floor"]:
        print(f"  Tokens avoided:     >= {r['tokens_avoided_floor']:,} (floor)")
    print()

    print(fmt_bold("Verdict:"))
    print(f"  {r['verdict']}")


def cmd_health(args):
    report = run_health_check()
    print(fmt_header("Engram Health Report\n"))
    print(fmt_bold("Memory Items:"))
    for itype, stats in report["items"].items():
        total = stats["total"]
        if total == 0:
            continue
        gc = stats["unused_180_plus_days"]
        rr = stats.get("reuse_rate_30d_plus")
        rr_str = f"  reuse:{rr:.0%}" if rr is not None else ""
        print(f"  {fmt_type(itype):30s} total:{total:4d}  new(30d):{stats['added_last_30_days']:3d}  gc-candidates:{gc:3d}{rr_str}")
    print()

    cr = report.get("capture_reuse", {})
    if cr.get("eligible_30d_plus"):
        rate = cr.get("reuse_rate")
        rate_str = f"{rate:.0%}" if rate is not None else "n/a"
        print(fmt_bold("Capture → Reuse:"))
        print(
            f"  {cr['reused']}/{cr['eligible_30d_plus']} memories captured 30+ days ago "
            f"were later used ({rate_str})"
        )
        print(fmt_dim("  Reuse is the capture-quality signal: high = you're saving the right things."))
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
