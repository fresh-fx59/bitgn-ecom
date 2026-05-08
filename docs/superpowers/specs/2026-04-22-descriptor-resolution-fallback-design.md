# Descriptor resolution — prepass semantic index

**Status:** proposal, awaiting local validation.
**Source failures:** `artifacts/ws_snapshots/t072_founder_invoices/`, `t075_founder_dob/`, `t076_degrade_lane/` (run_0 snapshots captured from PROD bench `3770ad1_t091_fix_p3i6_prod_runs1`).
**Scope:** one new preflight command + a tiny extension to the prepass bootstrap block.

## Context

The Fix A+B bench (commit 2c16d09, merged to main 2026-04-22) cleared t091 but surfaced three descriptor-resolution failures that share a single root cause:

| Task | Descriptor | Agent picked | Correct | Why wrong |
|---|---|---|---|---|
| t072 | "the founder I talk product with" | Elena (day_job_ceo) | Nina (startup_partner) | Matched on `relationship: day_job_ceo` + body word "Founder"; ignored Nina's body line "Pushes Miles to narrow the product" |
| t075 | "the founder I talk product with" (DoB) | Elena → 26-05-1984 | Nina → 05-09-1989 | Same semantic mismatch as t072 |
| t076 | "the do-not-degrade lane" | `black_library_evenings` ("preserve a protected lane" in goal) | `harbor_body` (`lane: health`, goal "stay functional enough…without quietly collapsing") | Matched on the literal token "lane" in the wrong project's goal text |

In all three, the agent did reach the cast/projects roots, listed the records, and read several candidates — but the semantic gap between *descriptor* (informal phrase the task uses) and *record field* (canonical frontmatter) was never bridged. The agent defaulted to the first superficial keyword match.

Fix A helps only when the descriptor is a proper-noun token with a case-sensitive miss (t091). It cannot help with paraphrase matching.

## Why previous integration points don't exist

An earlier sketch had the resolver hook into `preflight_entity` / `preflight_project` modules. Those were deleted on 2026-04-21 per `docs/superpowers/specs/2026-04-21-preflight-trim-verify-design.md` — only `preflight_schema` remains. The survivor is the PCM prepass in `src/bitgn_contest_agent/adapter/pcm.py:256-323`, which runs once at task start, tries `tree / read AGENTS.md / context / preflight_schema`, and injects `WORKSPACE SCHEMA …` into `bootstrap_content`.

The design below adds one command to that same prepass list — no new per-skill surface, no LLM-driven retrieval, no runtime tool the agent has to know to call.

## Proposed fix — `preflight_semantic_index`

### Shape

New request type `Req_PreflightSemanticIndex` in `src/bitgn_contest_agent/schemas.py`, dispatched in `src/bitgn_contest_agent/adapter/pcm.py:PcmAdapter.dispatch` (alongside the existing `Req_PreflightSchema` branch), implemented by a new `run_preflight_semantic_index(client, schema)` in `src/bitgn_contest_agent/preflight/semantic_index.py`.

It runs **after** `preflight_schema` (so `entities_root` / `projects_root` are known) and produces a compact JSON blob the prepass appends to `bootstrap_content` as a second bootstrap message.

### Bootstrap message format

```
WORKSPACE SEMANTIC INDEX (cast + projects digest, use to map informal
descriptors like "the founder I talk product with" or "the do-not-degrade
lane" to canonical ids before running any lookup):

CAST:
- entity.miles   alias=miles   relationship=self             "Overloaded systems builder trying to make AI useful at home and at work."
- entity.nina    alias=nina    relationship=startup_partner  "Pushes Miles to narrow the product and find a real buyer."
- entity.elena   alias=elena   relationship=day_job_ceo      "Founder and CEO who cares whether operational pain can become commercial leverage."
- entity.sara    alias=sara    relationship=wife             "Co-parent…"
  … (all cast members, one line each)

PROJECTS:
- project.harbor_body               lane=health  status=active  "Stay functional enough to carry family, work, and startup life without quietly collapsing."
- project.black_library_evenings    lane=family  status=active  "Preserve a protected evening lane for reading…"
  … (all project records, one line each)
```

Field shape:
- **Cast line**: `- <id>   alias=<alias>   relationship=<relationship>   "<summary>"` where `<summary>` is the first non-blank prose line after the bullet-list/frontmatter block (truncated to 160 chars).
- **Project line**: `- <id>   alias=<alias>   lane=<lane>   status=<status>   "<goal>"` where `<goal>` is the `goal:` field if present, else the first prose line (truncated to 160 chars).
- Missing field → the `key=` token is omitted, not left empty.

