# SPEC_NEXT — RETIRED

This file used to lay out the P1–P6 plan for pushing past the
41/42 ceiling. **All P1–P6 items landed and the deterministic
floor is now 44/44** (v0.1.111 on CloseRouter; equivalent v0.1.108
baseline holds on cliproxyapi for the 42-task era).

For the current state, see:
- `STATUS.md` — locked-in stack + LLM-side rules + provider notes
- Memory: `project_ecom_v108_deterministic_42_42.md` +
  `project_ecom_v108_handles_t43_t44_refund.md` — historical context

For session history (what was tried and reverted), see the
session memories under
`/home/claude-developer/.claude/projects/-home-claude-developer-bitgn-ecom/memory/MEMORY.md`.

If you're starting a new push because PROD dropped below 44/44:

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
   - If `error_kind=INTERNAL_CRASH` with `APIError: <message>`,
     add the message substring to `_TRANSIENT_MESSAGE_SUBSTRINGS`
     in `backend/openai_compat.py` (the v0.1.111 recipe).
3. Reproduce locally if a graded snapshot covers the family
   (`tests/test_*_snapshot.py`) — see `STATUS.md` "Local test
   infrastructure" section.
4. Ship a targeted fix. Commit + push immediately.

Do NOT touch the stack speculatively. Each removal/loosening risks
the deterministic floor. The session that produced 42/42 (and later
44/44) logged multiple regressions from "improvements" that broke
working paths (high reasoning_effort, count-override, brand-only
yes_no fallback, 2nd sku_verifier-after-completer pass — all
reverted). The `feedback_enforcer_cannot_replace_adaptive_llm`
memory captures the core principle: **enforcers ADD refs (union
semantics), never rewrite the LLM's numeric answer**.
