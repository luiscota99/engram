# RESEARCH HANDOFF — Estado del arte en agent memory (2026-07-21)

Para el agente que trabaja en engram. Misión: investigar a fondo el landscape de
memoria para agentes (proyectos abajo), extraer qué vale la pena adoptar, y proponer
mejoras — como **decisiones en el inbox de engram**, nunca como cambios directos
(nuestro propio trust model aplica a esta investigación).

## El norte (del autor, léelo antes de investigar)

1. **Engram no necesita pagar.** Cero presión de monetización. No adoptar nada cuyo
   único mérito sea "es lo que hace un SaaS".
2. **La métrica #1 es: el mejor asistente para Luis.** Toda propuesta se evalúa por
   valor-para-el-usuario-único, no valor-para-el-mercado.
3. **La adopción sí importa, pero como loop de feedback**: más usuarios = más
   conejillos de indias que estresan y mejoran la idea. Eso hace valiosa la
   *fricción-a-primer-valor* (onboarding, docs, instalación) — pero nunca a costa
   de la misión #2. Sin prisa: si nadie llega, el proyecto sigue completo.
4. **Invariantes no negociables:** local-first (SQLite, sin dependencia cloud),
   measured-not-vibes (toda idea adoptada entra con hipótesis de benchmark y se
   valida contra evals antes de quedarse), gobernanza fail-closed (propuestas nunca
   auto-ejecutan; reflexes con aprobación humana y hash-pinning).

## Contexto de mercado (verificado 2026-07)

La queja #1 de devs con coding agents en 2026 ya no es calidad de código — es
**amnesia**. El espacio de memory layers está caliente y financiado, pero todos los
jugadores grandes son SaaS B2B para app developers. Nadie productiza el paquete
completo de engram: memoria de ingeniería tipada + action ladder con gobernanza +
ROI medido + local-first. Ese es el territorio; la investigación es para reforzarlo,
no para abandonarlo.

## Proyectos a investigar (con preguntas específicas)

### 1. Mem0 — https://mem0.ai (y github)
~47k stars, ~90k devs. Memoria bolt-on: vector + graph + KV.
- ¿Cómo es su pipeline de extracción de memorias (write-time)? Comparar con nuestro
  auto-extract y dedup at write. ¿Qué hacen mejor?
- SDK ergonomics y onboarding: ¿por qué 90k devs? ¿Qué fricción-a-primer-valor
  tienen que nosotros no? (Relevante para el objetivo conejillos-de-indias.)
- Su "State of AI Agent Memory 2026" (mem0.ai/blog/state-of-ai-agent-memory-2026):
  ¿qué benchmarks reportan y con qué metodología? ¿Comparable con la nuestra?
- Graph memory de Mem0: ¿aporta algo sobre nuestras typed relations (v27 bi-temporal
  + provenance edges)?

### 2. Zep / Graphiti — temporal knowledge graph
63.8% LongMemEval **con GPT-4o como motor** (nosotros: 0.538 session-level R@5
totalmente local, embedder 274MB, sin LLM — clases de recursos distintas).
- Estudiar Graphiti a fondo: invalidación temporal de hechos, edges con vigencia,
  resolución de contradicciones. Comparar contra nuestro temporal facts + memoria
  bi-temporal v27. ¿Qué les robamos?
- Reproducir su setup de LongMemEval para tener una comparación apples-to-apples
  (mismo eval, nuestro stack local vs nuestro stack + LLM opcional). Publicar el
  resultado con caveats honestos — es asset de credibilidad.

### 3. Letta (ex-MemGPT) — https://letta.com
$10M seed, $70M val. Arquitectura OS-inspired: core memory siempre-en-contexto
(RAM) / recall (cache) / archival (disco). "El agente ES su memoria."
- Comparar su core-memory-siempre-visible contra nuestro pinned + recall hook.
  ¿Nos falta un tier "core" explícito que viaje en cada prompt? ¿Qué costo/beneficio
  en tokens tendría (medirlo con ladder_cost)?
- Letta tiene sleep-time agents; nosotros tenemos `engram sleep`. Comparar qué hace
  cada uno en el ciclo de consolidación. ¿Ideas para nuestro sleep/gc/consolidate?
- Su runtime-céntrico vs nuestro tool-céntrico (MCP/CLI): confirmar que nuestra
  elección sigue siendo correcta para multi-cliente (Claude Code, Cursor,
  Antigravity) — la portabilidad es moat nuestro.

### 4. CODESKILL (arXiv 2605.25430) — self-evolving skills para coding agents
Lo académico más cercano a nuestro skills/promotion/consolidation.
- ¿Cuál es su loop de adquisición/refinamiento de skills? ¿Qué señales usan para
  promover/demover? Comparar con nuestro proven-uses → promote → reflex.
- ¿Cómo evalúan que un skill ayuda? Robar metodología de eval si es mejor que la
  nuestra.

### 5. Always-On Agents survey (arXiv 2606.30306) — memoria, estado y gobernanza
- Leer completo, con foco en la sección de gobernanza: posicionar nuestro trust
  model (inbox, decide, hash-pinning, fail-closed) contra lo que el survey lista
  como problemas abiertos. Documentar dónde vamos adelante (es real) y dónde no.
- Minar su bibliografía: listar 3-5 papers/proyectos que no conozcamos y valgan
  segunda pasada.

### 6. El folklore DIY + memoria nativa de los labs
Markdown-file patterns, Claude Code auto-memory, Cursor Memories, reglas <500 líneas.
- Interop como estrategia de adopción: ya existe `import-claude-memories` — evaluar
  bridges bidireccionales (export a CLAUDE.md/Cursor rules, import de ambos). El
  pitch natural de engram no es "reemplaza tu memoria nativa" sino "es la capa
  durable y medida debajo de todas tus herramientas".
- La lección del folklore (memoria naive se pudre; archivos de >500 líneas dañan;
  memoria vieja sin update es peor que nada) valida nuestra obsesión con curación
  (decay FSRS, gc, dedup, invalidate). Documentar esa validación para el README.

## Formato del deliverable

Un reporte (`docs/research/landscape-2026-07.md`) con:
1. **Findings por proyecto** (qué hacen, qué hacen mejor que nosotros, evidencia).
2. **Steal-list rankeada por valor-para-Luis** — cada item con: qué es, por qué
   mejora al asistente personal, hipótesis de benchmark para validarlo, costo
   estimado de implementación.
3. **Reject-list explícita** — qué NO copiar y por qué (especialmente features cuyo
   mérito es SaaS/growth, o que violen los invariantes).
4. **Propuestas filed como decisiones en el inbox de engram** (`memory_propose_decision`)
   — una por mejora concreta, para que el autor apruebe con `engram decide`.
5. **Quick wins de onboarding** (si los hay) para bajar fricción-a-primer-valor,
   al servicio del loop de conejillos de indias.

Regla final: toda afirmación sobre un proyecto externo va con fuente (URL/paper) y
fecha. Nada de vibes — este repo tiene reputación de medir.
