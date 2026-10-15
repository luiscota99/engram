# ADR 0009: Remote mode — one canonical Engram instance, thin clients over Tailscale

Date: 2026-10-13
Status: accepted

## Context

Engram is single-machine by construction. ADR-0001 chose one SQLite file and
explicitly deferred "team/org mode needs a future backend abstraction"
(`docs/decisions/0001-storage-backend.md:25`). The Personal AI Hub spec (rev
64, 2026-10-09) asks for one *canonical* brain on an always-on Ubuntu VM
reachable only over Tailscale, used from four kinds of client: Claude Code
hooks + CLI on a Mac, the same on a Windows/WSL desktop, a Python voice agent
(Pipecat) calling `memory_search` / `memory_add`, and per-device nodes that
post shared items and screenshots. Audit of the code on 2026-10-13:

- **No network mode exists.** The MCP server is stdio-only, one JSON-RPC
  object per line (`src/mcp/protocol.py:3`, `:265`), protocol version
  `2024-11-05` (`src/mcp/constants.py:8`), one client per process (module-global
  client state, `src/mcp/protocol.py:34`). Its parent-death watchdog exits when
  the parent pid becomes 1 (`src/mcp/protocol.py:249-257`), so under systemd —
  whose children have ppid 1 from birth — it would die immediately.
- **No storage seam.** `get_db_path()` is `ENGRAM_DB_PATH` else
  `~/.engram/memory.db` (`src/database.py:40`, `:514-519`); `get_connection()`
  always `sqlite3.connect()`s a local file and runs migrations
  (`src/database.py:523-537`, `:597`). 32 modules call `get_connection` /
  `connection_scope` (147 call sites, 328 inline `.execute(` statements). Hooks
  hit SQLite directly: recall through `search()` (`src/hooks.py:76-85`), the
  v28 guard through a `trigger_ngrams` probe (`src/hooks.py:154-158`), the Stop
  checkpoint through `upsert_checkpoint` (`src/checkpoint.py:126-129`).
- **Side files are local per client.** Search/injection audit JSONL
  (`src/config.py:126-134`), `embed_cache.db` next to the DB
  (`src/embeddings.py:124-125`), `~/.engram/session-help.jsonl`
  (`src/config.py:44`).
- **Identity is a local path.** Checkpoints are `UNIQUE(project_path,
  session_id)` (`src/database.py:410-423`) where `project_path` is the local
  `realpath` of cwd (`src/checkpoint.py:29`); `resume` looks rows up by that
  path (`src/checkpoint.py:343-350`). `projects.path` is absolute too. No
  host, machine, or profile concept exists in the schema; chunk sync stamps
  `machine` on export (`src/chunk_sync.py:202-204`) and drops it on import.
- **Hooks read things a server cannot see.** The transcript tail from
  `transcript_path` (`src/checkpoint.py:52`, `:172-178`), git head/branch via
  `subprocess` in cwd (`src/checkpoint.py:45-49`), and echo detection joins
  the local audit log against the local transcript (`src/checkpoint.py:248-300`).
- **Model config is env-based** (`ENGRAM_EMBED_URL/MODEL/DIMENSIONS/API_KEY`,
  `ENGRAM_LLM_BASE_URL/MODEL/API_KEY/...`). ADR-0002's 768 lock has since
  softened to `schema_meta.vec_dimension` (`src/database.py:1409-1421`); the
  dimension is a property of the store, not of the caller.

Two platform facts (vendor docs, verified 2026-10-13) bear on the decisions.
Claude Code hooks now have an `http` handler: it POSTs the event JSON to a URL,
reads a 2xx JSON body as hook output, times out in whole seconds (30 s on
prompts, 600 s on tool calls), carries no project id or hostname, and on Stop
supplies `last_assistant_message` because the transcript "may lag". MCP
2026-07-28 is stateless: no sessions or initialize handshake, and
server-initiated requests such as elicitation become multi-round-trip retries.

