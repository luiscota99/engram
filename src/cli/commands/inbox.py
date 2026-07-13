"""Inbox and scheduling commands: alerts, decisions, self-check, cron."""
from __future__ import annotations

import os
import subprocess
import sys

from ...inbox import decide, list_items
from ..fmt import fmt_bold, fmt_dim, fmt_header

_SEV_ICON = {"critical": "🔴", "high": "🟠", "warning": "🟡", "info": "·"}


def cmd_inbox(args):
    items = list_items(status=getattr(args, "status", "open") or "open")
    if not items:
        print(fmt_dim("Inbox vacío — nada requiere tu atención."))
        return
    print(fmt_header(f"Inbox ({len(items)} abiertos)\n"))
    for it in items:
        icon = _SEV_ICON.get(it["severity"], "·")
        kind = "DECISIÓN" if it["kind"] == "decision" else "alerta"
        print(f"  {icon} #{it['id']} [{kind}] {fmt_bold(it['title'])}")
        if it.get("body"):
            first = it["body"].splitlines()[0][:100]
            print(fmt_dim(f"      {first}"))
        if it.get("proposed_reflex_id"):
            print(fmt_dim(f"      → engram decide {it['id']} --approve --run"))
    print(fmt_dim("\nResolver: engram decide <id> --approve [--run] | --reject | --ack"))


def cmd_decide(args):
    decision = "approve" if args.approve else "reject" if args.reject else "acknowledge"
    try:
        result = decide(int(args.id), decision, run=getattr(args, "run", False))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ #{result['id']} → {result['status']}: {result['title']}")
    if result.get("run"):
        out = result["run"]
        print(("✓ " if out["ok"] else "✗ ") + (out.get("output") or out.get("error", ""))[:400])


def cmd_self_check(args):
    _ = args
    from ...maintenance import run_self_check

    r = run_self_check()
    if r["count"]:
        print(f"✓ Self-check: {r['count']} hallazgo(s) nuevos en el inbox — engram inbox")
        for k in r["filed"]:
            print(fmt_dim(f"  + {k}"))
    else:
        print(fmt_dim("✓ Self-check: sin hallazgos nuevos."))


CRON_MARK = "# engram:"


def _crontab_lines() -> list[str]:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        return out.stdout.splitlines() if out.returncode == 0 else []
    except Exception:
        return []


def _write_crontab(lines: list[str]) -> None:
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True, timeout=10)


def _engram_invocation() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    py = os.path.join(root, ".venv", "bin", "python")
    if not os.path.exists(py):
        py = sys.executable
    return f"cd {root} && {py} -m src.cli"


def cmd_schedule(args):
    """engram reflex schedule <id> '<cron expr>' | unschedule <id> | self-check --daily."""
    tag = f"{CRON_MARK}{args.what}"
    lines = [ln for ln in _crontab_lines() if tag not in ln]
    if getattr(args, "remove", False):
        _write_crontab(lines)
        print(f"✓ Desprogramado: {args.what}")
        return
    cron = args.cron
    if args.what == "self-check":
        cmd = f"{_engram_invocation()} self-check"
    else:
        cmd = f"{_engram_invocation()} reflex run {args.what}"
    lines.append(f"{cron} {cmd} >> ~/.engram/cron.log 2>&1 {tag}")
    _write_crontab(lines)
    print(f"✓ Programado ({cron}): {cmd}")
    print(fmt_dim("  Ver: crontab -l | quitar: engram schedule <what> --remove"))
    # A cron job under a macOS-protected folder crashes silently — warn now
    # rather than let the monitor die unnoticed.
    from ...maintenance import install_path_tcc_warning

    warn = install_path_tcc_warning()
    if warn:
        print(fmt_header("\n" + warn))


NOTIFY_SCRIPT = '''set -euo pipefail
# Notificación local macOS. Cambia este script (y re-aprueba) para
# enviar a Telegram/Slack/WhatsApp via curl si prefieres el teléfono.
TITLE="${PARAM_TITLE:-Engram}"
BODY="${PARAM_BODY:-}"
osascript -e "display notification \\"$BODY\\" with title \\"Engram: $TITLE\\" sound name \\"Glass\\""
'''


def cmd_notify_init(args):
    """Create the notify reflex (draft) with a macOS-notification default script."""
    _ = args
    from ...database import get_connection
    from ...memory_ops import create_skill
    from ...reflex import promote_skill

    with get_connection() as c:
        exists = c.execute("SELECT id FROM reflexes WHERE name = 'notify'").fetchone()
    if exists:
        print(f"El reflex 'notify' ya existe (#{exists['id']}).")
        return
    with get_connection() as c:
        sid = create_skill(
            c, name="Notify", domain="system",
            trigger="entregar una notificación del inbox al usuario",
            workflow="osascript display notification (o webhook si lo editas)",
            tags="engram,notify",
        )
    r = promote_skill(sid)
    with get_connection() as c:
        c.execute("UPDATE reflexes SET script = ? WHERE id = ?", (NOTIFY_SCRIPT, r["id"]))
    print(f"✓ Reflex 'notify' creado como borrador (#{r['id']}) con osascript.")
    print("  Revísalo y actívalo: engram reflex approve " + str(r["id"]))
