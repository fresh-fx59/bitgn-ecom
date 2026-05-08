# Appendix — Empirical Derivation of Enforcer Rules (historical reference)

**Status:** **HISTORICAL REFERENCE ONLY.** Neither the v1 analysis below nor the stronger v2 analysis in `scripts/research-logs-from-old-agent/` is adopted into §2.4 of the main design doc as of v0.0.5. After a critique pass, the v0.0.5 design retreats to a minimum-confidence ruleset containing **only** the grounding-refs reachability rule (a structural self-consistency check requiring no historical calibration). All other candidate rules — including the strongest v2 signals (`finalization_not_ready`, `err_internal`, `respond_instructions_missing`) — are listed in §2.4.1 of the main design doc as deferred candidates awaiting real-run calibration. The reasoning is that false-positive costs cannot be estimated without measuring retry stability on live traces, and committing to uncalibrated rules now risks locking in mistakes we cannot untangle later. See §2.4 and §2.4.1 of the main design doc for the current ruleset and the promotion workflow for future rules.

**Date:** 2026-04-10
**Source data:** `task-t01-t43-logs-produced-by-bitgn-contest-agent/` — 1008 historical traces from the sibling Codex-backed agent, aggregated across all schema variants.
**Why historical only:** This appendix used a single inline one-shot script to look at the top-level canonical JSON traces only. That format does not expose the planner's internal workflow state (`evidence_inventory`, `verification_status`, `finalization_ready`, `terminal_mode`, etc.), so only the weakest rules could be calibrated. A later v2 analysis (see `scripts/research-logs-from-old-agent/rule_evaluator.py`) paired canonical JSON with the iterations JSONL sidecar on a 473-run recent-format corpus and found much stronger catch signals, but those too were rolled back from §2.4 during the v0.0.5 retreat to minimum-confidence rules.

**Purpose (v1, preserved as-is below):** Every enforcer rule in §2.4 of the main design doc was either (a) supported by measurable signal in the historical data, or (b) explicitly dropped for lack of signal. This appendix records the measurements so future readers can verify the reasoning.

## Baseline

| Metric | Value |
|---|---|
| Total runs analyzed | 1007 (1 file failed to parse) |
| Passing (score ≥ 1.0) | 443 |
| Failing (score = 0) | 559 |
| Baseline pass rate | 44.0% |

## Rule-by-rule analysis

### Rule 1 — Identity-context gate (KEPT)

**Check:** Did the run call any of `{/fs/context, /load-respond-instructions, /fs/read}` before terminal?

| | With identity tools | Without |
|---|---:|---:|
| Passed (n=443) | 441 (99.5%) | 2 |
| Failed (n=559) | 491 (87.8%) | 68 |

**Signal:** 11.7 pp delta between pass rate and fail rate. Missing identity context is 24× more common in failures than in passes.

**Decision:** KEEP. Rule fires only for non-refusal outcomes (see exemption below).

**Exemption:** `OUTCOME_NONE_UNSUPPORTED` and `OUTCOME_DENIED_SECURITY` are exempt — a task can be legitimately refused from its description alone without loading any context.

### Rule 2 — Nontrivial-work gate (KEPT, new)

**Check:** Did the run ever successfully call a read-like tool (`{/fs/read, /fs/list, /fs/search, /fs/tree, /fs/find, /fs/outline}`) before terminal?

| | With read-like call | Without |
|---|---:|---:|
| Passed (n=443) | 441 (99.5%) | 2 |
| Failed (n=559) | 487 (87.1%) | 72 (**12.9%**) |

**Signal:** 72 failures (13% of all failures) terminated without ever looking at anything. Only 2 passes did — both are trivial arithmetic tasks that legitimately don't need filesystem reads.

**Decision:** KEEP with refusal exemption. This is stronger signal than Rule 1 alone because it catches cases where the agent loaded `/fs/context` but then fabricated an answer without actually reading any task-relevant data.

### Rule 3 — Planner self-assertion (KEPT on principle)

**Check:** Does `NextStep.identity_verified == False` while the planner emits a terminal that requires identity context?

**Data:** The old trace format does not record `identity_verified` explicitly, so this rule cannot be calibrated against historical data.

**Decision:** KEEP. The check is cheap and defends against a specific class of Planner laziness — emitting "done" while simultaneously admitting identity wasn't verified. The regression harness will validate its value on real runs.

### Rule 4 — Grounding-refs reachability (KEPT on principle)

**Check:** Every path in `ReportTaskCompletion.grounding_refs` must appear in `session.seen_refs` (the set of paths successfully read during the run).

**Data:** Old traces don't record `grounding_refs` at all. This is a new field in the hardened single-session schema. Cannot be calibrated historically.

