# Architecture

Single-session SGR (structured-generation-reasoning) agent for BitGN PAC1.
This document describes the runtime as it shipped on `feat/perf-speedups` at
commit `58e22d1` (PROD score 104/104). It exists so future contest cycles
can use this build as a base without code/docs drift.

For operational rules and the iterate-fix workflow, see `AGENTS.md`. For
the prompt rules that grade-out as broad correctness fixes, see
`docs/PROMPT_RULES.md`.

---

## Top-level shape

```
task → orchestrator → (router → skill?) → agent loop
                                            ├─ LLM step (SGR JSON)
                                            ├─ validator (rules + LLM trigger)
                                            ├─ tool calls via PCM adapter
                                            └─ report_completion
```

- One LLM "round" = one structured JSON object per the SGR schema.
- The orchestrator drives the loop; the agent enforces step limits, tool
  budgets, and outcome rules.
- Routing happens once per task. Skill body is appended to the system
  prompt for the rest of that task; on `UNKNOWN` no skill is appended.

---

## Module map (`src/bitgn_contest_agent/`)

Top level:

| File | Role |
|---|---|
| `cli.py` | Entry point (`bitgn-contest-agent`). Parses args, wires backend, runs orchestrator. |
| `agent.py` | Per-step state machine, tool-budget accounting, step-limit guard. |
| `orchestrator.py` | Drives the SGR loop, dispatches validator + reactive router, emits trace. |
| `session.py` | Per-task session bag (workspace path, transcript, refs). |
| `prompts.py` | Static system prompt (`_STATIC_SYSTEM_PROMPT`) + prepass + workspace-schema preface. |
| `schemas.py` | Pydantic models for the SGR step output (the JSON the LLM emits). |
| `config.py` | Env-var and CLI-arg config. |
| `learning.py` | (Reserved — not active in PAC1.) |

Routing + skills:

| File | Role |
|---|---|
| `router.py` | Tier-1a regex → tier-1b normalize+regex → tier-2 classifier LLM. |
| `router_config.py` | Confidence threshold, enable flag, classifier model. |
| `reactive_router.py` | Fires reactive skills on tool name + path patterns mid-run. |
| `skill_loader.py` | Loads `.md` skills with frontmatter (name, category, regex, hints). |
| `classifier.py` | Tier-2 LLM call (default `claude-haiku-4-5-20251001`, threshold 0.6). |
| `task_hints.py` | Light-touch matchers; emits structured "hint" lines into the prompt. |
| `skills/` | 6 main skills + 2 reactive skills (see Skills section). |

Validation:

| File | Role |
|---|---|
| `validator.py` | Tier-1 deterministic rules + tier-2 LLM-triggered rules. |
| `format_validator.py` | YAML / frontmatter shape checks for outbox + migration writes. |
| `verify.py` | Final-step verification (grounding refs exist, outcome consistency). |

Backend + adapter:

| File | Role |
|---|---|
| `backend/base.py` | Abstract LLM backend. |
| `backend/openai_compat.py` | OpenAI-compatible client (used for `gpt-5.3-codex` via cliproxyapi). |
| `adapter/pcm.py` | Persistent Content Manager — workspace tool surface (`tree`, `read`, `write`, `delete`, `move`, `context`). |
| `adapter/pcm_tracing.py` | PCM call tracing for trace JSONL. |

Trace + benchmarking:

| File | Role |
|---|---|
| `trace_writer.py` | Writes per-task trace JSONL (one event per step). |
| `trace_schema.py` | Trace event shapes. |
| `arch_constants.py` | Hard limits: max_steps, max_inflight_llm, etc. |
| `arch_log.py` | Structured log emitter. |
| `harness.py` | BitGN-server harness client (round-trip the contest harness URL). |

Auxiliary:

| Subpackage | Role |
|---|---|
| `preflight/` | `response.py`, `schema.py`, `semantic_index.py` — infrastructure stubs after the match-found preflight was retired 2026-04-21. |
| `bench/` | In-tree bench helpers used by `scripts/local_bench.py`. |

---

## Skills

Skills are markdown files with YAML frontmatter. Each defines a category,
optional regex patterns, an optional classifier hint, and a body that gets
appended to the system prompt when the skill matches.

Main skills (`skills/*.md`):

| Skill | Category | Trigger surface |
|---|---|---|
| `bill_query.md` | finance | "what's my bill", "how much do I owe" patterns |
| `document_migration.md` | document | OCR / structure / migrate / convert / queue |
| `entity_message_lookup.md` | relationship | who-is / find message-from / contact lookups |
| `finance_lookup.md` | finance | invoice / receipt / vendor lookups (entity-graph traversal) |
| `inbox_processing.md` | inbox | inbox triage, security, attachment handling |
| `project_involvement.md` | knowledge | project-X status / involvement / membership |

