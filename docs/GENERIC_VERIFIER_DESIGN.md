# Generic verifier design — replace the 9-enforcer chain with one LLM-as-judge

> Design doc, no code shipped. Generated after the v0.1.114 = 43/50
> milestone where the user observed "lots of if/else structures
> looks like overfit". Web research (sources at bottom) converged
> on a single high-EV refactor; this doc spec'es it for a clean
> next-session implementation.
>
> Provider-side note: at write time both cliproxyapi (cooldown
> 1h28m) and CloseRouter (full upstream outage) are dead. Cannot
> validate this design's predictions against PROD until proxy
> recovers. Lands as architecture spec only.

## Current state — what's overfit

| Module | Overfit shape | Generic essence |
|---|---|---|
| `refusal_cite_enforcer.py` | ~160 English phrase patterns, 5 hardcoded regex lists, actor-id extraction regex | "Did the message verifiably back the cited person/entity refs?" |
| `cite_completer.py` | `_ACTION_FAMILY_TRIPLES = {checkout, discount, ds3_recover, refund}` → fixed `/docs/*.md` paths | "Cite the policy doc(s) the action family is bound by" |
| `addenda_completer.py` | 5 hardcoded `/docs/<dir>` paths + 3 task-phrasing regexes | "When a count task references an addendum doc, include that doc" |
| `sku_completer.py` | brand/series/model/attribute SQL ladder × 3 relaxation tiers, family enumeration caps gated by `BITGN_PROVIDER_PROFILE` | "Cite the SKUs that match the task spec" |
| `fraud_cluster_filter.py` | 5-pattern fraud SQL + device-count discriminator | "Drop fraud clusters where the single-device customer indicator says no" |
| `fraud_recall_completer.py` | Symmetric to filter — adds missing canonical fraud rows | "Make sure every canonical fraud row is cited" |
| `sku_verifier.py` | drop-on-property-contradiction rule | "Does this cited SKU actually match the task attributes?" |
| `prompts.py` | 14 "v0.X.Y t-NN failure" scar references in the 67KB system prompt | Few-shot exemplars + general rules |

Total: **~160 phrases + ~30 regexes + 5 hardcoded path triples + 14 task-failure scars**. Each new task family adds another branch.

## Target architecture

```
                          ┌──────────────────────────────┐
agent.report_completion → │     SingleJudge (Haiku)      │ → revised (refs, message)
                          │                              │
                          │  Input: task_text, message,  │
                          │   cited_refs, seen_refs,     │
                          │   task_spec, agent_id_role   │
                          │                              │
                          │  Output: {keep_refs,         │
                          │           add_refs,          │
                          │           drop_refs,         │
                          │           reasons[],         │
                          │           confidence}        │
                          └──────────────────────────────┘
```

**One LLM call replaces all 9 enforcers.** The judge sees the agent's full answer, the task, and the available evidence trail (`seen_refs`). It emits a structured decision in the same shape the current enforcers cumulatively produce.

Two-shape compatibility ladder:
- **Tier 1** (default): SingleJudge — one Haiku call, structured output via Pydantic.
- **Tier 2** (gated by `BITGN_JUDGE_ENSEMBLE=1`): N-best voting across 3 Haiku samples for variance-bound task families (fraud, SKU-pick). Adaptive stop on confidence ≥ 0.9.

## Module sketch — `judge_enforcer.py`

