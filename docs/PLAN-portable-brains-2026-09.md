# Plan: Portable Brains — September 2026

**Thesis:** "transferable" is three different jobs, and engram already does one and a half of
them. *Moving* a whole brain to another machine works today by construction (a brain is one
directory with one SQLite file, and `engram restore` swaps a `.db` in safely). *Sharing* a
subset already exists as `engram sync export/import` (chunk sync: content-hash identity,
append-only gzipped JSONL, merges in git without conflicts) — but it covers five tables out of
twenty, stamps no provenance, has no filters, and is not in the README. *Merging* one brain into
another does not exist. This plan finishes the first two and builds the third on top of them,
without a new format, a server, or a schema-wide identity column.

**Invariants that shape every phase:** local-first; agents propose, users decide (foreign
content that could change behaviour — reflexes, near-duplicates, name collisions — arrives as
inbox items, never as silent writes); non-use is never a deletion signal (a merge inserts or
proposes, it never removes); measured-not-vibes (each phase ships with a round-trip metric).

**Gate:** Phase 0 is decisions only. Phases 1–3 are sequenced after the adapter registry in
[PLAN-reach-2026-08.md](PLAN-reach-2026-08.md) unless the decision in Phase 0 reorders them.

---

## Baseline — what transfers today, table by table

| State | `brain` dir copy / `restore` | `sync export/import` (chunk) | `backup` JSON |
|---|---|---|---|
| mistakes, patterns, skills, conversations, prompts | yes | yes (content-hash identity) | yes, **no importer** |
| tags / item_tags | yes | yes | yes, no importer |
| memory_relations (v27 edges) | yes | **no** | yes, no importer |
| item_projects (project affinity) | yes, but paths are absolute (`/Users/…`) | **no** | yes, no importer |
| item_pins, memory_facts, skill_tests | yes | **no** | yes, no importer |
| reflexes + runs | yes (approved scripts travel unreviewed) | **no** | yes, no importer |
| inbox, checkpoints | yes | no (correct — session-local) | yes |
| retrieval_feedback, memory_dynamics (FSRS), injection ledger | yes | no (correct — personal telemetry) | partial |
| embeddings (sqlite-vec) | yes, only valid for the same embed model | no (regenerated on import) | no |
| trigger_ngrams (v28) | yes | rebuilt by `index_in_fts` on import | rebuilt by migration |

Two conclusions fall out of the table. First, the JSON backup is an archive, not a transfer
format — it needs no importer, it needs to stop being described as one. Second, chunk sync is
the right substrate for sharing and merging: it already replays foreign entries through the
canonical `memory_ops.create_*` paths (FTS, tags, trigger shingles, embedding queue all behave
like a local add) and its identity rule — a content hash over each type's dedup fields — is the
same rule write-time dedup uses. Merge does not need a new identity column; it needs that rule
extended to the tables chunk sync skips.

---

## Phase 0 — Decisions (zero code, ~1 hour)

Filed as inbox decision `research:portable-brains:plan`. Decide:

1. **Knowledge vs telemetry.** Proposed line: *knowledge* (the five content types, tags,
   relations, pins, facts, project affinity by name) transfers; *telemetry* (feedback, FSRS
   dynamics, ledger, checkpoints, inbox) never does — base rates from another machine or
   person are not comparable and would poison the stability curves.
2. **Provenance stamp.** Imported items must be distinguishable from local ones forever.
   Zero-schema option: a code-set tag `origin:<machine-or-brain>`. Cleaner option: a
   `provenance` column on the five content tables, mirroring `memory_relations.provenance`
   (v27). Recommendation: the column — tags are user-editable, provenance must be code-set
   (anti-poisoning, same argument that won for v27).
3. **Sequencing.** Phase 1 is small and independent; it can land before the adapter registry
   without displacing it. Phases 2–3 go after.