Constraints: hooks fire per prompt and per tool call, so the client latency
budget is small (v28 guard ~110 ms end-to-end locally, ~350 ms worst case; the
spec measures ~50 ms RTT from Hermosillo and ~110 ms from Tokyo to a US-West
VM); hook failure must be non-blocking; and four house invariants must
survive: local-first, measured-not-vibes, fail-closed governance (ADR-0007),
single-owner writes (the v23 lesson).

## Decision

### 1. Transport: plain HTTP + JSON over the tailnet

`engram serve` exposes a small JSON-over-HTTP API reachable only inside the
tailnet. Tailnet membership and Tailscale grants are the authentication
boundary; WireGuard is the encryption. One request = one hook call.

Rejected — **MCP Streamable HTTP** as the hook transport: hooks, the voice
agent and the nodes are not MCP clients, and MCP's own `mcp_tool` hook handler
is skipped at session start and has no documented `additionalContext` path.
MCP-over-HTTP for Claude Code's *tool* surface is deferred, not rejected
(2026-07-28's statelessness removed the old session-negotiation cost): in
Phase 1 the local stdio `engram-mcp` keeps running on each client and forwards
the Phase 1 tools through the same HTTP client. The Phase 2 cost is that
ADR-0007's elicitation gate (`src/mcp/protocol.py:65`) is the old
server-to-client request shape and must become a multi-round-trip result.
Rejected — **SSH-tunnelled stdio**: one process per client, a handshake per
hook call that alone exceeds the guard budget, the pid-1 watchdog problem, and
multiplexing hooks over one tunnel needs a daemon anyway.

### 2. The seam: client/server split at the hook + CLI boundary

The seam is *above* the modules, not under them. `engram serve` is a new
entrypoint that hosts the existing modules unchanged against its own local
SQLite. The `engram` client, when `ENGRAM_REMOTE_URL` is set, routes the Phase
1 commands to HTTP instead of opening a DB; when unset, nothing changes.

Phase 1 endpoints (each a thin mapping over an existing function):

| Endpoint | Maps to | Client |
|---|---|---|
| `POST /v1/recall` | `build_recall_context` | `engram hook recall` |
| `POST /v1/guard` | `_probe_guard_warnings` | `engram hook guard` |
| `POST /v1/checkpoint` | `upsert_checkpoint` + echo detection | `engram hook checkpoint` |
| `GET /v1/resume` | `get_checkpoints` | `engram resume`, `memory_resume`, SessionStart hook |
| `POST /v1/search` | `search()` | `engram search`, `memory_search`, voice |
| `POST /v1/add` | `memory_ops.create_*` | `engram add`, `memory_add`, voice, nodes |
| `POST /v1/route` | `memory_route` handler | `engram route`, `memory_route` |
| `POST /v1/extract` | `memory_auto_extract` handler | `memory_auto_extract`, nightly digest |
| `GET /v1/inbox` | inbox listing, read-only | plugin poller, `engram inbox` |
| `GET /v1/health` | `doctor` summary | `engram doctor`, client self-test |

Everything else (`brain`, `gc`, `sync`, `migrate-embeddings`, `self-check`,
reflex approval, inbox *decisions*) is an operator action on the server host
over SSH. MCP tools outside the Phase 1 set return a structured "not available
in remote mode (ADR-0009)" error. The checkpoint endpoint answers with an
empty body: a Stop hook that returns `additionalContext` makes Claude keep
working.

Rejected — **a storage-backend abstraction under all 32 modules**: 147 call
sites and 328 inline statements, no second backend to test against, and the
wrong shape — SQL-over-the-wire puts per-row round-trips on the hook hot path
where one call is the budget. ADR-0001 deferred this for team/org mode; this
ADR keeps it deferred. Remote mode moves the *whole* single-writer store to
one host; it does not make the store pluggable.

Deferred — **Claude Code `http` hooks aimed straight at the server**, no local
client. That removes the interpreter startup that is most of the local guard's
110 ms and is the natural Phase 2 for recall and guard. Not Phase 1: the
payload carries no git remote, hostname or project id (the server would first
need a per-host cwd → project map registered once by the CLI), hook timeouts
are whole seconds (the sub-second budgets in §5 must live in a client), and
`http` is not offered on SessionStart.

### 3. Identity: portable project key + host + profile

A local realpath cannot key work across `/Users/…`, `/home/…` and `/mnt/c/…`.
The client computes `project_key`: the normalized `origin` remote URL (scheme,
credentials and `.git` stripped — `github.com/luiscota99/engram`), else a slug
from client config, else `host:realpath`. `host` is the client's hostname;
`profile` is a namespace from client config (`default` in Phase 1; the voice
agent stamps its own, with the call id as its `session_id`).

Checkpoints gain `project_key`, `host`, `profile`; uniqueness becomes
`(project_key, host, profile, session_id)`; `project_path` stays as the
informational cwd. `resume` queries by `project_key` across hosts. Recall,
guard and resume are **profile-scoped by default** — work never injects into
personal — with cross-profile reads an explicit flag; hard isolation, if a
profile ever needs it, is a second `engram serve` on a second DB path, not a
schema feature. Items added through the server carry a code-set origin stamp —
the same `provenance` mechanism as v27 relation edges, which the
portable-brains plan already proposes extending to the content tables.

Rejected — a per-machine path-alias table (grows forever, brittle); hostname
as the only key (loses cross-machine resume, the point of the hub); re-keying
`projects` / `item_projects` now (a known portable-brains gap; out of scope).

### 4. What the client sends vs what the server computes

**Client-supplied**, because only the client can see it: prompt or action
text, `project_key` (from git), `session_id`, `host`, `profile`, git
head/branch, and the turn summary — `last_assistant_message` from the Stop
payload, falling back to the locally extracted transcript tail
(`extract_transcript_tail`, bounded read) when the field is absent. That
assistant text rides with the checkpoint for echo detection; the server cannot
open `transcript_path`.

**Server-side**, because that is where the store and the models are: search,
ranking, FSRS dynamics, the recall/guard gates, the trigger probe, echo
detection (its own ledger joined against the sent text), the injection ledger,
embeddings, and the LLM layer. `ENGRAM_EMBED_*` and `ENGRAM_LLM_*` exist only
on the server; a client never embeds. The voice agent follows the Pipecat
memory-processor shape (recall before the LLM, capture after the turn, capture
skipping the text recall injected) with a ~1 s search budget inside the spec's
1.5 s voice round trip.

Rejected — client-side caching or ranking of memories (duplicates state,
breaks single-owner writes); server-side access to client git or transcripts
(mounting client filesystems is the fragile version of sending 4 KB of text).

### 5. Offline behaviour: degrade to "no context", and measure it

Phase 1 fails open and quiet. A hook that cannot reach the server within its
budget (guard ≈300 ms, recall ≈800 ms, checkpoint ≈1 s) prints nothing; the
client enforces these budgets itself because Claude Code's per-hook timeout is
whole seconds. A failed checkpoint is dropped — the next turn upserts the same
row, so one lost turn costs nothing. `engram add` / `memory_add` report the
failure to the caller. Strict guard mode maps to the hook's `onFailure:
"block"`, so a reachable server is required there. Anthropic-hosted cloud
sessions cannot reach a tailnet and run only repo-scoped hooks: remote mode
covers local and Remote Control sessions, and a cloud session simply gets no
context. Every failure is counted (see Consequences) so the unreachable rate
is a number before anyone designs around it.

Rejected — **read-only local snapshot** (nightly DB copy via Drive): ships
model config and a second schema version to every client, reintroduces the
per-client SQLite this ADR removes, and serves stale reads nobody has asked
for. Rejected — **write queue**: a second writer-in-waiting with ordering and
dedup semantics, for a failure mode with no measurement yet. Both are
revisitable once the ledger shows an unreachable rate that hurts.

### 6. Security: tailnet-only, grants as authorization, no tokens of our own

`engram serve` binds to `127.0.0.1` only and is published on the tailnet by
`tailscale serve`, which terminates TLS on the node's tailnet name, strips any
spoofed identity headers, and injects `Tailscale-User-Login` /
`Tailscale-User-Name` (users only — not tagged nodes) and
`Tailscale-App-Capabilities` (users *and* tagged nodes, 1.92+). Authorization
keys on the capability grant in the tailnet policy — e.g. `engram:read`,
`engram:write`, `engram:add-only` for nodes — so the voice agent on a tagged
node is authorized without a login; identity headers are attribution for the
ledger. No public port; Tailscale Funnel is forbidden; the VM disk and the
nightly snapshot to Drive are encrypted. No bearer tokens in Phase 1: the
instance is single-user, and a token distributed to every client mitigates no
threat that tailnet membership does not already.

Rejected — binding to the `tailscale0` address directly (no TLS, no identity
or grant headers, a firewall rule to maintain); peer-IP → node lookups via the
LocalAPI (works, but Serve already delivers the same facts as headers).

### 7. Schema, migration, and what stays local

One migration (v29): `checkpoints` gains `project_key`, `host`, `profile` and
the new unique index, backfilled with `project_key = project_path`,
`host = <server hostname>`; content tables gain the code-set origin stamp;
`schema_meta` gains `instance_id` so a client can tell it is talking to a
different brain. Migration happens on the server only.

On each client, in remote mode, **nothing persistent remains** except the
config (`ENGRAM_REMOTE_URL`, `ENGRAM_PROFILE`) and hook stderr logs: no
`memory.db`, no `embed_cache.db` (clients do not embed), no audit JSONL (the
ledger must be in one place for `engram roi` and echo detection to be whole).
`session-help.jsonl` stays local until Phase 2 gives it an endpoint.

## Consequences

- **Local-first holds.** Without `ENGRAM_REMOTE_URL` the client is byte for
  byte the current install; the server is itself a local-first install that
  happens to listen on a socket. The HTTP layer is tested by driving the same
  functions through a test client.
- **Measured, not vibes.** Every remote call is a ledger row with
  `transport=remote`, `rtt_ms` and `outcome ∈ {ok, timeout, unreachable}`;
  `engram roi` reports p50/p95 RTT per endpoint and the unreachable rate. The
  Phase 1 acceptance gate is numeric: guard p95 ≤ 350 ms end-to-end over the
  tailnet, recall p95 no worse than today's local p95, zero hook errors.
- **Fail-closed governance is untouched.** Remote clients gain no execution
  authority; the inbox is readable, but proposals are still inbox items and
  approvals and reflex runs are operator actions on the server host. Nothing
  in this ADR adds a way for a client to approve.
- **Single-owner writes get stronger.** The server is the only process that
  opens `memory.db` for writing; backups, chunk-sync export and self-check run
  there. Clients hold no store.
- **The first daemon.** ADR-0007 was proud that Engram "ships no daemon";
  remote mode is one, under systemd. It is `engram serve`, not `engram-mcp` —
  the stdio server and its watchdog stay as they are and must not be run as a
  service.
- **A new silent failure.** An unreachable VM means no memory, quietly. The
  server's self-check cannot see client failures, so the client's
  `engram doctor` must show last-success time and recent failure count, and
  the unreachable rate in `roi` is the alarm.
- **The spec must change where it disagrees.** Its read-only Drive snapshot
  on clients, its "stdio MCP behind an SSE proxy" option, and its 768-fixed
  vector claim are superseded by §5, the Context, and `schema_meta`.
- **ADR-0001 and ADR-0002 are unchanged.** No backend abstraction; the vector
  dimension lives in the server's `schema_meta` and clients never see
  embeddings. The origin stamp converges with the portable-brains plan rather
  than adding a second provenance scheme. Separately: `engram-mcp` still
  declares `2024-11-05` while Claude Code's v2 MCP runtime negotiates
  2026-07-28 and falls back; that fallback is untested and must be before
  Phase 1 ships.
