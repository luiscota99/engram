# ADR 0008: Skill validation — proof that memory changes behavior

Date: 2026-07-11
Status: accepted

## Context

Engram's reuse metric proves a memory gets *retrieved*; it never proved a memory
*works*. A captured skill could be wrong, vague, or something the model already
knew — and still accrue usage_count every time it surfaces. Superpowers (obra,
252k★) solved the analogous problem for its curated methodology skills with
"TDD for skills": *if you didn't watch an agent fail without the skill, you
don't know if the skill teaches the right thing.* We adopt the rigor, applied
to Engram's personal, accumulated memory rather than universal curriculum.

## Decision

A **validation test** (`skill_tests`, schema v19) attaches a `scenario` +
`assertion` to a memory item. Running it asks the LLM the scenario twice — cold
(baseline) and with the memory's content injected (treatment) — and grades each
against the assertion (`contains`, deterministic, or `llm_judge`). Verdicts:

- **validated** — baseline FAILS, treatment PASSES: the memory earned its keep.
- **redundant** — both pass: the model already knew it; the memory adds nothing.
- **ineffective** — treatment fails: the memory didn't fix the behavior.
- **regressed** — baseline passed, treatment failed: the memory made it worse.
- **untested** — no LLM backend reachable (degrades gracefully).

Only **validated** counts. A test that passes cold proves nothing — the exact
loophole "watch it fail first" closes.

Integration: `engram route` badges validated prior art (`✓validated`) — proven
memory is stronger than merely-retrieved memory. The daily self-check files a
decision for any reused-but-unvalidated skill ("add a proof test?"). CLI:
`engram validate add/run`.

## Consequences

- Validation complements reuse: reuse says "retrieved a lot", validation says
  "demonstrably helps". Together they are a far stronger promotion signal than
  either alone.
- The baseline/treatment loop needs the optional LLM layer; without it tests
  are `untested`, never silently "passed". `contains` grading keeps the common
  case deterministic and unit-testable.
- This is the discipline half of the Superpowers lesson; Engram keeps its own
  differentiator (personal accumulated memory + retrieval + reflexes) — we
  borrow the rigor, not the curated-curriculum model, which Superpowers owns.
