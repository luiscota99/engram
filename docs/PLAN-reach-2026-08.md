# Plan: Reach & Resilience — August 2026

**Thesis:** engram's retrieval, measurement, and governance are ahead of everything we've
evaluated. What it lacks is *reach* — it works deeply in one client instead of adequately in
ten — and a handful of session-resilience details. This plan closes that gap without touching
the invariants (local-first, measured-not-vibes, fail-closed) and without diluting the deep
Claude Code integration that makes it unique.

**Positioning line for everything we build:** *la capa de memoria medible debajo de todas tus
herramientas* — every injection accounted for, retrieval benchmarked in CI, nothing changes
memory without your approval.

**Gate:** all build phases start after the July 31 ROI verdict. Phase 0 is decision-and-data
work only. Each phase ships with its metric wired into `engram roi` — if we can't measure it,
we don't build it.

---

## Phase 0 — Decisions & baselines (now → July 31, zero code)

1. **Decide the open inbox proposals** that this plan depends on (adapter registry, small
   steals batch, naming). The plan assumes at least the adapter registry is approved.
2. **Naming decision before any public artifact.** The current name is heavily crowded in
   this exact category (multiple active projects and one commercial product). If engram is
   ever published — brew, marketplace, MCP server name — it needs a distinctive name and an
   `mcp__<name>__*` tool namespace that cannot collide. If it stays private, no action.
   Either way, decide *now* so Phase 1 registers the right names.
3. **Baselines for the plan's metrics:** current per-client tool adoption is trivially known
   (one client: Claude Code, 100%). Record time-to-setup on a clean machine for the current
   manual flow, injection acceptance rate post-compaction (audit log has the data), and
   dup-gate hit rate — these are the numbers Phases 1–3 must move.

## Phase 1 — Multi-client reach (~1 week, the centerpiece)

**Problem:** the promise is "the durable memory layer beneath all your tools"; the reality is
deep Claude Code integration plus untested claims. Meanwhile the client landscape (Cursor,
Antigravity, Windsurf, Gemini CLI, Codex, VS Code, OpenCode) shares three MCP config shapes
and a rules-file convention — supporting many clients is a data problem, not a code problem.

**Build:**
- `engram setup <client>` backed by a **declarative adapter registry**: one table entry per
  client = `{mcp_config_path, mcp_json_shape, instruction_surface(s)}`. Three JSON shapes
  cover the ecosystem (`mcpServers` object / `servers` object / `mcp` object). Instruction
  injection via **marker-delimited managed blocks** (`<!-- BEGIN ENGRAM PROTOCOL -->…END`)
  inside each client's rules file — idempotent on re-run, never touches user content, and
  `engram setup <client> --remove` cleanly deletes only our block.
- **Per-client protocol text** for clients without hook support: a short rules blurb that
  teaches the agent the MCP drill-in pattern (search → read_item), when to propose captures,
  and to run `resume` at session start. Hooks stay a Claude Code superpower; the floor
  everywhere else is MCP tools + protocol.
- **Deferred-tools first-turn nudge:** clients that lazy-load MCP tools never discover memory
  exists. The protocol block's first line instructs loading the memory tools immediately
  (ToolSearch-style select line where applicable).
- **Client quirks table** maintained as data, not code: e.g. Cursor requires pasting global
  rules through its Settings UI (generate the file + print the instruction); Antigravity
  reads Gemini-family config paths. Start with the two clients actually in use here —
  **Cursor and Antigravity** — then add the rest cheaply.

**Metrics:** engram tool-calls per client per week (audit log already records source);
time-to-working-setup per client < 60s; zero regressions in the Claude Code paths (CI).

## Phase 2 — Session resilience everywhere (~2-3 days)

**Problem:** two real gaps in continuity. (a) After context compaction, the agent loses the
injected memory context and nothing restores it. (b) Deep resilience (checkpoints, resume,
handoffs) exists only where hooks exist.

**Build:**
- **Post-compaction recovery:** SessionStart hook matcher for the compaction event re-injects
  a compact resume block (latest milestone handoff + open decisions count + last checkpoint).
  We already checkpoint *before* compaction (PreCompact); this closes the loop *after* it.
- **Resume-first protocol everywhere:** the Phase 1 rules blurb makes `memory_resume` the
  documented first action in every client, so cross-session continuity doesn't depend on
  hooks existing.
- **Checkpoint via MCP for hook-less clients:** a lightweight `memory_checkpoint` MCP tool
  (same core as the Stop hook) the protocol asks agents to call at milestones. Ambient
  per-turn capture stays Claude-Code-only; deliberate milestones become universal.

**Metrics:** injections accepted in the 3 turns after a compaction event (measurable from
ledger + transcript timestamps); % of non-Claude sessions that begin with a resume call.

## Phase 3 — Knowledge lifecycle polish (~3-4 days)

**Problem:** evolving knowledge ("the deploy target is now X") currently creates near-dup
friction: the dedup gate blocks, or a new item + supersedes edge is manual ceremony.

**Build:**
- **Topic-key upsert, with history:** optional `--topic <key>` on capture; same
  project+scope+key updates the living item, bumps a revision counter, **and records the
  prior version via the existing supersedes/bi-temporal machinery** (v27 makes the history
  free — an update is an invalidate+re-assert, queryable `as_of` any date). A living document
  with a full audit trail, not an overwrite.
- **Zero-config first-run:** `engram init` auto-provisions DB + local embedder profile and
  ends with a scripted, self-verifying `add → search` round-trip that prints the hit. Target:
  first successful search < 60s on a clean machine. Plus `llms.txt` so agents can read the
  docs, and an installable Claude Code skill that wires a repo end-to-end.
- **Private-content hygiene:** strip `<private>…</private>` spans at capture time across all
  ingest paths — cheap, and it composes with the export filters already queued for sync.

**Metrics:** dup-gate friction rate on evolving items (before/after); time-to-first-search;
revision-history correctness covered by tests.

## Phase 4 — Sharing, later and only if wanted (not scheduled)

Chunk-based git sync already exists. The remaining pieces if team use ever materializes:
export filters (already an inbox item), a read-scoped view (polish the existing TUI browser
rather than building any server), and nothing else — no hosted anything, no dashboards, no
accounts. Local-first is the identity, not a tier.

## Explicitly not doing (so the plan stays honest)

- **Unconditional context dumps at session start** — the relevance gate is the product;
  recency dumps are its anti-pattern, whatever their token budget.
- **Verbatim capture of every user prompt** — privacy-negative, low signal.
- **Prompt-prose enforcement where code enforcement exists** — protocols are the floor for
  hook-less clients, never a replacement for hooks/gates where we have them.
- **Any server/cloud/dashboard layer.**
- **LLM-as-judge in the write path** — judging stays in sleep-time passes with local models,
  proposals routed through the inbox.

## Sequencing against the already-queued retrieval work

The landscape retrieval items (cross-encoder rerank, temporal grounding, overlap guard,
entity boost) compete for the same post-freeze time. Recommended order after July 31:
**adapter registry first** (unique new capability, unblocks every other tool you use daily),
then retrieval items in fit-harness-measured order, interleaving Phase 2/3 items as small
fills. If the ROI verdict is weak, Phase 1 still stands on its own merits — reach multiplies
whatever value the verdict confirms; it doesn't presuppose it.
