# Agent-memory landscape — July 2026

Per [RESEARCH-HANDOFF-2026-07-21.md](../../RESEARCH-HANDOFF-2026-07-21.md). Method: four parallel
deep-read agents (full-code reads of shallow clones + primary web sources, every claim dated) plus
direct verification against engram at v27. Evaluation lens, per the handoff's norte: value for a
single-user personal assistant; local-first, measured-not-vibes, and fail-closed governance are
non-negotiable. Proposals are filed as inbox decisions (`engram inbox`), never applied directly.

Clones read: mem0ai/mem0 @ ca2abca, getzep/graphiti @ 4674e1e, letta-ai/letta @ b76da909
(all fetched 2026-07-22). Papers: arXiv 2605.25430 (CODESKILL, v1 2026-05-25), arXiv 2606.30306
(Always-On Agents survey, v1 2026-06-29). Web sources dated inline.

---

## 1. Findings per project

### Mem0 (~51k stars, $24M raised)

**What it is now:** ADD-only extraction. The famous ADD/UPDATE/DELETE/NOOP loop is **dead code**
(`configs/prompts.py:176` referenced only by tests); the V3 pipeline extracts facts with one LLM
call per `add()`, dedups by exact MD5 against top-10 semantic neighbors, and never resolves
conflicts at write time — their own BEAM benchmark shows the bill: **contradiction_resolution
35.7 @1M memories** ([State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026),
pub. 2026-07-22). Typed graph relations were **removed** — platform "Graph Memory" is untyped
entity co-occurrence ("connections are inferred from co-occurrence rather than declared", their
docs, fetched 2026-07-22). OSS has no decay (platform-only, params *error into a sales pitch* —
a 1,582-line `notices.py` upsell machine).

**What they do better:** extraction-prompt engineering (observation-date grounding: every relative
time resolved to absolute dates at write time; "capture the transition, not just the new state";
anti-echo rules), batch-everything efficiency (one LLM call → N facts), integer-ID remapping so
the LLM can't hallucinate memory UUIDs, and genuinely superb onboarding (zero-config constructor,
4-command self-verifying first run, agents can onboard *themselves* via `mem0 init --agent`,
installable skills that teach Claude Code to integrate it).