### Why a digest, not the raw files

The failure is not "missing info" — the agent reached the raw files and still picked wrong. It's **lack of a side-by-side view**. Dumping one line per record forces the contrast between candidates into the same message, which is what lets the LLM compare "startup_partner + pushes Miles on product" against "day_job_ceo + operational pain commercial leverage". Reading cast records sequentially via `read` gives it one at a time; the digest gives it all at once.

### Why no new tool and no LLM-driven matcher

- No new agent-facing tool → nothing for the agent to forget to call, no per-skill wiring.
- No LLM-driven matcher inside the resolver → deterministic, cheap, no extra budget.
- The digest reuses the same metadata parser (`parse_record_metadata` in `preflight/schema.py:73`) that already handles YAML frontmatter / bullet lists / ASCII tables / heading heuristic — no new parsing surface.

### Sizing

t072 snapshot: 22 cast + 14 projects. At ~130 chars/line that's ~4.7 KB of added prepass context per task. Cheap and bounded — the cast is authored once per benchmark world and projects scale with the fixture, not with the task count.

### Failure / truncation behavior

- If `entities_root` is unset in the schema → emit only `PROJECTS:` (silent partial).
- If `projects_root` is unset → emit only `CAST:`.
- If both unset or the command errors → the prepass behaves exactly as it does today (no bootstrap addition, `preflight_semantic_index` contributes no trace entry beyond `ok=false`).
- If a record's frontmatter is unparseable → skip with no error (trust parser's existing fail-safe semantics).
- Per-lane cap: 100 records each (far above PROD's current 22/14). Beyond the cap, emit `…(+N more)` suffix so the agent knows the index is truncated.

## Test plan

1. **Unit — parser shape** (`tests/preflight/test_semantic_index.py`)
   - Given a fixture dir with 3 cast (one YAML-frontmatter, one bullet-list, one malformed), assert the digest emits 2 lines and skips the malformed record silently.
   - Given 2 project records (one with `goal:` field, one without), assert goal-field wins over first prose line.
   - Given `entities_root=None`, assert digest emits only `PROJECTS:` block.

2. **Adapter — wired into prepass** (`tests/adapter/test_pcm_prepass.py`)
   - Extend the existing prepass fixture to include a cast + projects tree.
   - Assert `bootstrap_content` contains two entries (schema + semantic index) in order.
   - Assert trace writer records a `preflight_semantic_index` step with `ok=true` and non-zero bytes.

3. **Local replay via `local_bench.py`**
   - t072, t075, t076 each 5× on main (baseline): confirm all 5/5 fail with server-side wrong answer or OUTCOME_NONE_CLARIFICATION.
   - Apply fix, run 5× each: acceptance criterion is ≥3/5 pass per task.

4. **PROD bench**: p3i6 runs=1 on the fix commit, compare server-side pass count against baseline of 101/104 (the Fix A+B post-merge number, normalized by intent not slot).

## Acceptance criteria

- Baseline (main): ≥4/5 of each of t072/t075/t076 local replays fail.
- Fixed: ≥3/5 of each of t072/t075/t076 local replays pass.
- PROD bench: server-side pass rate strictly higher than Fix-A+B baseline, with no regression on t091 (Badger slot — whatever slot ID it lands in), t053, t078, t084.
- No new flaky tests in CI.

## Non-goals

- Rewriting the entity-message or project-involvement skill markdown.
- Adding a natural-language resolver (LLM-backed `resolve_descriptor` tool).
- Indexing arbitrary markdown beyond `entities_root` and `projects_root`.
- Solving cross-record references (e.g. "the partner Sara works with") — that's a second-hop problem and out of scope; the digest gives direct one-hop descriptors only.
- Normalizing descriptor phrasing (e.g. canonical phrase lists per entity) — the LLM does this matching using the digest as context.

## Open questions

1. Should the bootstrap message live in `pcm.py`'s prepass loop, or should `run_preflight_semantic_index` return the pre-formatted bootstrap string itself (adapter just appends)? *Leaning toward the latter so the formatting stays testable in isolation.*
2. On the trace side, should the semantic index's per-record work (list + read of every cast/project file) attribute to `prepass` origin like `preflight_schema` does, or to a dedicated `preflight_semantic_index` origin for easier log filtering? *Leaning toward `prepass` for consistency; log filter can still discriminate on the `cmd` label.*
