"""Chunk sync commands: sync-export, sync-import, sync-status.

Share memories across machines/teams through a plain directory of
append-only chunks (git-friendly by construction). See src/chunk_sync.py.
"""
from __future__ import annotations

from ...chunk_sync import DEFAULT_SYNC_DIR, export_chunks, import_chunks, sync_status
from ...database import get_connection
from ..fmt import fmt_bold, fmt_dim, fmt_header


def cmd_sync_export(args):
    with get_connection() as conn:
        result = export_chunks(conn, args.dir)
    if result["exported"]:
        print(fmt_header(f"✓ {result['exported']} entradas exportadas → {result['dir']}/chunks/{result['chunk']}"))
        print(fmt_dim("Versiona el directorio (git add/commit/push) para compartirlo; los chunks nunca se mutan."))
    else:
        print(fmt_dim(f"Nada nuevo que exportar — {result['dir']} ya tiene todas las entradas locales."))


def cmd_sync_import(args):
    with get_connection() as conn:
        result = import_chunks(conn, args.dir)
    if result["imported"]:
        by_type = ", ".join(f"{n} {t}" for t, n in sorted(result["by_type"].items()))
        print(fmt_header(f"✓ {result['imported']} entradas importadas ({by_type})"))
        print(fmt_dim("FTS y tags quedaron indexados; los embeddings se generan en el siguiente pase (doctor/reembed)."))
    else:
        print(fmt_dim(f"Nada que importar desde {result['dir']} — la base local ya está al día."))


def cmd_sync_status(args):
    with get_connection() as conn:
        s = sync_status(conn, args.dir)
    print(fmt_bold(f"Sync dir: {s['dir']} ({s['chunks']} chunks)"))
    print(f"  local: {s['local_entries']} entradas · remoto: {s['remote_entries']}")
    print(f"  por exportar: {s['to_export']} · por importar: {s['to_import']}")


def add_parsers(sub):
    """Wire the three sync-* subcommands (called from cli/main.py)."""
    for name, fn, help_ in (
        ("sync-export", cmd_sync_export, "Export new memories as an append-only chunk (git-friendly share)"),
        ("sync-import", cmd_sync_import, "Import memories from a sync dir's chunks (idempotent)"),
        ("sync-status", cmd_sync_status, "Compare local DB vs sync dir without changing anything"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--dir", default=DEFAULT_SYNC_DIR, help=f"Sync directory (default {DEFAULT_SYNC_DIR})")
        p.set_defaults(func=fn)