Reactive skills (`skills/reactive/*.md`):

| Skill | Fires on |
|---|---|
| `inbox_security.md` | `read` of inbox-shaped paths (any path containing `inbox` segment) |
| `outbox_writing.md` | `write` of outbox-shaped paths |

Reactive skills inject a one-shot reminder when the agent first touches a
sensitive path; they do not replace the main skill body.

---

## Router decision flow

Implemented in `router.py:112-175`:

1. **Tier 1a — regex on original task text.** Free, instant. If any
   skill's regex matches, return that decision with confidence 1.0.
2. **Tier 1b — regex on normalised text.** If no Tier-1a hit and the
   task is non-English, normalise to English and retry the regex set.
3. **Tier 2 — classifier LLM.** On Tier-1 miss, call the classifier with
   the (normalised) task text and the skill metadata. Default model
   `claude-haiku-4-5-20251001`, threshold 0.6 (`router_config.py`).
4. **UNKNOWN.** Below threshold or classifier failure. No skill body is
   appended; the agent runs on the static prompt only.

The classifier returns `{category, confidence, extracted}` — `extracted`
is opaque metadata a skill may use for templating.

`task_hints.py` runs in parallel with the router and emits short
structured "hint" lines (e.g. `_hint_nora_doc_queue`) into the prompt
when its matchers fire. Hints are advisory; they never block routing.

---

## Validator

`validator.py` runs after every LLM step with two layers.

**Tier 1 — deterministic rules** (cheap, always on):

- `CONTRADICTION_OK_NEG` — `outcome=OK` with `outcome_leaning=NONE_*`.
- `DANGEROUS_DENIED_TO_OK` — destructive verb resolved into `OK` without
  evidence the request was safe.
- `MUTATION_GUARD` — write/delete/move while `outcome_leaning=GATHERING_INFORMATION`.
- Stale-gathering — N consecutive `GATHERING_INFORMATION` steps with no
  read/search progress.

**Tier 2 — LLM-triggered checks** (fired only when a deterministic
trigger condition matches):

- `FIRST_TRANSITION` — first step that leaves `GATHERING_INFORMATION`.
- `CLARIFICATION` — step that proposes `OUTCOME_NONE_CLARIFICATION`.
- `INBOX_READ` — first inbox read in a task.
- `PROGRESS_CHECK` — periodic sanity check on long tasks.
- `ENTITY_FINANCE_SEARCH` — entity-graph traversal for finance lookups.

Tier-2 calls return a structured critique. The agent re-emits the step
with the critique embedded as a `validator_feedback` field; the LLM may
revise its plan.

`format_validator.py` is invoked before any write whose body begins with
`---` (frontmatter). It rejects malformed YAML so the agent gets a
critique on the same step instead of a silent grading failure.

---

## Local bench tooling (`scripts/`)

| Script | Role |
|---|---|
| `local_bench.py` | Run the agent locally against a snapshot directory. Supports `--max-parallel`, `--max-inflight-llm`, `--runs`. |
| `scrape_harness_url.py` | Pull a harness URL into a local snapshot. |
| `scrape_prod_full.py` / `scrape_prod_run_smoke.py` | Full / smoke scrape of a PROD run. |
| `ingest_bitgn_scores.py` | Pull server-side scores for a run-id and merge into a bench JSON. **Canonical truth for grading.** |
| `rebuild_ws_from_transcript.py` | Reconstruct a workspace from a trace transcript. |
| `capture_workspace.py` | Snapshot a live workspace for offline replay. |
| `ws_compare.py` / `ws_dump.py` / `ws_verify_determinism.py` | Workspace diffing + determinism checks across runs. |
| `harvest_failed_fixtures.py` / `harvest_test_cases.py` | Pull failing fixtures from a bench JSON for snapshot work. |
| `bench_summary.py` | Per-task wall stats + outcome breakdown for a bench JSON. |
| `compare_scrape_runs.py` | Diff two PROD scrapes (workspace and instructions). |

Canonical bench config is `--max-parallel 3 --max-inflight-llm 6` (p3i6).
Raising `--max-parallel` reduces local total wall but does not improve
the contest score (see `docs/PROMPT_RULES.md` and AGENTS.md scoring
section).

---

## Iterate-fix workflow

The full procedure (snapshot → reproduce 5x → diagnose → broad-rule fix
→ 5x validate → regression → bench gate) is documented in `AGENTS.md`.
This file does not duplicate it; treat AGENTS.md as the authoritative
source for the operational loop.

Workspace snapshots live under `artifacts/ws_snapshots/<task_id>/run_*/`
with a `metadata.json` carrying `intent` and `expected_outcome`.

---

## Versioning

`VERSION` is the canonical version source for the agent build. Bump it
on every commit that changes runtime behavior, prompt rules, or skills.
`pyproject.toml` mirrors `VERSION` for packaging.