4. **Document chunk sync now.** The README has no section for `engram sync`; the dual-surface
   drift is the kind of gap the audit flagged (Pattern #9). One paragraph, before any code.

---

## Phase 1 — Whole-brain bundle (~1 day)

The existing pieces, made into one command with a manifest so the receiving side can check
compatibility before touching anything.

- `engram brain export <name> [--out file.engram]` → tarball containing `memory.db` (written
  through the SQLite backup API, WAL-safe, never a file copy) and `manifest.json`:
  `schema_version`, `engram_version`, `embed_model`, `embed_dimensions`, per-table counts,
  `origin_host`, `exported_at`. Telemetry tables are exported empty unless
  `--include-telemetry` (for moving *your own* brain to *your own* new machine).
- `engram brain import <bundle> --as <name>` → refuses if the bundle's schema version is
  newer than the code (older is fine — migrations run forward). Restores into
  `~/.engram/brains/<name>/` via `restore_database` (snapshot-first, integrity-checked),
  regenerates the launcher with the local `engram` path, then:
  - **paths:** `projects.path` rows that do not exist locally are kept but flagged; the
    import report lists them and offers `engram brain remap <name> <old-prefix> <new-prefix>`.
    Affinity is never dropped — it is the only link between a memory and where it was learned.
  - **embeddings:** if `embed_model`/`embed_dimensions` differ from the local config, the vec
    table is cleared and a re-embed is queued; the report says so. Same model → vectors are
    kept and spot-checked (10 random rows re-embedded, cosine ≥ 0.999).
- Metric: a round-trip test (`export → import --as copy`) must produce identical result lists
  for the labeled eval queries in `benchmarks/` and a clean `engram doctor`. Both run in CI
  against a scratch DB (instrument gate: never the real DB — Mistake #26).

## Phase 2 — Chunk sync completeness + provenance (~2 days)

Make `engram sync` carry everything on the *knowledge* side of the Phase 0 line, and make
every imported row say where it came from.

- Extend `TYPES` with relations (endpoints referenced by content hash, not integer id, so they
  resolve on any machine), pins, memory_facts, skill_tests, and project affinity **by project
  name** (paths never leave the machine).
- Reflexes travel, but arrive as **inbox proposals with the script body attached** — an
  imported automation is exactly the thing the fail-closed rule exists for. Never auto-approved.
- Provenance stamp from Phase 0 applied on import; the existing `machine` field in the chunk
  record becomes the stamp's value. `engram list --origin <x>` and `engram roi` gain a
  by-origin split so the reuse rate of imported memories is measured separately from local
  ones (the honest way to learn whether someone else's memories help you at all).
- Filters on export: `--type`, `--tag`, `--project`, `--since`. Subset sharing ("only the mtg
  brain's patterns") is a filter on the existing exporter, not a new mode.
- The UNIQUE-name collision path (patterns/skills/prompts) stops being a silent skip: the
  foreign version is filed as an inbox `consolidate:` decision with both bodies.
- Metric: `engram sync status` reports coverage per table; the round-trip test from Phase 1
  extends to two DBs converging through a shared directory (the existing
  `test_divergent_machines_converge` widened to every knowledge table).

## Phase 3 — Merge brains (~2–3 days)

`engram brain merge <src> --into <dst> [--dry-run]` is chunk sync pointed at a temporary
directory, plus the review gate that a merge between *different* corpora needs and a sync
between *your own* machines does not.

- Identity match (same content hash) → skip, nothing to decide.
- Near-duplicate (`memory_find_similar` above the consolidation threshold) → **not inserted**;
  filed as `consolidate:` inbox decision showing both, exactly like the self-check's existing
  pairs. The user merges or keeps both.
- Genuinely new → inserted with provenance `merge:<src>`.
- The source brain is never modified. The report prints inserted / proposed / skipped counts
  and `--dry-run` prints the same report without writing.
- Metric: doctor finds zero soft-FK orphans after a merge; the by-origin reuse split from
  Phase 2 tells you, a month later, whether the merged memories earned their place. If they
  did not, the answer is still a proposal to the user — never an automatic prune.

## Explicitly not doing

- **A hosted sync service, accounts, or a dashboard.** A git repo or any synced folder holding
  the chunk directory is the whole transport, by design.
- **CRDT / live bidirectional sync.** Append-only chunks already merge without conflicts; the
  only real conflicts are semantic, and those go to the inbox.
- **Transferring telemetry between people or machines by default.** FSRS state, feedback and
  the ledger describe *your* usage; they are not knowledge.
- **A UUID column on every table.** The content-hash identity rule exists and is already
  proven by chunk sync's idempotency tests; adding a second identity would create the drift
  the v23 FTS fix removed.
- **Auto-approving anything that arrived from outside** — reflexes, near-duplicates, or name
  collisions. Import proposes; the user decides.

## Sequencing against the queued work

Unfreeze order after the July 31 verdict was: guard fast path (done, v28) → adapter registry
→ capture policy → subtraction pass. Phase 0 and Phase 1 here are small enough to slot in
before the adapter registry if the inbox decision says so; Phases 2–3 wait until after it,
because the by-origin ROI split they depend on is more useful once more than one client feeds
the ledger.
