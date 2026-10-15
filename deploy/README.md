# Deploying `engram serve` on the hub VM

ADR-0009 Phase 1 shape: one canonical Engram on an Ubuntu VM, reachable only
inside the tailnet. `engram serve` listens on `127.0.0.1:7437`; `tailscale serve`
terminates TLS, injects identity and capability headers, and is the only way in.

## 1. User, directories, code

```bash
sudo adduser --system --group --home /var/lib/engram --shell /usr/sbin/nologin engram
sudo install -d -o engram -g engram -m 0750 /var/lib/engram
sudo install -d -o root -g engram -m 0750 /etc/engram
sudo git clone https://github.com/luiscota99/engram.git /opt/engram
cd /opt/engram && sudo python3 -m venv .venv && sudo .venv/bin/pip install -e '.[anthropic,fastembed]'
sudo chown -R engram:engram /opt/engram
```

## 2. Environment file (holds the keys)

```bash
sudo install -o root -g engram -m 0640 deploy/engram.env.example /etc/engram/engram.env
sudoedit /etc/engram/engram.env   # paste your own values; never commit this file
```

`ENGRAM_LLM_API_KEY` is your Anthropic key (Console → API keys; scope it to one
workspace, give it an expiry you will remember to rotate). It never leaves the
VM: clients talk to `engram serve`, and only the server talks to Anthropic.
Anthropic has no embeddings API, so embeddings use `fastembed` in-process or a
Gemini key — see the comments in the env file.

## 3. Unit

```bash
sudo cp deploy/systemd/engram-serve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now engram-serve
systemctl status engram-serve
curl -s http://127.0.0.1:7437/v1/health   # from the VM itself; 401 is expected with auth=caps
```

The health check returning `401 missing Tailscale-App-Capabilities` is correct:
callers must come through Serve with a grant.

## 4. Publish on the tailnet with identity + capability headers

```bash
sudo tailscale serve --bg --accept-app-caps=engram.dev/cap/engram 7437
tailscale serve status
```

(`--accept-app-caps` needs Tailscale 1.92+; check `tailscale serve --help` for
the exact spelling on your version.) The server is now
`https://<vm-name>.<tailnet>.ts.net/v1/...`. Never use Funnel.

## 5. Grants in the tailnet policy

Authorization keys on the capability, not on the login, so tagged nodes (the
voice agent) work too. Grant values are `{"actions": [...]}` with `read`,
`write`, `add` (add-only), or `*`.

```jsonc
"grants": [
  { // your own devices: full access
    "src": ["autogroup:member"], "dst": ["tag:hub"], "ip": ["tcp:443"],
    "app": { "engram.dev/cap/engram": [ { "actions": ["write"] } ] }
  },
  { // the voice agent node: add-only + read
    "src": ["tag:voice"], "dst": ["tag:hub"], "ip": ["tcp:443"],
    "app": { "engram.dev/cap/engram": [ { "actions": ["read", "add"] } ] }
  }
]
```

## 6. Verify from a client

```bash
curl -s https://<vm>.<tailnet>.ts.net/v1/health
```

A `200` with `schema_version` proves TLS, the grant, and the server. Every call
also lands in `/var/lib/engram/audit.jsonl` as a `remote_call` row.

## Operations

- Logs: `journalctl -u engram-serve -f`
- Backups and chunk-sync export run on the VM as the `engram` user
  (`sudo -u engram /opt/engram/.venv/bin/engram backup`).
- Upgrades: `cd /opt/engram && sudo -u engram git pull && sudo systemctl restart engram-serve`
  (migrations run on the next start; take a backup first).
