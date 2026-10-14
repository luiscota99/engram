# ADR 0009: Remote mode — one canonical Engram instance, thin clients over Tailscale

Date: 2026-10-13
Status: proposed

## Context

Engram is single-machine by construction. ADR-0001 chose one SQLite file and
explicitly deferred "team/org mode needs a future backend abstraction"
(`docs/decisions/0001-storage-backend.md:25`). The Personal AI Hub spec now
asks for one *canonical* brain on an always-on Ubuntu VM reachable only over
Tailscale, used from three kinds of client: Claude Code hooks + CLI on a Mac,
the same on a Windows/WSL desktop, and a Python voice agent (Pipecat) that
calls `memory_search` / `memory_add`. Audit of the code on 2026-10-13:

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

Constraints that shape the design: hooks fire per prompt and per tool call,
so the client latency budget is small (the v28 guard fast path is ~110 ms
end-to-end locally, ~350 ms worst case); hook failure must be non-blocking
(VM unreachable → the hook prints nothing, never errors); and four house
invariants must survive: local-first, measured-not-vibes, fail-closed
governance (ADR-0007), single-owner writes (the v23 lesson).

## Decision

### 1. Transport: plain HTTP + JSON over the tailnet

`engram serve` exposes a small JSON-over-HTTP API bound to the VM's Tailscale
address. Tailnet membership and Tailscale ACLs are the authentication
boundary; WireGuard is the encryption. One request = one hook call.

Rejected — **MCP Streamable HTTP / SSE** as the hook transport: hooks and the
voice agent are not MCP clients; MCP framing adds session negotiation and a
second protocol version (ours is `2024-11-05`) on a three-endpoint hot path.
Exposing MCP-over-HTTP for Claude Code's *tool* surface is a separate
concern, deferred: in Phase 1 the local stdio `engram-mcp` process keeps
running on each client and forwards the Phase 1 tools through the same HTTP
client. Rejected — **SSH-tunnelled stdio**: one process per client, a
handshake per hook call that alone exceeds the guard budget, and the pid-1
watchdog problem; multiplexing hooks over one tunnel needs a daemon anyway.

### 2. The seam: client/server split at the hook + CLI boundary

The seam is *above* the modules, not under them. `engram serve` is a new
entrypoint that hosts the existing modules unchanged against its own local
SQLite. The `engram` client, when `ENGRAM_REMOTE_URL` is set, routes the
Phase 1 commands to HTTP instead of opening a DB; when unset, nothing changes.

Phase 1 endpoints (each a thin mapping over an existing function):

| Endpoint | Maps to | Client |
|---|---|---|
| `POST /v1/recall` | `build_recall_context` | `engram hook recall` |
| `POST /v1/guard` | `_probe_guard_warnings` | `engram hook guard` |
| `POST /v1/checkpoint` | `upsert_checkpoint` + echo detection | `engram hook checkpoint` |
| `GET /v1/resume` | `get_checkpoints` | `engram resume`, `memory_resume` |
| `POST /v1/search` | `search()` | `engram search`, `memory_search`, voice |
| `POST /v1/add` | `memory_ops.create_*` | `engram add`, `memory_add`, voice |
| `GET /v1/health` | `doctor` summary | `engram doctor`, client self-test |

Everything else (`brain`, `gc`, `sync`, `migrate-embeddings`, `self-check`,
reflex approval, inbox decisions) is an operator action run on the server
host over SSH. MCP tools outside the Phase 1 set return a structured
"not available in remote mode (ADR-0009)" error rather than touching a DB
that does not exist on the client.

Rejected — **a storage-backend abstraction under all 32 modules**: 147 call
sites and 328 inline statements, no second backend to test against, and the
wrong shape — SQL-over-the-wire puts per-row round-trips on the hook hot path
where one call is the budget. ADR-0001 deferred this for team/org mode; this
ADR keeps it deferred. Remote mode moves the *whole* single-writer store to
one host; it does not make the store pluggable.

### 3. Identity: portable project key + host + profile

A local realpath cannot key work across `/Users/…`, `/home/…` and `/mnt/c/…`.
The client computes `project_key`: the normalized `origin` remote URL
(scheme, credentials and `.git` stripped — `github.com/luiscota99/engram`),
else a slug from client config, else `host:realpath`. `host` is the client's
hostname; `profile` is a namespace from client config (`default` in Phase 1;
the voice agent will stamp its own).

Checkpoints gain `project_key`, `host`, `profile`; uniqueness becomes
`(project_key, host, profile, session_id)`; `project_path` stays as the
informational cwd. `resume` queries by `project_key` (any host by default).
Items added through the server carry a code-set origin stamp — the same
`provenance` mechanism as v27 relation edges, which the portable-brains plan
already proposes extending to the content tables. One mechanism, not two.

