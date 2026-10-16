#!/usr/bin/env bash
# Install a secret into the hub VM's /etc/engram/engram.env — from YOUR machine,
# over SSH, without the secret ever appearing in a shell argument, a log, or a
# chat transcript. The value travels on stdin only.
#
#   deploy/install-key.sh                       # ENGRAM_LLM_API_KEY from ~/Documents/engram-hub-key.txt → host "hub"
#   deploy/install-key.sh --var ENGRAM_EMBED_API_KEY --key-file ~/Documents/gemini.txt
#   deploy/install-key.sh --host hub-ip --keep   # keep the local file afterwards
#   deploy/install-key.sh --dry-run              # show the plan, touch nothing
#
# What it does on the VM (all via `sudo -n`): creates /etc/engram if missing,
# installs deploy/engram.env.example as engram.env if missing (0640 root:engram,
# or root:root until the engram group exists), replaces the VAR= line, and
# restarts engram-serve if it is running. Then deletes the local key file
# unless --keep. Refuses placeholders and, for ENGRAM_LLM_API_KEY, anything that
# is not an sk-ant- key.
set -euo pipefail

HOST="hub"
VAR="ENGRAM_LLM_API_KEY"
KEY_FILE="$HOME/Documents/engram-hub-key.txt"
KEEP=0
DRY=0
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/engram.env.example"
REMOTE_ENV="/etc/engram/engram.env"

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --var) VAR="$2"; shift 2 ;;
    --key-file) KEY_FILE="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$VAR" in
  *[!A-Z0-9_]*|"") echo "refusing: --var must be an env var name (A-Z, 0-9, _)" >&2; exit 2 ;;
esac

[ -r "$KEY_FILE" ] || { echo "key file not found: $KEY_FILE" >&2; exit 2; }
# First non-empty line, surrounding whitespace stripped. Never echoed.
KEY="$(grep -m1 -v '^[[:space:]]*$' "$KEY_FILE" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
[ -n "$KEY" ] || { echo "refusing: $KEY_FILE is empty" >&2; exit 2; }
case "$KEY" in
  PASTE-*|*REPLACE-ME*) echo "refusing: $KEY_FILE still holds the placeholder — paste the key on line 1" >&2; exit 2 ;;
esac
if [ "$VAR" = "ENGRAM_LLM_API_KEY" ]; then
  case "$KEY" in
    sk-ant-*) ;;
    *) echo "refusing: $VAR must be an Anthropic key (sk-ant-…); got something else (not shown)" >&2; exit 2 ;;
  esac
fi
case "$KEY" in
  *[[:space:]]*|*[\'\"]*) echo "refusing: key contains whitespace or quotes (not shown); check the paste" >&2; exit 2 ;;
esac

echo "plan: set $VAR in $HOST:$REMOTE_ENV from $KEY_FILE (${#KEY} chars), then $( [ "$KEEP" = 1 ] && echo keep || echo delete ) the local file"
[ "$DRY" = 1 ] && { echo "dry run — nothing sent"; exit 0; }

# 1. Template if the env file is missing (the template holds no secrets).
# shellcheck disable=SC2016
REMOTE_TEMPLATE='set -euo pipefail
ENV=/etc/engram/engram.env
if [ -f "$ENV" ]; then echo "env: present"; exit 0; fi
grp=root; getent group engram >/dev/null 2>&1 && grp=engram
install -d -o root -g "$grp" -m 0750 /etc/engram
umask 077; tmp=$(mktemp); cat > "$tmp"
install -o root -g "$grp" -m 0640 "$tmp" "$ENV"; rm -f "$tmp"
echo "env: installed template ($grp)"'
ssh -o BatchMode=yes "$HOST" "sudo -n bash -c $(printf '%q' "$REMOTE_TEMPLATE")" < "$TEMPLATE"

# 2. The secret: stdin → remote `read`, never argv. Replace the VAR= line in place.
# shellcheck disable=SC2016
REMOTE_SET='set -euo pipefail
VAR="$1"; ENV=/etc/engram/engram.env
IFS= read -r KEY
[ -n "$KEY" ] || { echo "remote: empty value" >&2; exit 3; }
grp=root; getent group engram >/dev/null 2>&1 && grp=engram
umask 077; tmp=$(mktemp)
{ grep -v "^${VAR}=" "$ENV" || true; printf "%s=%s\n" "$VAR" "$KEY"; } > "$tmp"
install -o root -g "$grp" -m 0640 "$tmp" "$ENV"; rm -f "$tmp"
if systemctl is-active --quiet engram-serve 2>/dev/null; then systemctl restart engram-serve && echo "engram-serve: restarted"; else echo "engram-serve: not running (nothing restarted)"; fi
echo "set: $VAR ($(wc -c < <(printf "%s" "$KEY")) chars) in $ENV"'
printf '%s\n' "$KEY" | ssh -o BatchMode=yes "$HOST" "sudo -n bash -c $(printf '%q' "$REMOTE_SET") bash $(printf '%q' "$VAR")"
unset KEY

# 3. The local copy has done its job.
if [ "$KEEP" = 1 ]; then
  echo "kept $KEY_FILE (--keep)"
else
  if command -v shred >/dev/null 2>&1; then shred -u "$KEY_FILE"; else rm -P "$KEY_FILE" 2>/dev/null || rm -f "$KEY_FILE"; fi
  echo "deleted $KEY_FILE"
fi
echo "done. verify on the VM: sudo -u engram /opt/engram/.venv/bin/engram llm-status"
