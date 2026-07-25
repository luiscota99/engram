# Engram vs the agent-memory field (2026)

**Thesis:** Engram is not trying to be another conversational personalization layer.
Its SOTA claim is narrower and harder to copy:

> **Local-first engineering memory that compiles into action** —
> typed mistakes / patterns / skills, hybrid FTS+vector retrieval, IDE enforcement
> hooks, measured ranking, and human-gated reflexes on an Action Ladder.

Competitor numbers below mix **retrieval recall** and **QA accuracy**. Engram
publishes retrieval metrics with caveats; see [`benchmarks/BENCHMARKS.md`](../benchmarks/BENCHMARKS.md).

## Comparison matrix

| System | Bet | Strength | Weakness | Engram response |
|--------|-----|----------|----------|-----------------|
| **Mem0** | Chat personalization (vector + optional graph) | Adoption; ADD/UPDATE/DELETE/NOOP | Cloud / Pro graph; flat facts | Fact ops via inbox; stay local MIT + typed schema |
| **Zep / Graphiti** | Bi-temporal knowledge graph | `as_of` / validity windows | SaaS; slow graph ingest | First-class `as_of` search; FTS searchable immediately |
| **Letta (MemGPT)** | OS-tiered self-editing runtime | Long-horizon agents | Runtime lock-in; tool-loop cost | Sleep-time maintenance; remain a drop-in layer |
| **MemPalace** | Local verbatim recall | Local-first; KG MCP; backends | Verbatim ≠ engineering lessons | `StorageBackend` ABC; keep typed schema + ladder |
| **Hindsight** | TEMPR multi-channel + Reflect | Multi-strategy + rerank; BEAM claims | Postgres-centric; generalist | Graph-hop channel + optional rerank; keep SQLite |
| **Cognee** | ECL → dense semantic graph | Connectors; many retrieval modes | Heavy LLM ingest | Optional connectors; don't densify everything |
| **OpenMemory** | Multi-sector cognitive memory | Explainable traces; importers | Less coding-native | Score breakdown + Mem0/Zep importers |
| **LangMem** | LangGraph-native memory | Zero-friction in LC | Framework lock-in; poor latency reports | Thin optional adapter only |
| **SuperMemory** | Personal KM / browser | Human UX | Not agent-native | Out of scope; optional import |
| **Mastra** | TS agent framework + OM | Published framework numbers | Cloud LLM OM layer | Honest metric footnotes |
| **Odysseus** | AI workspace + provider ABC | `MemoryProvider` abstraction | Not a pure memory product | Native provider protocol |
| **brainctl** | Maximal cognitive MCP surface | Feature breadth | 200+ tools = token tax | **Anti-pattern** — ≤15–20 MCP tools |
| **Hmem / cursor-brain** | Minimal SQLite FTS | Simple & fast | No lifecycle / ranking / ladder | Engram already supersedes |

## Where Engram leads (defend)

1. **Typed engineering schema** — mistakes, patterns, skills, prompts (not chat blobs).
2. **Action Ladder + human-approved reflexes** — memory → cheap deterministic action.
3. **Propose-don't-decide** inbox / monitors trust model.
4. **IDE lifecycle depth** — recall, guard, checkpoint, PreCompact hooks (Cursor / Claude / Antigravity).
5. **FTS-primary hybrid** — correct for code identifiers (`getConnection`, error strings).
6. **Measured ranking** — fit harness, FSRS, feedback, instrument gates, fit-blindness warnings.
7. **Metric honesty** — retrieval R@5 published separately from vendor QA scores.

## Metric footnotes

| Claim type | Example | Comparable to Engram? |
|------------|---------|------------------------|
| Retrieval R@k | “correct session in top-5” | Yes — Engram LongMemEval oracle R@5 ≈ 0.538 |
| QA / LLM-as-judge | Mem0 LoCoMo ~67% | **No** — different pipeline |
| Home-domain engineering | Engram seeded R@5 = 1.00; EEME | Engram-defined niche bench |

## Soft limits & non-goals

- Soft limit ~50k items on single-file SQLite (see ADR-0001).
- Not a full agent runtime (Letta).
- Not a 200-tool cognitive kitchen sink (brainctl).
- Not browser PKM (SuperMemory).
- Never auto-deletes user knowledge.

## Related

- [ADR-0009: SOTA scope fence](decisions/0009-sota-scope-fence.md)
- [Migrate from Mem0 / Zep / OpenMemory](MIGRATION_FROM_COMPETITORS.md)
- [RAGE audit (historical)](RAGE_AUDIT.md)
- [July 2026 comparative review](COMPARATIVE_REVIEW_2026-07.md)