**Their benchmarks vs ours:** LongMemEval 94.4 is end-to-end QA accuracy (LLM judge, top-200
retrieval budget, managed platform with "proprietary optimizations not available in the
open-source SDK" — their words). Not comparable to engram's deterministic retrieval-only
R@5=0.538. Rigor grade C+: token budgets and CIs disclosed, harness released, own bad numbers
published; but unnamed judge, self-authored benchmark (BEAM), unreproducible on OSS.

### Zep / Graphiti (temporal knowledge graph)

**Temporal model vs v27 — split decision, engram ahead where it counts:**

| Capability | Graphiti | engram v27 |
|---|---|---|
| Bi-temporal edges (event + ingestion time) | ✓ | ✓ parity |
| Write-time contradiction detection | ✓ LLM nominates + deterministic overlap guard | ✗ (manual invalidate) |
| Dating facts from free text ("last week") | ✓ dedicated small-model prompt | ✗ |
| Graph-expansion retrieval (BFS from hits) | ✓ | ✗ |
| `status` + `as_of` point-in-time API | ✗ (manual date filters) | ✓ |
| Supersedes lineage (what replaced what) | ✗ (timestamps only, causal link lost) | ✓ one-to-many |
| Provenance/actor code-set (anti-poisoning) | ✗ — fact text, dates, invalidation targets all LLM-authored | ✓ |
| Race-safe writes | ✗ (semaphores; concurrent adds can duplicate) | ✓ CAS |
| Per-memory decay | ✗ | ✓ FSRS |
| LLM-free ingest | impossible by design | ✓ |

**The 63.8% LongMemEval claim** ([arXiv 2501.13956](https://arxiv.org/abs/2501.13956)): gpt-4o-mini
as reasoner over ~1.6k-token graph context, GPT-4o judge; 71.2% with gpt-4o. **It is a different
metric AND a different resource class** than engram's fully-local R@5 (274MB embedder, no LLM).
A faithful reproduction recipe (fresh DB per question, session ingest with real dates, our RRF
retrieval → 2k-token context → gpt-4o-mini composer → official judge; plus a fully-local composer
variant reported as its own resource class) is in the Graphiti agent's report and filed as an
inbox decision. Cost estimate: a few dollars of API for the honest apples-to-apples run.

**Their ingest tax:** every episode costs ~3 + N_nodes + 2×N_edges LLM calls, seconds of latency,
requires a graph DB (Neo4j/FalkorDB/Neptune) and works "best" with OpenAI structured output.
Engram ingests in milliseconds with zero network. Their intelligence is at write time; ours is at
retrieval/consolidation time — ours is the right shape for local-first.

**Name collision, important:** lumetra.io markets a commercial memory product **named "Engram"**
with published LongMemEval numbers (91.6% claimed; found 2026-07-22). The naming concern is no
longer hypothetical — there is a direct namesake competitor publishing benchmarks.

### Letta (ex-MemGPT, $70M val)

**Core memory = labeled blocks compiled into the system prompt every request** (persona/human
20k-char caps; docs recommend <50k chars total). Measured cost at their shapes: **~2.5k–10k tokens
every prompt, unconditional** — versus engram's relevance-gated ~202 tokens/injection. Their
guaranteed-presence benefit is real (identity facts survive off-topic prompts) but the economics
lose 10–50× for a personal assistant; pinning + hooks covers the need.

**Sleep-time agents:** a companion agent sharing live block IDs runs every N turns over the
**delta since a persisted watermark**, with precise-edit tools and a prompt that mandates absolute
dates and selectivity. The watermark, the diff-shaped edit format (str_replace/insert — proposals
the size of a diff, not a paragraph), the "absolute dates, never 'recently'" rule, and read-only
blocks with visible char budgets are all portable. The autonomous-write-to-live-memory part
violates fail-closed and is rejected.

**Runtime-centric vs tool-centric — confirmed for engram:** Letta requires its server to *own*
the agent loop (FastAPI + Postgres + server-held API keys). For one user across Claude Code,
Cursor, and Antigravity, that means a second parallel LLM loop and losing the IDE's native agent.
Honest steelman recorded: server ownership guarantees maintenance runs and gives observability —
which argues for engram keeping the cron path (`engram self-check`/`sleep`) as its guarantee
mechanism, not for switching architectures.

### CODESKILL (arXiv 2605.25430)

Self-evolving skills via a GRPO-trained 4B policy that Adds/Merges/Drops/Evolves markdown skills
from trajectories. Two granularities (task-level + **event-driven micro-skills** keyed to error
signatures — their ablation shows both needed). Eval methodology worth stealing outright:
**adherence-gated counterfactual reward** (execution credit only when a judge confirms the run
actually followed the skill — stronger than our citation-echo), OOD holdout (Terminal-Bench 2),
and bank-health metrics (count, add/merge/drop mix, reasoning-steps-saved: 44.1 → 35.2).
Their own ablation: full autonomous lifecycle maintenance *costs* ~2% pass rate to shrink the
bank 1252→676 — a trade they make silently and we would route through the inbox. No executable
tier at all ("natural-language instruction skills" only) — engram's reflex ladder is ahead of the
closest academic work.

### Always-On Agents survey (arXiv 2606.30306; 435-work corpus)

The field "concentrates more heavily on accumulating and retrieving state than on governing,
recovering, or relinquishing it" — rollback: 27/435 works; authority: 72/435, the rarest axis.
Position of engram's trust model, axis by axis: **authority ahead** (inbox-propose/user-decide +
hash-pinning is literal authority monotonicity; the survey barely discusses propose-then-approve
at all — engram is ahead of the literature, not just the median system); **mutability ahead**
(user-gated promotion, windowed demotion, CAS, bi-temporal); **actionability ahead** (the
memory→skill→reflex ladder is exactly "state typed by actionability"); **recoverability ahead on
traceability** (the injection ledger is the "handle back to the records that justified an
action"); **scope at par** (no per-item scope typing yet — cheap gap); **deletion verified ahead**
during this review: `delete_item` fully propagates (FTS+vec+soft-FK, hardened 2026-07-17), and
`invalidate` is *governed retention* (title-marked, default-filtered, reason recorded in
memory_facts) rather than the survey's silent-retention failure. One honest residue: the embed
cache retains vectors of deleted text on disk (never searchable) — gc candidate, filed.
Their AOEP-v0 governance scorecard is a positioning asset: mainstream wrappers fail to expose
the fields engram has.

Bibliography second-pass candidates: Trace2Skill; subtask-level memory (CODESKILL's strongest
baseline); ReasoningBank; Xu et al. 2026 (long-term state poisoning — attack scenarios to replay
against our gates); Lin et al. 2026 (memory-security survey — a second governance checklist).

### DIY folklore + lab-native memory (verified live 2026-07-22)

- **Cursor removed its Memories feature** (v2.1.x; users told to export to Rules —
  [forum](https://forum.cursor.com/t/about-cursors-memory-record-feature/107355),
  [third-party](https://memnexus.ai/blog/2026-02-20-cursor-persistent-memory)). A major IDE
  shipped auto-accumulating memory, watched it rot, and retreated to user-curated rules.
- **Claude Code's native memory converged on curation** ([docs](https://code.claude.com/docs/en/memory),
  fetched 2026-07-22): CLAUDE.md guidance is now *under 200 lines* (folklore said 500);
  auto-memory loads only the first 200 lines/25KB of MEMORY.md; writes near the cap trigger
  shorten-reminders; `/doctor` proposes trims; memory files carry `modified` staleness timestamps.
  The labs are rebuilding engram's curation stack (caps, trims, staleness) — without the
  measurement half (no decay model, no dedup, no helpfulness signal, machine-local only).
- **Interop is the ecosystem's lingua franca:** Claude Code reads AGENTS.md via import/symlink and
  `/init` ingests Cursor and Copilot rules. Engram has `import-claude-memories` (one-way,
  content-hash idempotent) but **no export**. The natural positioning — "the durable, measured
  layer beneath all your tools' native memories" — needs the bidirectional bridge.

**Validation for the README:** naive accumulation demonstrably fails at industry scale (Cursor
removed it; Claude Code caps it; Mem0's ADD-only scores 35.7 on contradictions; the survey's
435-work corpus shows 6× more effort on accumulation than governance). Engram's bet — curation,
decay, measurement, governance — is the part everyone else is now discovering they need.

---

## 2. Steal-list, ranked by value-for-Luis

Each filed as an inbox decision (`engram inbox`); approve with `engram decide <id>`.

1. **Local cross-encoder rerank after RRF** (Graphiti). Rerank top-20 with bge-reranker-base
   (~280MB, CPU, no LLM). The single biggest available lever on LongMemEval R@5=0.538.
   Hypothesis: +several points R@5 on LongMemEval slice, no regression on the 100-query gate,
   +50–200ms/query. Cost: ~2-3 days.
2. **Deterministic temporal-overlap guard on invalidation** (Graphiti
   `edge_operations.py:538-573`). Only invalidate when intervals actually overlap and the old
   fact predates the new; expire the *new* edge on out-of-order arrival. Pure code, zero LLM.
   Hypothesis: fewer wrong invalidations on knowledge-update evals. Cost: ~half day.
3. **Consolidation watermark + delta processing** (Letta). Persist a last-processed cursor;
   `engram sleep`/session-review reads only the delta. Direct fix for the O(session) cost class
   behind MISTAKE #25. Hypothesis: consolidation LLM cost ∝ delta with zero recall loss.
   Cost: ~half day.
4. **Temporal grounding at capture** (Mem0 prompt rules + Graphiti's batched `extract_timestamps`
   + Letta's absolute-dates rule — three systems converged). Resolve relative dates to absolute
   at write time; tag event/state/plan/preference; run as sleep-time pass with the local model.
   Hypothesis: targets the two weakest LongMemEval categories (temporal 0.45, preference 0.30).
   Cost: ~1-2 days.
5. **Diff-shaped consolidation proposals** (Letta). Proposals as str_replace patches instead of
   whole-entry rewrites — approval in seconds; attacks the throughput bottleneck of fail-closed
   governance. Hypothesis: proposals-reviewed-per-session rises. Cost: ~1-2 days.
6. **Adherence-gated validation + OOD split** (CODESKILL). Validation harness upgrade: a local
   judge confirms the run *followed* the skill's steps (not just cited it) before strong-positive
   credit; validate each skill on one scenario it wasn't captured from. Hypothesis: cleaner ROI
   helpfulness numbers; predicts which skills merit reflex promotion. Cost: ~1-2 days.
7. **LLM-free entity store + rank boost** (Mem0). Proper-noun/quoted-text extraction into an
   entities table; query entities boost linked memories (their popularity damper). Serves "what
   do I know about X" queries. Hypothesis: R@5 gain on an entity-centric label subset.
   Cost: ~3-5 days.
8. **Onboarding quick wins** (Mem0, adapted local-first): `engram init` zero-config profile +
   4-command self-verifying first run (add→search prints the hit) + an installable Claude Code
   skill that wires engram into a repo + `llms.txt`. Metric: time-to-first-successful-search on a
   clean machine < 60s. Serves the conejillos-de-indias loop without any cloud. Cost: ~2-3 days.
9. **LongMemEval apples-to-apples reproduction** (Zep recipe, §1). One-time eval spend (~a few
   dollars API for the honest comparison run + local-composer variant). Deliverable: published
   table with resource-class caveats — the credibility asset the handoff asked for.
10. **Small, near-free items**: bank-health metrics in `engram roi` (skill count trend,
    add/merge/drop mix, reasoning-steps-saved); budget-visible pinned tier (chars_current/limit,
    read_only flags); forward-looking synopsis line on milestone handoffs; AOEP-v0 self-scorecard
    run (incl. fixing the embed-cache deleted-content residue); integer-ID remap in any
    LLM-facing consolidation prompt; 1-hop relation expansion via recursive CTE as a search
    experiment behind the fit harness.

## 3. Reject-list (explicit, with reasons)

- **ADD-only/never-delete memory** (Mem0): their own benchmark scores 35.7 on contradiction
  resolution; conflicts with FSRS/gc/supersedes. The market leader's architecture is engram's
  anti-pattern.
- **Untyped co-occurrence graphs replacing typed relations** (Mem0): they went backwards; adopting
  this would delete v27's temporal validity, provenance, and anti-poisoning.
- **Mandatory LLM extraction at ingest** (Graphiti): seconds + cents per episode, model-authored
  temporal fields (poisoning surface), graph-DB dependency. Keep ingest deterministic; enrich at
  sleep time.
- **Graph database backend** (Graphiti): SQLite recursive CTEs cover personal-scale traversal.
- **Community detection + hierarchical LLM summarization** (Graphiti): org-scale feature; O(cluster)
  LLM calls for a roll-up engram's consolidation already provides.
- **Always-in-context core blocks at Letta scale**: 2.5k–10k unconditional tokens/prompt vs ~202
  gated; wrong economics by 10–50×.
- **Runtime-centric server** (Letta): second LLM loop, loses the IDEs' native agents; incompatible
  with the three-client reality.
- **Autonomous memory self-editing / autonomous merge-drop** (Letta sleeptime writes, CODESKILL
  lifecycle): violates fail-closed; CODESKILL's own ablation shows silent maintenance costs
  pass-rate. Consolidation pressure yes — through the inbox.
- **RL-trained skill-management policy** (CODESKILL): wrong cost profile for local-first, and it
  deletes the human from the authority loop — the axis the survey says is rarest and most valuable.
- **Shadow accounts, per-call telemetry, upsell notices** (Mem0): growth machinery; privacy-negative;
  antithetical to local-first honesty. (The *self-verifying onboarding* is stolen without these.)
- **LLM-judge QA headline chasing** (Mem0's 94.4, Zep's 63.8 as banners): different metric, different
  resource class. Engram publishes retrieval metrics + the honest reproduction (steal #9), with
  caveats, or nothing.
- **Graphiti's RRF constant (k=1)** as-is: sweep k in our fit harness instead of copying a
  winner-take-most constant.

## 4. Follow-ups filed elsewhere

- Inbox decisions: one per steal-list item above (source=`research`, finding keys
  `research:landscape:*`).
- Name collision (lumetra "Engram") noted for the positioning discussion — not a code decision.
- Bibliography second-pass list retained in §1 (survey section) for a future research session.

---

## Addendum (2026-07-24): Gentleman-Programming/engram — the direct namesake competitor

Deep-read of the fourth "Engram" (clone @ 763a6ba; GitHub API 2026-07-24: **5,693 stars,
603 forks, 30 releases, created 2026-02-16** — 5 months old, PR #604 velocity, backed by a
major Spanish-speaking dev-education audience).

**What it is:** Go single binary, SQLite+**FTS5-only** (the embedding columns exist in schema
but nothing reads or writes them — verified), untyped prose observations, exact-hash dedup,
no decay/feedback/measurement of any kind (no benchmark, no recall metric, no eval harness
anywhere in the repo). Injection is an unconditional ~2-3K-token recency dump every session —
the exact anti-pattern our relevance gate exists to kill. Capture is prompt-prose-enforced
("please call mem_save") rather than code-enforced. Where they are genuinely ahead:
**distribution and breadth** — 13 agent clients via a declarative adapter registry (3 MCP JSON
shapes + marker-block rules injection), brew tap, Claude marketplace plugin, self-hosted
cloud sync with dashboard, 1,618 test functions, goreleaser release engineering.

**Steal-list (filed as inbox decisions):** the multi-client adapter registry (~300 lines of
logic → Cursor/Antigravity/Windsurf support in days); topic-key upsert *with* revision history
(they lose history); post-compaction SessionStart recovery hook; first-message ToolSearch
force-load for deferred-MCP clients. Their git-chunk sync — engram already shipped the
equivalent (2026-07-17), independently.

**Reject:** LLM-CLI-as-judge conflict scanning; unconditional context dumps; verbatim capture
of every user prompt; the Postgres dashboard layer; regex-only passive capture.

**Positioning (the consequential part):** the collision is total — same name, same tagline,
same substrate, same category, and they own every public registry surface (brew tap, plugin
marketplace name `engram`, the `mcp__engram__*` tool namespace — side-by-side installs would
literally collide). Publishing under "engram" now means being presumed the clone. What their
repo proves is genuinely absent there, not just unmarketed: measured memory (ledger/ROI/CI-gated
R@5), real hybrid retrieval, bi-temporal+code-set-provenance relations, governed automation
(FSRS, gates, hash-pinned reflexes, inbox). Strategic options filed as a decision: rename
before any public push (positioning: "the *measured* memory layer"), or stay private and
treat GP-engram as upstream R&D for client breadth while the moat stays retrieval quality +
measurement — which untyped rows + FTS-only + no evals cannot cheaply retrofit.
