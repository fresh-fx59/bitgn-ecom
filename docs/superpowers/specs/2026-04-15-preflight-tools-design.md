# Preflight Tools — Hard-Gated Workspace Discovery

**Date:** 2026-04-15
**Status:** Design approved, awaiting user review
**Motivation:** Bench #2 (fcb9f3e, 96/104) failed 8 tasks. Five failures share a common shape: the agent acted on the task without first enumerating the workspace structure or the entity→artifact graph. t016/t066/t091 wrote OCR frontmatter to one bill when three bills existed for the referenced entity. t041 surrendered after a single finance search came back empty. t051 reported clarification-needed without canonicalizing the project name. t096 blundered through outbox validation. The common fix is forcing a structured discovery step before action.

## Goal

Introduce a **preflight phase** in the agent loop: before the agent issues any read/search/mutation beyond a small whitelist, it must call a `preflight_*` tool appropriate to the task. Tools remain callable throughout the loop so the agent can re-consult them when a search dead-ends.

## Non-goals

- No embeddings, RAG, or semantic search. The underlying data is YAML frontmatter with exact-match keys; the problem is graph traversal, not retrieval.
- No hardcoded paths. Every workspace root is discovered at runtime and passed as a parameter.
- No changes to the router or the existing bitgn skill set in this iteration. Preflight is additive.
- No attempt to fix the PROD task-content rotation behavior or rate-limit errors — orthogonal concerns.

## Tool Set

Six tools. One discovery tool plus five specialized preflights.

### `discover_workspace_schema() -> {summary, data}`

Blind-crawls the workspace, identifies roots by frontmatter signatures and directory content, returns a `WorkspaceSchema`. Cached per task (discovery is deterministic within a benchmark run; safe to memoize).

**Role detection signatures:**
- `entities_root`: dirs containing files with frontmatter keys `aliases`, `role`, `relationship`
- `inbox_root`: dirs containing files with frontmatter `inbox_*` keys or names matching inbox patterns
- `finance_roots[]`: dirs with files carrying `eur_*`, `vendor`, `line_items` frontmatter
- `projects_root`: dirs with files carrying `project` / `start_date` / `members`
- `outbox_root`: dirs with files carrying `to`, `subject`, `body` (email frontmatter)
- `rulebook_root`, `workflows_root`, `schemas_root`: located via content heuristics (md with `# Rules`, `# Workflow`, etc.)

**Return shape:**
```json
{
  "summary": "Workspace has 1 inbox, 2 finance roots, 1 entities root, 1 projects root, 1 outbox root.",
  "data": {
    "inbox_root": "00_inbox",
    "entities_root": "20_entities",
    "finance_roots": ["50_finance/purchases", "50_finance/invoices"],
    "projects_root": "30_projects",
    "outbox_root": "60_outbox/outbox",
    "rulebook_root": "10_rulebook",
    "workflows_root": "11_workflows",
    "schemas_root": "12_schemas"
  }
}
```

### `preflight_inbox(inbox_root, entities_root, finance_roots) -> {summary, data}`

Enumerates open inbox items. For each item, parses its body to identify the referenced entity, then walks the entity's frontmatter (canonical name, aliases, any `bills`/`invoices`/`purchases` reference lists) and joins against `finance_roots` to produce the full bill list for that entity.

**Return shape:**
```json
{
  "summary": "3 open inbox items. Item #1 references entity 'Juniper' → 3 bills in finance. Item #2 references 'Hearthline Sensor Bundle' → 1 bill. Item #3 references 'Foundry' → 0 bills (check aliases).",
  "data": {
    "items": [
      {
        "path": "00_inbox/2026_04_01__juniper_ocr.md",
        "task_type": "ocr_verification",
        "entity_ref": "Juniper",
        "entity_canonical": "Juniper Systems",
        "related_finance_files": [
          "50_finance/purchases/2026_01_02__eur_000050__bill__hearthline.md",
          "50_finance/purchases/2026_02_14__eur_000105__bill__house_mesh_juniper_ssd.md",
          "50_finance/purchases/2026_03_11__eur_000029__bill__repair_ledger_filter.md"
        ]
      }
    ]
  }
}
```

**Failure modes it catches:**
- t016/t066/t091: agent now sees all 3 bills before OCRing.
- t041: agent sees 0 bills → can decide clarify vs. alias-retry instead of surrendering blindly.

### `preflight_finance(finance_roots, entities_root, query) -> {summary, data}`

Canonicalizes `query` against entity aliases and vendor names, then enumerates matching purchase/invoice files across all finance roots. Returns file list with pre-extracted `vendor`, `date`, `total`, `line_items` for each.

