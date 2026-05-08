# Keep regex tier-1 routing — 2026-04-14

## Decision

Regex tier-1 in `src/bitgn_contest_agent/router.py` stays in place.
Do NOT remove in favour of tier-2-only routing.

## Evidence

Trace `logs/20260414_184041` (bench: `f9613a7_v019_archlog_p10i15_prod_runs1.json`):

| Source        | Tasks | Pass rate |
|---------------|-------|-----------|
| tier1_regex   | 4     | 100%      |
| tier2_llm     | 100   | 91%       |

Zero mis-routing observed. Removing tier-1 would replace 4 free
regex hits with 4 extra LLM calls and gain nothing.

## Revisit when

A future trace shows a regex-routed task failing because the regex
skill was wrong for the task (e.g. it grabbed a task that should have
matched a different category). Grep the `arch_report.py --category
SKILL_ROUTER --source tier1_regex` output against failing task ids.