Rejected — a path-alias table per machine (grows forever, brittle); hostname
as the only key (loses cross-machine resume, the point of the hub);
re-keying `projects` / `item_projects` in Phase 1 (affinity by key is a
known gap in the portable-brains plan; out of scope here).

### 4. What the client sends vs what the server computes

**Client-supplied**, because only the client can see it: prompt or action
text, `project_key` (from git), `session_id`, `host`, `profile`, the transcript
tail already extracted locally (bounded read, `extract_transcript_tail`), and
git head/branch. For echo detection the client sends the assistant tail text
with the checkpoint; the server cannot open `transcript_path`.

**Server-side**, because that is where the store and the models are: search,
ranking, FSRS dynamics, the recall/guard gates, the trigger probe, echo
detection (its own ledger joined against the sent tail), the injection
ledger, embeddings, and the LLM layer. `ENGRAM_EMBED_*` and `ENGRAM_LLM_*`
exist only on the server; a client never embeds.

Rejected — client-side caching or ranking of memories (duplicates state,
breaks single-owner writes); server-side access to client git or transcripts
(mounting client filesystems over the network is the fragile version of
sending 4 KB of text).

### 5. Offline behaviour: degrade to "no context", and measure it

Phase 1 fails open and quiet. A hook that cannot reach the server within its
budget (guard ≈300 ms, recall ≈800 ms, checkpoint ≈1 s) prints nothing. A
failed checkpoint is dropped — the next turn upserts the same row, so one
lost turn costs nothing. `engram add` / `memory_add` report the failure to
the caller; the user or agent retries. Every failure is counted (see
Consequences) so the unreachable rate is a number before anyone designs
around it.

Rejected — **read-only local snapshot** (nightly DB copy via Drive): ships
model config and a second schema version to every client, reintroduces the
per-client SQLite this ADR removes, and serves stale reads nobody has asked
for. Rejected — **write queue**: a second writer-in-waiting with ordering and
dedup semantics, for a failure mode with no measurement yet. Both are
revisitable once the ledger shows an unreachable rate that hurts.

### 6. Security: tailnet-only, no tokens of our own

Bind only to the Tailscale interface address (never `0.0.0.0`); the VM
firewall admits port traffic on `tailscale0` only; no public port; Tailscale
Funnel is forbidden. Tailscale ACLs restrict which nodes may reach the port.
The server logs peer IP → node name per request so the ledger can attribute
calls by client. No bearer tokens in Phase 1: the instance is single-user,
and a token distributed to three clients and a voice agent mitigates no
threat that tailnet membership does not already. A non-tailnet client, if
one ever appears, is a new ADR, not a flag. TLS via Tailscale Serve is an
acceptable Phase 2 convenience, not a requirement.

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
`session-help.jsonl` stays local and is excluded from the remote ROI until
it gets an endpoint in Phase 2.

## Consequences

- **Local-first holds.** Without `ENGRAM_REMOTE_URL` the client is byte-for-
  byte the current install; the server is itself a local-first install that
  happens to listen on a socket. The test suite's default path is unchanged;
  the HTTP layer is tested by driving the same functions through a test
  client.
- **Measured, not vibes.** Every remote call is a ledger row with
  `transport=remote`, `rtt_ms` and `outcome ∈ {ok, timeout, unreachable}`;
  `engram roi` reports p50/p95 RTT per endpoint and the unreachable rate. The
  Phase 1 acceptance gate is numeric: guard p95 ≤ 350 ms end-to-end over the
  tailnet, recall p95 no worse than today's local p95, zero hook errors. A
  Mac ↔ US-West VM round trip is tens of milliseconds; from Japan it is
  ~100–150 ms, still inside the guard budget but visible in the report.
- **Fail-closed governance is untouched.** Remote clients gain no execution
  authority; proposals are still inbox items; approvals and reflex runs are
  operator actions on the server host. Nothing in this ADR adds a way for a
  client to approve.
- **Single-owner writes get stronger.** The server is the only process that
  opens `memory.db` for writing; backups, chunk-sync export and self-check run
  there. Clients hold no store.
- **The first daemon.** ADR-0007 was proud that Engram "ships no daemon";
  remote mode is one, by necessity, under systemd. It is `engram serve`, not
  `engram-mcp` — the stdio server and its watchdog stay as they are and must
  not be run as a service.
- **A new silent failure.** An unreachable VM means no memory, quietly. The
  server's self-check cannot see client failures, so the client's
  `engram doctor` must show last-success time and recent failure count, and
  the unreachable rate in `roi` is the alarm.
- **ADR-0001 and ADR-0002 are unchanged.** No backend abstraction; the vector
  dimension lives in the server's `schema_meta` and clients never see
  embeddings. The origin stamp converges with the portable-brains plan rather
  than adding a second provenance scheme.