**Return shape:**
```json
{
  "summary": "Query 'walking buddy' → entity 'Harbor Body' (1 match via aliases). 2 finance files reference this entity.",
  "data": {
    "canonical_entity": "Harbor Body",
    "aliases_matched": ["walking buddy"],
    "finance_files": [
      {"path": "...", "vendor": "...", "date": "...", "total": "...", "line_items": [...]}
    ]
  }
}
```

**Failure modes it catches:**
- t003: canonicalizes "walking buddy" upfront, surfaces both matches (Harbor Body, House Mesh expected — t003 returned Harbor Body + Reading Spine, wrong second match).
- t030/t055: sanity-checks vendor/line-item/date filter before the agent commits to a number.

### `preflight_entity(entities_root, query) -> {summary, data}`

Pure entity-disambiguation tool. Searches entity files by canonical name, aliases, and role fields. Returns matching entities with full frontmatter.

**Return shape:**
```json
{
  "summary": "Query 'health baseline project' → 0 direct matches. Closest: entity 'Health Baseline' (role: project). Canonical project name appears to be 'Health Baseline'.",
  "data": {
    "direct_matches": [],
    "alias_matches": [{"name": "Health Baseline", "role": "project", "frontmatter": {...}}]
  }
}
```

**Failure modes it catches:**
- t051: catches "health baseline project" → "Health Baseline" canonicalization, agent finds the record instead of reporting clarification-needed.

### `preflight_project(projects_root, entities_root, query) -> {summary, data}`

Looks up project records and the entities involved. Returns project frontmatter (start_date, members, status) plus the set of people/orgs on the project.

**Return shape:**
```json
{
  "summary": "Project 'Health Baseline' found. Start date: 2025-11-14. 3 members involved.",
  "data": {
    "project": {"name": "Health Baseline", "start_date": "2025-11-14", "members": [...]},
    "involved_entities": [{"name": "...", "role": "..."}]
  }
}
```

### `preflight_doc_migration(source_paths, entities_root, query) -> {summary, data}`

For document migration tasks (t092-class). Resolves target destination root from `query` (entity name or area name), checks for collisions with existing files at destination, returns the resolved target paths.

**Return shape:**
```json
{
  "summary": "Target 'NORA' → entity 'NORA Rees' (alias match). Destination root: 20_entities/nora_rees/. 5 source files, 0 collisions.",
  "data": {
    "target_canonical": "NORA Rees",
    "destination_root": "20_entities/nora_rees/",
    "migrations": [
      {"source": "path/a.md", "destination": "20_entities/nora_rees/a.md", "collision": false}
    ]
  }
}
```

## Enforcement

**Harness-level hard gate.** The agent loop inspects each tool call before dispatch. Gate logic:

```
WHITELIST = {"discover_workspace_schema", "list_dir", "get_task_metadata"}

if tool_name not in WHITELIST and tool_name not in PREFLIGHT_TOOLS:
    if not any(call.tool in PREFLIGHT_TOOLS for call in trace.prior_calls):
        reject with message:
          "Preflight required. Call discover_workspace_schema first, then
           the preflight_* tool(s) matching your task (preflight_inbox,
           preflight_finance, preflight_entity, preflight_project,
           preflight_doc_migration). Preflight tools remain callable at any
           point in the loop."
```

- Fires on first non-whitelisted action (mutation OR non-trivial read/search)
- Rejection does not consume a step toward the step-budget — the agent retries with preflight
- Tools remain callable throughout the loop (not just at the start)

**Not a validator rule.** The harness gate is deterministic and free; the validator LLM is not involved in the gate decision.

## Prompt Changes

System-level instruction appended to the agent system prompt:

> **Preflight protocol.** Before reading, searching, or writing anything beyond the workspace root listing, you must:
> 1. Call `discover_workspace_schema` to learn the workspace layout.
> 2. Call whichever `preflight_*` tool(s) match your task shape. For inbox/OCR tasks call `preflight_inbox`. For finance lookups call `preflight_finance`. For entity/person/project questions call the corresponding tool. You may call multiple preflight tools if the task spans areas.
> 3. Only then act on the task.
>
> Preflight tools return a short `summary` and structured `data`. The summary tells you what to expect; the data lets you cross-reference. Preflight tools remain callable throughout the loop — re-invoke them if a later search comes back empty or a graph traversal dead-ends.

## File Structure