**Decision:** KEEP on principle. Fabricated file references are a well-known LLM hallucination pattern and the cost of the check is near zero.

### DROPPED: OUTCOME_OK minimum message length

**Check:** Reject `OUTCOME_OK` terminal if `len(message.strip()) < 10`.

| | n | median | min | p5 |
|---|---:|---:|---:|---:|
| OK passed | 190 | 160 | **3** | 31 |
| OK failed | 143 | 168 | 2 | 31 |

**Signal:** None. Pass and fail distributions are statistically identical. Passing runs exist with 3-byte OK messages.

**Decision:** DROPPED. A minimum-length rule would catch almost no real failures and would reject some legitimate passes.

### DROPPED: NONE_CLARIFICATION keyword coherence

**Check:** Reject `OUTCOME_NONE_CLARIFICATION` if `message` contains neither "clarif" nor "?".

| | n | With keyword | Rate |
|---|---:|---:|---:|
| CLARIFICATION passed | 80 | 32 | 40% |
| CLARIFICATION failed | 166 | 61 | 37% |

**Signal:** 3 pp difference. Effectively zero discriminative power.

**Decision:** DROPPED. Many valid clarification responses don't use the word "clarification" or a question mark.

### DROPPED: Short-run guardrail

**Check considered:** Reject terminals emitted at step ≤ 2.

| | Passed | Failed |
|---|---:|---:|
| ≤2 steps | 27 | 94 |
| Passing outcomes | `NONE_UNSUPPORTED` (20), `NONE_CLARIFICATION` (4), `DENIED_SECURITY` (3) | — |
| Failing outcomes | — | `OUTCOME_ERR_INTERNAL` (57), `""` (21), `NONE_UNSUPPORTED` (6) |

**Signal:** Early termination is legitimate for refusal outcomes (23 of 27 passing short runs). The failing short runs are mostly `OUTCOME_ERR_INTERNAL` (agent gave up) which the enforcer already rejects implicitly (because `ERR_INTERNAL` terminals indicate the planner couldn't proceed).

**Decision:** DROPPED. The signal is already captured by Rules 1 and 2 combined with refusal-outcome exemption.

## Findings the enforcer cannot fix — flagged for §2.5 prompt design

These are real reliability leaks found in the data, but they cannot be caught by algorithmic policy. They go into prompt engineering instead.

### Leak 1: `OUTCOME_NONE_CLARIFICATION` hallucinates ambiguity

`OUTCOME_NONE_CLARIFICATION` has a **29.4% pass rate** (64 / 218 emissions). The agent claims it needs clarification 70% of the time when the task actually has a definite answer. This is the largest reliability leak in the dataset.

**Prompt implication:** the system prompt should heavily weight the "do not ask for clarification when the task is answerable from local evidence" rule, and the `outcome_justification` field in `ReportTaskCompletion` should be required to explain *why* clarification is needed — forcing the planner to commit to a concrete ambiguity.

### Leak 2: `OUTCOME_OK` has 43% false positives

`OUTCOME_OK` has a **57.1% pass rate** (190 / 333 emissions). When the agent says "done", the grader disagrees 43% of the time. Second-biggest leak.

**Prompt implication:** the `completed_steps_laconic` field should be required to list concrete operations performed, and the prompt should require cross-checking stated completion against actual tool dispatches before emitting OK. The Enforcer could potentially be extended in a future iteration to check that `completed_steps_laconic` is non-trivial, but for now this is out of scope.

## Pass rate by outcome (for reference)

| Outcome | Passed | Failed | Pass rate |
|---|---:|---:|---:|
| `OUTCOME_NONE_UNSUPPORTED` | 79 | 16 | **83.2%** |
| `OUTCOME_DENIED_SECURITY` | 54 | 17 | **76.1%** |
| `OUTCOME_OK` | 190 | 143 | 57.1% |
| `OUTCOME_NONE_CLARIFICATION` | 64 | 154 | **29.4%** |
| `OUTCOME_ERR_INTERNAL` | 0 | 154 | 0% |
| `ERROR_INTERNAL` (legacy) | 0 | 31 | 0% |
| `NONE_CLARIFICATION_NEEDED` (legacy) | 16 | 12 | 57.1% |
| empty / malformed | 40 | 32 | 55.6% |

Refusal outcomes have the highest pass rates; OK is middling; CLARIFICATION is the worst non-error outcome. This ordering directly informs which planner behaviors are most in need of improvement.

## Reproducibility

The Python analysis that produced all numbers in this appendix is inline in the conversation log for the session ending 2026-04-10. No fixture files are committed because the source data (`task-t01-t43-logs-produced-by-bitgn-contest-agent/`) is already in the repository and the analysis is a ~50-line stdlib-only script. If the analysis is repeated after the new-agent regression harness is running, the new numbers should supersede this appendix entirely.