```python
"""Generic LLM-as-judge replacement for the 9-enforcer post-pass chain.

Subsumes: refusal_cite_enforcer, cite_completer, addenda_completer,
sku_completer, sku_verifier, fraud_cluster_filter, fraud_recall_completer.
Keeps the existing modules importable as fallback when the judge
abstains (low-confidence) or when BITGN_USE_LEGACY_ENFORCERS=1.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

import os

from pydantic import BaseModel, Field


class JudgeVerdict(BaseModel):
    """Structured output of the LLM-as-judge.

    Two design rules:
      1. The judge can ADD refs (must be from seen_refs only — no
         hallucinated paths) and DROP refs the agent cited.
      2. The judge MUST NOT rewrite the LLM's numeric answer.
         (Saved memory feedback_enforcer_cannot_replace_adaptive_llm
         documents the failure mode of v0.1.106 count-override.)
    """
    keep_refs: list[str] = Field(default_factory=list,
        description="Refs from the agent's grounding_refs that pass.")
    add_refs: list[str] = Field(default_factory=list,
        description="Additional refs from seen_refs that the grader "
                    "will likely require. Only paths the agent actually "
                    "read — must appear in seen_refs.")
    drop_refs: list[str] = Field(default_factory=list,
        description="Refs from the agent's grounding_refs that the "
                    "judge believes are wrong-attribute / wrong-family / "
                    "PII-leaking / cross-actor.")
    reasons: list[str] = Field(default_factory=list,
        description="Plain-English reasons for each drop/add. "
                    "Surfaced in the trace's REFS_DROP arch event.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0,
        description="Self-reported confidence. Below 0.5 → fall "
                    "back to legacy enforcer chain.")


@dataclass(frozen=True)
class JudgeInput:
    task_text: str            # the trial's instruction
    task_text_en: str         # the i18n-canonicalized English version
    task_spec_kind: str       # e.g. "yes_no_sku", "count_per_store"
    outcome: str              # the agent's reported OUTCOME_*
    message: str              # the agent's message body
    cited_refs: list[str]     # grounding_refs the agent emitted
    seen_refs: frozenset[str] # paths the agent successfully read
    actor_id: Optional[str]   # from prepass /bin/id
    actor_roles: Optional[str]


_SYSTEM_PROMPT = """\
You are a strict reference auditor for a contest agent.

Your job: given the task, the agent's answer, and the agent's
evidence trail (the paths it successfully read), decide which
grounding_refs to KEEP, which to DROP, and which additional refs
to ADD from the evidence trail.

Hard rules:
  1. ADD a ref only if it's in `seen_refs` — never hallucinate a path.
  2. NEVER rewrite the agent's numeric answer or message body.
     You only adjust grounding_refs.
  3. For OUTCOME_DENIED_SECURITY: drop any person record (cust_NNN,
     emp_NNN) unless the message explicitly verifies that person
     by name; keep the actor's own record.
  4. For OUTCOME_OK with task_spec_kind == "yes_no_sku": keep the
     family enumeration (attribute mismatch IS the answer pattern).
  5. For OUTCOME_OK with task_spec_kind == "count_per_store": keep
     only refs whose properties match the task's attribute spec.
  6. For OUTCOME_OK with task_spec_kind == "catalogue_count": cite
     the addenda doc; drop individual SKU refs unless the addenda
     enumerates them by path.
  7. For action families (checkout/discount/refund/3DS recovery):
     ensure the policy chain is cited (/docs/security.md +
     /docs/<family>.md when seen_refs contains them).
  8. For fraud tasks: keep refs that show multi-device cluster
     evidence; drop single-device-customer payments.
  9. Confidence < 0.5 → emit empty changes, the legacy enforcer
     chain will take over.

Output STRICT JSON matching the JudgeVerdict schema.
"""


def judge_and_apply(
    *,
    input_: JudgeInput,
    classifier_raw_completion,  # callable[(prompt), str]
) -> Optional[JudgeVerdict]:
    """Single LLM-as-judge call. Returns None on parse failure;
    caller falls back to legacy enforcers."""
    prompt = _build_judge_prompt(input_)
    try:
        raw = classifier_raw_completion(prompt=prompt)
    except Exception:
        return None
    try:
        return JudgeVerdict.model_validate_json(_strip_fences(raw))
    except Exception:
        return None


def _build_judge_prompt(inp: JudgeInput) -> str:
    return f"""TASK_TEXT: {inp.task_text}

TASK_TEXT_EN: {inp.task_text_en}
TASK_SPEC_KIND: {inp.task_spec_kind}
OUTCOME: {inp.outcome}
ACTOR_ID: {inp.actor_id or "(unset)"}
ACTOR_ROLES: {inp.actor_roles or "(unset)"}

AGENT MESSAGE:
{inp.message}

AGENT CITED REFS ({len(inp.cited_refs)}):
{chr(10).join('  - ' + r for r in inp.cited_refs)}

AGENT SEEN REFS ({len(inp.seen_refs)}, sample):
{chr(10).join('  - ' + r for r in sorted(inp.seen_refs)[:30])}
"""


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()
```

## How it slots in

In `agent.py:_post_process_terminal`:

