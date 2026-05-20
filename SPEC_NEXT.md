# SPEC_NEXT — RETIRED

This file used to lay out the P1–P6 plan for pushing past the
41/42 ceiling. **All P1–P6 items landed and the deterministic
floor is now 42/42** (two consecutive PROD runs at v0.1.108).

For the current state, see:
- `STATUS.md` — locked-in stack + LLM-side rules + cost
- Memory: `project_ecom_v108_deterministic_42_42.md` — context
  for the next session

For session history (what was tried and reverted), see the
session memories under
`/home/claude-developer/.claude/projects/-home-claude-developer-bitgn-ecom/memory/MEMORY.md`.

If you're starting a new push because PROD dropped below 42/42:

1. Identify the specific failing task(s) from the bench artifact
   under `artifacts/bench/`.
2. Read the trace under `logs/<latest>/<task>__run0.jsonl` and
   inspect:
   - The agent's `task_spec` emission (correct kind? correct
     products?)
   - Completer events (`REFS_DROP` arch records for `sku_completer`,
     `addenda_completer`, `fraud_*`, `cite_completer`,
     `refusal_*`)
   - Grader's `score_detail` — what is it specifically rejecting?
3. Reproduce locally if a graded snapshot covers the family
   (`tests/test_*_snapshot.py`) — see `STATUS.md` "Local test
   infrastructure" section.
4. Ship a targeted fix. Commit + push immediately.

Do NOT touch the stack speculatively. Each removal/loosening risks
the deterministic floor. The session that produced 42/42 logged
multiple regressions from "improvements" that broke working paths
(high reasoning_effort, count-override, brand-only yes_no
fallback — all reverted). The `feedback_enforcer_cannot_replace_adaptive_llm`
memory captures the core principle: **enforcers ADD refs (union
semantics), never rewrite the LLM's numeric answer**.
