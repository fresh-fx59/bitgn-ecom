## Research tools for old-agent traces

Purpose: empirical derivation of §2.4 enforcer rules for the new BitGN agent
design doc (`docs/superpowers/specs/2026-04-10-bitgn-agent-design.md`) from
historical traces produced by the sibling Codex-backed agent
(`~/bitgn-contest`).

### Scope — "most recent format" only

These scripts target **one** trace schema only: paired
`<timestamp>_bitgn_pac1-dev_taskid_<tid>.json` (canonical, graded) +
`iterations/<timestamp>_bitgn_pac1-dev_taskid_<tid>.jsonl` (step-by-step
workflow state). All 43 tasks (t01–t43) appear in this format. Older formats
(e.g. `bitgn_sandbox_*`, pre-`iteration_log_file` traces) are **intentionally
excluded** from research per the working note in this repo:

> Use only most recent format. Do not try to cover all logs by universal script.
> If schema of older logs does not match because of code change, do not try to
> write scripts for them. Exclude them entirely from research. The only option
> to include them is when you have no data for this specific task.

Since all 43 tasks are covered by the recent format, no manual review of older
traces was needed.

### Dataset

After pairing canonical JSON with its referenced iterations JSONL (the
canonical JSON carries `iteration_log_file`):

| Metric | Value |
|---|---:|
| Paired runs | 473 |
| Passed (score ≥ 1.0) | 234 |
| Failed (score < 1.0) | 239 |
| Task coverage | all 43 (t01–t43) |
| Date range | 2026-03-31 – 2026-04-09 |
| Distinct top-level schemas | 1 |

### Files

- `legacy_loader.py` — pairs canonical JSON + iterations JSONL into a uniform
  `Run` record. Extracts terminal outcome, final `workflow_state`, tool set,
  step count, grounding refs. Filters out runs where either file is missing or
  unparseable. Library only; no CLI.
- `bucket_failures.py` — CLI that buckets runs by outcome, terminal_mode, or
  task_id and prints a pass/fail table. Used to eyeball signals before
  codifying them as rules.
- `rule_evaluator.py` — CLI that takes a named candidate rule and reports:
  - fire rate on passes vs fails
  - per-rule precision (of runs the rule would have rejected, what fraction
    were actual failures)
  - discriminative power (pp delta between pass-fire-rate and fail-fire-rate)

  The set of rules is defined in-file as a registry so that adding a new
  candidate is a one-line change.

### Reproducing the §2.4 analysis

```
python3 scripts/research-logs-from-old-agent/bucket_failures.py --by outcome
python3 scripts/research-logs-from-old-agent/rule_evaluator.py --all
```

Outputs are written to stdout only. These scripts read the repo's
`task-t01-t43-logs-produced-by-bitgn-contest-agent/` directly and have no
state. They use stdlib only (no dependencies).