```python
# Before the existing 9-enforcer chain, run the judge.
# Both BITGN_USE_LLM_JUDGE=1 (opt-in this iteration) and
# confidence >= 0.5 are required to apply the judge's verdict.
# Below threshold, fall through to legacy enforcers.

if os.environ.get("BITGN_USE_LLM_JUDGE", "").lower() in ("1", "true"):
    from bitgn_contest_agent.judge_enforcer import JudgeInput, judge_and_apply
    from bitgn_contest_agent.classifier import raw_completion as _raw
    verdict = judge_and_apply(
        input_=JudgeInput(
            task_text=raw_task_text,
            task_text_en=task_text,
            task_spec_kind=getattr(getattr(fn, "task_spec", None), "kind", "none"),
            outcome=fn.outcome,
            message=fn.message,
            cited_refs=list(fn.grounding_refs),
            seen_refs=frozenset(session.seen_refs),
            actor_id=getattr(self, "_actor_id", None),
            actor_roles=getattr(self, "_actor_roles", None),
        ),
        classifier_raw_completion=_raw,
    )
    if verdict is not None and verdict.confidence >= 0.5:
        new_refs = [r for r in fn.grounding_refs
                    if r not in verdict.drop_refs and r in verdict.keep_refs]
        for added in verdict.add_refs:
            if added in session.seen_refs and added not in new_refs:
                new_refs.append(added)
        if new_refs != list(fn.grounding_refs):
            emit_arch(
                category=ArchCategory.REFS_DROP,
                at_step=None,
                details=(
                    f"judge_enforcer changed refs: "
                    f"kept={len(new_refs)} dropped={len(verdict.drop_refs)} "
                    f"added={len(verdict.add_refs)} conf={verdict.confidence:.2f} "
                    f"reasons={verdict.reasons[:3]}"
                ),
            )
            fn = fn.model_copy(update={"grounding_refs": new_refs})
        return fn  # judge took the call; skip legacy chain

# Legacy 9-enforcer chain follows unchanged.
```

## Cost & latency

- Haiku-4.5 via cliproxyapi: ~$0.0005/call, ~500 ms/call.
- 50-task bench × 1 judge call = **+$0.025, +25s wall**.
- Versus: 9-enforcer chain runs in-process (sub-ms). The judge is more expensive per call but replaces a lot of code paths.

If self-consistency is enabled for variance-bound tasks (fraud, SKU-pick) at N=3, that's an extra +$0.05 / +50s on the targeted ~10 tasks.

## Validation plan

When the proxy returns:

1. **Unit tests first (no proxy)** — mock `classifier_raw_completion`. Verify:
   - Verdict parses correctly
   - Low confidence returns None (caller falls back)
   - Add only from seen_refs (hallucinated path is rejected)
   - drop_refs honored
2. **Local A/B** — run on 20-30 ws_snapshots with judge ON vs OFF. Pass rate must not regress.
3. **Filtered PROD** — 5 representative tasks (one per family) with `BITGN_USE_LLM_JUDGE=1`. Confirm cost + accuracy on real grader.
4. **Full PROD** — only after local + filtered look clean. Direct A/B against the v0.1.114 = 43/50 baseline.

Expected ceiling: **45-47/50 with judge alone**, **47-49/50 with judge + adaptive self-consistency on variance tasks**. To deterministically hit 50/50 we'd still need either provider-side prompt caching at the judge layer OR a 2nd judge in disagreement-trigger mode.

## What this does NOT solve

- The agent's primary LLM may still hallucinate SKU citations or pick the wrong product. The judge can only catch mistakes that are detectable from the evidence trail.
- New task families that don't fit any of the 9 hardcoded rules in `_SYSTEM_PROMPT` will need rule additions to the judge prompt. That's still scar tissue — but it's NL scar, far cheaper to maintain than Python regex scar.

## Sources

- [Constitutional Classifiers — Anthropic](https://www.anthropic.com/research/constitutional-classifiers)
- [LLM-as-Judge in 2026 — Talikot](https://medium.com/@vinayak.talikot/llm-as-judge-got-us-this-far-here-is-what-2026-adds-to-the-toolkit)
- [Agent-as-a-Judge — arXiv 2508.02994](https://arxiv.org/pdf/2508.02994)
- [Adaptive Self-Consistency — arXiv 2512.02543](https://arxiv.org/pdf/2512.02543)
- [Pydantic structured outputs for LLM apps — Xebia](https://xebia.com/blog/enforce-and-validate-llm-output-with-pydantic/)