```
src/bitgn_contest_agent/
├── preflight/
│   ├── __init__.py
│   ├── schema.py              # WorkspaceSchema dataclass + discover_workspace_schema
│   ├── inbox.py               # preflight_inbox
│   ├── finance.py             # preflight_finance
│   ├── entity.py              # preflight_entity
│   ├── project.py             # preflight_project
│   ├── doc_migration.py       # preflight_doc_migration
│   ├── canonicalize.py        # shared alias/entity canonicalization helpers
│   └── response.py            # {summary, data} response builder
├── agent.py                   # MODIFIED — register preflight tools
├── harness/
│   └── gate.py                # NEW — harness-level preflight gate
├── prompts/
│   └── system.py              # MODIFIED — append preflight protocol instruction
└── config.py                  # MODIFIED — register new tool list

tests/
├── preflight/
│   ├── test_schema.py
│   ├── test_inbox.py
│   ├── test_finance.py
│   ├── test_entity.py
│   ├── test_project.py
│   ├── test_doc_migration.py
│   └── test_canonicalize.py
└── harness/
    └── test_gate.py
```

Each preflight module stays focused on one task shape. `canonicalize.py` holds shared name-matching logic (entity aliases, vendor aliases, case/whitespace normalization). `response.py` keeps the `{summary, data}` shape consistent across all six tools.

## Data Flow

```
Task arrives
    ↓
Agent system prompt contains preflight protocol instruction
    ↓
Agent calls discover_workspace_schema
    ↓ (returns WorkspaceSchema with all roots)
Agent calls preflight_<task_type>(roots..., query)
    ↓ (returns {summary, data} with canonicalized entities + artifact graph)
Harness gate: preflight observed ✓
    ↓
Agent proceeds with normal tools (read_file, search, write, ...)
    ↓
Agent can re-call preflight_* at any later step
    ↓
report_completion
```

## Error Handling

- `discover_workspace_schema` on a malformed workspace: returns partial schema with `errors[]` field; agent sees this in `summary` and can still proceed.
- `preflight_*` with no matches: returns empty `data` with `summary` explaining why (e.g., "Query 'Foundry' → 0 entity matches. Aliases searched: [...]. Consider broader query or clarification."). Agent decides next action.
- `preflight_*` query that resolves to multiple candidates: returns all candidates with ranking in `data`; agent picks or asks for clarification.
- Harness gate hit on a genuinely preflight-incompatible task: mitigated by the broad whitelist; if we see false positives, the whitelist can grow. Gate rejection is non-fatal (no step cost).

## Testing Strategy

**Unit tests per preflight module:**
- Happy path: known workspace fixture, known query, assert canonical output.
- Alias resolution: query uses an alias, assert canonical entity returned.
- Zero matches: assert empty-data response with explanatory summary.
- Multi-match: assert all candidates returned with ranking.

**Integration tests (`tests/harness/test_gate.py`):**
- Agent calls `read_file` first → gate rejects, agent called again.
- Agent calls `discover_workspace_schema` then `preflight_inbox` then `read_file` → passes.
- Agent calls preflight late in trace → gate still passes (once preflight observed, all subsequent calls allowed).

**Bench validation:**
- Run p10i15 on PROD after implementation.
- Expect t016/t041/t066/t091 to pass. t051 depends on project-record presence in the task rotation; improvement is probabilistic.
- Regression watch: tasks currently passing should not break. Harness gate adds one extra tool call at the start of each task (discover_workspace_schema is cached) — minimal latency impact.

## Rollout

Single branch (`feat/preflight-tools`), single PR. Landing criteria:
- All unit + integration tests pass
- p10i15 PROD run shows net improvement (baseline: 96/104 at fcb9f3e)
- No new REJECT reasons in arch traces beyond existing ones

## Open Risks

1. **Gate too aggressive.** If the whitelist is wrong and the gate fires on tasks that genuinely don't need preflight, we'll see step-budget waste on retries. Mitigation: conservative whitelist + easy to extend; first bench run will reveal false positives.
2. **Preflight latency.** Each preflight tool reads frontmatter from many files. Mitigation: memoize within-task; `discover_workspace_schema` runs once per task.
3. **LLM picks wrong preflight tool.** If agent calls `preflight_finance` for an inbox task, the gate passes but the data is useless. Mitigation: each preflight tool's `summary` field is honest ("no finance data relevant to 'Handle inbox item'"), prompting agent to re-preflight correctly.
4. **Canonicalization gaps.** First-version alias matcher won't cover every naming convention. Mitigation: start narrow (exact + case-insensitive + obvious alias fields), broaden iteratively based on bench failures.
