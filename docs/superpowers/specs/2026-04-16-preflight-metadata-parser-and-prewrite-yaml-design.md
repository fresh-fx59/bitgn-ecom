# Preflight Metadata Parser + Pre-Write YAML Validation — Design

**Date**: 2026-04-16
**Branch**: feat/step-validator (continuation)
**Context**: PROD p3i6 bench (commit 5cc0ad1) landed 102/104 (98.1%) vs baseline 100/104. Two residual failures — t001 (`project_start_date`) and t071 (`inbox_en` resend) — both have root causes unrelated to the agent's reasoning quality. Log analysis shows these are infrastructure bugs in the preflight stack and the write path.

---

## Goals

1. Make `preflight_project` produce useful output on PAC1 PROD workspaces (currently returns "no match" for every invocation).
2. Stop the grader from flagging duplicate writes that arise from post-write YAML format validation.
3. Make the effectiveness of these fixes directly observable in bench trace JSONL.

## Non-goals

- Lane-specific post-write schema checks (stays as-is).
- New skills, new routing rules, new prompt engineering.
- Grader-side tolerance changes (we don't own the grader).
- `preflight_entity` / `preflight_finance` metadata rework (same parser will become available to them but their call sites are out of scope).

---

## Bug 1 root cause chain (t001 `project_start_date`)

Five compounding failures, each masking the next:

1. **No PROD metadata parser.** `preflight/schema.py:73 _parse_frontmatter` matches only `^---\n...\n---\n` YAML blocks. PAC1 PROD records use markdown bullet lists (`- record_type: project`) or ASCII tables (`| record_type | project |`). Parser returns `{}` for every PROD file.
2. **Classifier falls back to substring matching.** `_classify_dir` returns `[]` (no frontmatter). `_classify_dir_by_content` greps raw text with predicates like `"project" in c and ("- members:" in c or "- start_date:" in c)`. Invoice line_items contain the substring "project" and invoices have dates → `50_finance/invoices` classified as BOTH finance AND projects.
3. **`projects_root` misdetected.** `schema.projects_root = "50_finance/invoices"` instead of `"40_projects"`.
4. **`preflight_project` can't find anything.** With the wrong root it lists 11 invoices, reads each, calls `fm.get("project", "")` — always empty because no frontmatter. Even with correct root it would fail: projects live at `<projects_root>/<slug>/README.MD` and the code does a flat `list` filtered to `.md` entries (subdirs rejected).
5. **Skill falls through to agent.** Agent boots (tree, AGENTS.MD, context) and short-circuits via the AGENTS.MD rule "40_projects folder prefix YYYY_MM_DD is canonical project start date". Derives `2026-04-21` from the folder name — correct value, wrong source. Grader rejects: `"answer missing required reference '40_projects/2026_04_21_studio_parts_library/README.MD'"`.

Adjacent passing task t051 (same template) followed the same broken chain but the agent voluntarily read the README at steps 6-7. Pure luck.

## Bug 2 root cause chain (t071 `inbox_en` duplicate write)

1. Step 19: agent writes outbox file with unquoted colon in `subject:` field. Call succeeds (PCM doesn't validate YAML).
2. Format validator fires **after** persistence, emits `FORMAT_VALIDATOR` arch event. Agent receives error feedback.
3. Step 20: agent rewrites same path with quoted subject. Second mutation to identical path now present in bench state.
4. Grader flags `"unexpected file write '60_outbox/outbox/eml_2026-03-31T16-32-00Z.md'"` — duplicate writes violate mutation policy.
5. Enforcer separately caught it: `"mutation integrity: agent claimed a single write, actual shows file was written twice"`.

Sibling tasks t021, t047 passed with the same filename convention — the only difference is those agents happened to quote their subjects correctly on the first attempt.

---

## Design

### Fix X — unified metadata parser

**Location**: `src/bitgn_contest_agent/preflight/schema.py`

**New function**: `parse_record_metadata(text: str) -> dict[str, str]`

Handles three encodings:

1. **YAML frontmatter** — existing behavior, `^---\n(.*?)\n---\n`.
2. **Markdown bullet list** — leading `- key: value` lines at the top of the file, scanned until the first non-matching line.
3. **ASCII table** — `| key | value |` rows; the header row is skipped by detecting `| --- | --- |` separator; keys and values are stripped of whitespace and pipe chars.

Scan order: YAML first (cheap regex), then bullet list, then ASCII table. Return the first non-empty dict. Keys are lowercased. Multi-value fields (members, aliases, line_items) return the raw value string — callers split on `\n` or `, ` as needed. Unknown shapes return `{}`.

**Deletions**:
- `_parse_frontmatter` becomes a thin alias for `parse_record_metadata` (kept for backward compatibility of existing tests and the legacy filesystem-based code path).
- `_classify_dir_by_content` and its fraction heuristics → deleted. Single-mode classification only.

### Fix A — classifier uses `record_type`

**Location**: `src/bitgn_contest_agent/preflight/schema.py:_classify_dir`

Simplified predicate: inspect `md.get("record_type")` on each parsed record.

| record_type value | role |
|---|---|
| `project` | projects |
| `invoice`, `bill`, `receipt`, `purchase` | finance |
| `inbound_email`, `inbox` | inbox |
| `outbound_email`, `outbox` | outbox |
| `person`, `entity`, `cast` | entities |

Threshold stays at 30% — a directory needs ≥30% of its `.md` files to match a record_type before we commit to that role. Mixed-purpose dirs (e.g. a legacy shared folder) still get skipped safely.

**Deletion**: `_classify_dir_by_content` + loose substring predicates (`"project" in c and ...`).

### Fix Z — `preflight_project` subdir recursion

**Location**: `src/bitgn_contest_agent/preflight/project.py:run_preflight_project`

Projects in PAC1 PROD live at `<projects_root>/<slug>/README.MD` (note uppercase). Current code does flat list of the root; subdirectory entries are filtered out by the `.md` check.

**New behavior**:
1. List `req.projects_root` entries.
2. For each `e.is_dir=True` entry, read `<projects_root>/<e.name>/README.MD` AND `<projects_root>/<e.name>/README.md`. Whichever exists wins; both missing → skip.
3. For each `e.is_dir=False` entry ending in `.md`, read directly (covers the DEV workspace layout where project records are flat `.md` files).
4. Parse metadata via `parse_record_metadata`.
5. Match `md.get("project")` against the query using the existing normalize_name logic.

### Fix B — attribution refs on match

**Location**: `src/bitgn_contest_agent/preflight/project.py:run_preflight_project`

When `found`, return `refs=(found["file"],)`. The grader's attribution rule + the system prompt's grounding_refs tracking then pressure the agent to `read` the file before reporting completion.

### Fix C — non-leaky summary

**Location**: `src/bitgn_contest_agent/preflight/project.py:run_preflight_project`

Replace:
```python
summary = f"Project '{found['name']}' found. Start date: {found['start_date']}."
```
with:
```python
summary = f"Project '{found['name']}' found at {found['file']}."
```

The `data` payload still contains the full frontmatter; this is defense-in-depth against agents using the summary string as evidence.

### Fix E — pre-write YAML frontmatter validation

**Location**: `src/bitgn_contest_agent/adapter/pcm.py` (the write tool dispatcher)

Before calling `client.write(...)`:

```python
if content.startswith("---\n"):
    end = content.find("\n---\n", 4)
    if end != -1:
        block = content[4:end]
        try:
            yaml.safe_load(block)
        except yaml.YAMLError as exc:
            return ToolResult(
                ok=False,
                content="",
                refs=tuple(),
                error=f"YAML frontmatter parse error: {exc}",
                error_code="FORMAT_INVALID",
                wall_ms=0,
            )
```

On rejection: emit a `FORMAT_PRE_WRITE_REJECT` arch event (see observability) and return the failed ToolResult. The write is **not** dispatched to PCM; no mutation is recorded.

Post-write `FORMAT_VALIDATOR` arch events stay in place for non-YAML format issues and as a belt-and-suspenders check for YAML parsers that disagree with ours.

---

## Observability

### New `TracePrepass` fields

**Location**: `src/bitgn_contest_agent/trace_schema.py:TracePrepass`

Three optional fields, all default `None`:

- `schema_roots: dict[str, str | list[str]] | None` — populated on `cmd="preflight_schema"` records. Keys: `projects_root`, `finance_roots`, `entities_root`, `inbox_root`, `outbox_root`. Lets us grep one line per task and compare classifier output pre/post-fix.
- `match_found: bool | None` — populated on `cmd="routed_preflight_project"`, `cmd="routed_preflight_entity"`, `cmd="routed_preflight_finance"` records. Explicit signal of preflight success without byte-count heuristics.
- `match_file: str | None` — canonical file path of the match. Useful for spot-checking which README / entity record each task used.

### New arch category

**Location**: `src/bitgn_contest_agent/arch_constants.py`

Add to `ArchCategory` enum:
```python
FORMAT_PRE_WRITE_REJECT = "FORMAT_PRE_WRITE_REJECT"
```

Emitted once per pre-write YAML rejection with `details=f"path={path} error={yaml_error_message}"`.

### Post-run observation one-liners

```bash
# projects_root correctness across bench
jq -c 'select(.cmd=="preflight_schema") | .schema_roots.projects_root' artifacts/bench/*.jsonl | sort | uniq -c

# preflight_project success rate
jq -c 'select(.cmd=="routed_preflight_project") | .match_found' logs/*/t*.jsonl | sort | uniq -c

# pre-write rejects count
grep FORMAT_PRE_WRITE_REJECT logs/*/t*.jsonl | wc -l
```

No bench_summary.py changes in this spec — raw JSONL grep is sufficient for the first observation pass. Aggregate metrics can be added later if needed.

---

## Test strategy

### Parser tests (new)

`tests/preflight/test_metadata_parser.py`:
- Fixture: YAML frontmatter file → parser returns `{record_type: 'project', project: 'Foo', start_date: '2026-01-01'}`
- Fixture: bullet list file with same content → same dict
- Fixture: ASCII table file with same content → same dict
- Fixture: file with all three encodings mixed → first one wins (YAML > bullet > table)
- Fixture: file with no metadata → `{}`
- Fixture: file with malformed YAML but valid bullet list → falls through to bullet list

### Classifier tests (update existing)

`tests/preflight/test_schema.py`:
- PROD-shape fixture: bullet-list invoice files containing the word "project" in line_items → classified as `finance` only, never `projects`.
- PROD-shape fixture: bullet-list project file at `<slug>/README.MD` → parent dir classified as `projects`.
- PROD-shape fixture: entity records with `- record_type: person` → classified as `entities`.
- Delete tests that asserted the old `_classify_dir_by_content` behavior.

### preflight_project tests (update existing)

`tests/preflight/test_project.py`:
- Fixture: `<projects_root>/studio_parts_library/README.MD` with `record_type: project, project: Studio Parts Library, start_date: 2026-04-21` → `run_preflight_project(query="Studio Parts Library")` returns match with `refs=("<projects_root>/studio_parts_library/README.MD",)` and summary `"Project 'Studio Parts Library' found at ..."` (no date in summary).
- Fixture: flat `<projects_root>/*.md` layout (DEV) → still works.
- Fixture: query with no match → returns `"no project match"`, `refs=()`.

### Pre-write YAML validation tests (new)

`tests/adapter/test_pcm_write_validation.py`:
- Invalid YAML frontmatter → `ToolResult(ok=False, error_code="FORMAT_INVALID", error=...)` and `client.write` NOT called.
- Valid YAML frontmatter → dispatches to `client.write` as before.
- Content without frontmatter (`---` not at start) → dispatches to `client.write` without validation.
- Frontmatter with unquoted colon in value (the t071 regression) → rejected.

### Trace writer tests (update)

`tests/test_trace_writer.py`:
- `append_prepass` accepts optional `schema_roots`, `match_found`, `match_file`.
- Records serialize correctly with these fields set and with them absent.

---

## Rollout

1. Land all changes on `feat/step-validator` branch.
2. Run full test suite: `uv run pytest tests/ -x`.
3. Smoke test on PROD: 1 task (t001) to confirm classifier picks correct `projects_root`, preflight_project returns `match_found=true` with the README path in `refs`, agent reads the file, grader scores 1.0.
4. Smoke test on PROD: 1 task (t071 or a synthetic outbox task) to confirm pre-write rejection fires and no duplicate write occurs.
5. Full p3i6 PROD bench. Expected: 104/104 or very close; regression should be zero since the changes only affect code paths that were failing before.

## Risks

- **Metadata parser edge cases**: PROD workspace layout may contain encodings we haven't anticipated. Mitigation: parser returns `{}` on unknown shapes, which degrades gracefully to the "no match" path — same as today's behavior.
- **Classifier regression on DEV**: DEV workspaces use YAML frontmatter; new `record_type` check expects different values. Mitigation: DEV frontmatter uses the same `record_type` field names; if they don't, tests will catch it before PROD.
- **Pre-write YAML check false-positives**: PyYAML may be stricter than the format validator's parser. Mitigation: use the same YAML library (PyYAML) in both places; unit tests cover edge cases (colons, nested lists, quoted strings).
- **Grader unknowns on t001**: Even with README read, the grader might reject if the answer format doesn't match its expectation. Mitigation: passing t051 (same template) already reads README and passes — high confidence.
