# Router-Driven Preflight Design

**Date:** 2026-04-16
**Branch:** `feat/r4-validator-correctness`
**Predecessor:** `1af9bd7` (USE-WHEN preflight wording rewrite)

## Goal

Make preflight a deterministic harness side-effect of routing instead of an LLM-callable tool. After the router decides a category, the harness dispatches the matching `preflight_*` call directly and injects the result as a user message before the main loop. The model never opts in.

## Motivation

In bench `1af9bd7_usewhen_p3i6_gpt54_prod_runs1.json` (104 tasks, gpt-5.4):

- **0/104** invocations of `preflight_finance` / `preflight_entity` / `preflight_project` / `preflight_inbox` / `preflight_doc_migration` at LLM-step level.
- Pass rate **100/104** (server-confirmed) — same as the no-preflight R4 baseline `8880fc8`.
- 3 of the 4 fails are exactly the task shapes the unused preflight tools were designed to solve:
  - **t001** ("the house AI thing" project lookup) — wrong project picked. `preflight_project` would have canonicalized.
  - **t022** ("take care of next inbox message") — over-refused with `OUTCOME_NONE_CLARIFICATION`. `preflight_inbox` would have surfaced an actionable entity→bill graph.
  - **t081** ("delete all 0.6 mm hardened nozzle purchases") — missed `black_library_terrain_spool`. `preflight_finance` vendor/item canonicalization would have caught it.

Two prior wording revisions ("you must call X" → "USE WHEN you need to…") moved adoption zero. The model treats preflight as optional and chooses the plain `tree+search+read` path it already knows. Forcing the call deterministically eliminates the opt-in problem.

## Architecture

```
prepass: tree → AGENTS.md → context → preflight_schema
   ↓
WORKSPACE SCHEMA injected as user message
   ↓
router.route(task_text) → RoutingDecision(category, skill_name, extracted)
   ↓
NEW: dispatch_routed_preflight(decision, schema) → optional ToolResult
   ↓ (if not None)
PREFLIGHT message injected as user message
   ↓
LLM main loop (preflight_* tools removed from function schema)
```

### Category → preflight dispatch table

| Category                  | Preflight tool             | Args                                                |
|---------------------------|----------------------------|-----------------------------------------------------|
| `FINANCE_LOOKUP`          | `preflight_finance`        | `query`, `finance_roots`, `entities_root`           |
| `BILL_QUERY`              | `preflight_finance`        | `query`, `finance_roots`, `entities_root`           |
| `entity_message_lookup`   | `preflight_entity`         | `query`, `entities_root`                            |
| `project_involvement`     | `preflight_project`        | `query`, `projects_root`, `entities_root`           |
| `document_migration`      | `preflight_doc_migration`  | `query`, `source_paths`, `entities_root`            |
| `INBOX` *(future)*        | `preflight_inbox`          | `inbox_root`, `entities_root`, `finance_roots`      |
| anything else / `UNKNOWN` | none                       | —                                                   |

The mapping lives next to the skill files: each routed skill's frontmatter declares which preflight (if any) the harness should run. This keeps the skill ↔ preflight binding co-located with the skill body. Concretely, frontmatter gains:

```yaml
preflight: preflight_finance         # optional — omit for "no preflight"
preflight_query_field: query         # which extracted field carries the query string
```

Loaded into `BitgnSkill` and consumed by the new `dispatch_routed_preflight` helper.

### Query extraction

Three sources, in priority order:

1. **`decision.extracted[query_field]`** — populated by tier1 regex named groups OR tier2 classifier `extracted` dict. The classifier system prompt is extended to populate `{"extracted": {"query": "<vendor or item or person reference>"}}` whenever the chosen category needs one.
2. **Raw task text** as fallback. Preflight canonicalizers already do fuzzy matching; a noisy query that returns nothing is no worse than skipping the call.
3. **`preflight_inbox` needs no query** — purely root-driven.

If a category requires a query and neither source produces a non-empty string, skip the call (treat as no-preflight).

### Roots discovery

The prepass `preflight_schema` result already returns the workspace roots as a JSON-ish summary. We need to parse it into a typed structure once and pass into both:

- The bootstrap injection text (already done at `pcm.py:267-273`).
- The new `dispatch_routed_preflight` call (so it can populate `finance_roots` etc. as keyword args).

Concretely, change `run_prepass` to return both `bootstrap_content: list[str]` AND `schema: WorkspaceSchema | None`. `WorkspaceSchema` is a small dataclass: `inbox_root`, `entities_root`, `finance_roots`, `projects_root`. Source-of-truth is the `preflight_schema` summary parsed once.

### Result injection

When dispatch returns a successful `ToolResult`, inject:

```
PREFLIGHT (auto-dispatched by router for category=<X>, query=<Y>):
<result.content>

This is the canonical narrowing for this task. Use these references first; broader search only if the answer is not derivable from them.
```

When it returns no result (skip / failure), inject nothing — the agent runs as it does today, modulo the function-schema change.

### Function schema change

Remove all six preflight types from `FunctionUnion` in `schemas.py`:

- `Req_PreflightSchema`
- `Req_PreflightInbox`
- `Req_PreflightFinance`
- `Req_PreflightEntity`
- `Req_PreflightProject`
- `Req_PreflightDocMigration`

The Pydantic classes remain (the harness still constructs them for `dispatch`), they just leave the LLM-visible function-calling discriminated union. The model can no longer attempt these calls; the harness invokes them itself in prepass + post-router.

Skill bodies stop instructing the model to call preflight; the `PREFLIGHT_PROTOCOL` block in `prompts.py` is deleted entirely.

### Skill body changes

For each of the 5 routed skills, replace the "Step 0: Workspace exploration shortcut" section with a one-liner:

```
## Step 0: Pre-fetched context

A `PREFLIGHT` message above contains the canonical narrowing for this task — the matching record(s), entity canonicalization, or destination resolution. Treat it as ground truth and start from those references. Fall through to the search strategy below only if preflight returned nothing usable.
```

If preflight wasn't dispatched (skill has no `preflight:` frontmatter, or query missing), the model just runs the search strategy below — no degradation vs. today.

### Trace + arch logging

Append a new prepass-style record per dispatch so `arch_report.py` can analyze adoption:

```python
trace_writer.append_prepass(
    cmd=f"routed_{tool_name}",   # e.g. "routed_preflight_finance"
    ok=result.ok,
    bytes=result.bytes,
    wall_ms=result.wall_ms,
    error=result.error,
    error_code=result.error_code,
)
```

Plus an arch event `ROUTED_PREFLIGHT` with `(category, tool, query_source, ok)` so we can read adoption + cost from the standard arch view.

## Failure-mode analysis

| Scenario | Behavior |
|----------|----------|
| Router returns UNKNOWN | No preflight dispatched. Model runs as today. |
| Routed skill has no `preflight:` frontmatter | No preflight dispatched. Model runs with skill body alone. |
| Query field empty/missing | Skip dispatch (don't pass empty string). Model runs without preflight injection. |
| Preflight call raises | Caught + traced; model runs without injection. Never crashes the task. |
| Preflight returns empty data | Inject summary anyway (it may say "no match found", which is signal). Model can choose to widen search. |
| Preflight returns wrong record | Same risk class as today's WORKSPACE SCHEMA misdirection. Skill body's fall-through search clause covers it. |

## Testing

- **Unit:** `dispatch_routed_preflight` table tests — every (category → tool) row, plus skip cases.
- **Unit:** `WorkspaceSchema.parse(preflight_schema_summary)` round-trip.
- **Unit:** Updated router classifier system prompt asks for `extracted.query` and tests confirm parser keeps it on `decision.extracted`.
- **Integration:** Agent loop test — router hits FINANCE_LOOKUP, dispatch fires `preflight_finance`, message injected before first LLM step. Use a fake adapter that records dispatched preflights.
- **Integration:** Negative test — UNKNOWN category, no preflight injected.
- **Integration:** Negative test — query missing for required category, no preflight injected.
- **Bench:** Full PROD p3i6 with `--runs 1`. Compare server scores task-by-task against `1af9bd7` baseline (100/104 fails t001/t009/t022/t081). Specific predictions:
  - t001 should pass (or fail with a different mode).
  - t022 should pass (preflight_inbox → not dispatched without INBOX category; if no INBOX skill exists yet, t022 outcome unchanged — record this gap).
  - t081 should pass.
  - t009 (TCP reset) unrelated; expect re-roll.

## Out of scope

- **Adding an INBOX skill** for t022 — separate work. The architecture supports it; landing the skill is a follow-up.
- **Reactive preflight re-invocation mid-task** — today's reactive_router could trigger a re-dispatch on dead-end search, but not this PR.
- **Preflight result caching across runs** — every task pays one preflight call; that's fine at p3i6.
- **Multi-preflight per task** — one category, one preflight. Inbox tasks that span finance + projects can use `preflight_inbox` which already does multi-graph traversal.

## Risk vs. reward

**Reward:** Adoption goes 0% → ~80%+ (every routed task with a query gets preflight). Three of four current fails are in preflight-shaped categories — best-case +3 net pass rate (100 → 103/104).

**Risk:** 5 categories × 1 extra LLM-equivalent dispatch per task = +0–500ms latency per routed task. Server cost ~unchanged (preflight tools were already counted in budget; they just weren't getting called). If preflight returns wrong record consistently for some task family, that family regresses — mitigated by the skill body's "fall through to search" clause and the `decision.extracted.query` quality.

**Reversibility:** Single config flag `BITGN_ROUTED_PREFLIGHT_ENABLED` (default `1`) gates the dispatch. Set to `0` and the agent reverts to today's behavior modulo the function-schema removal (which is the only irreversible part — and we're removing tools the model never used).
