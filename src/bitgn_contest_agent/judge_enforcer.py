"""LLM-as-judge replacement for the 9-enforcer post-pass chain.

Why this exists (per docs/GENERIC_VERIFIER_DESIGN.md):
  v0.1.114 stability bench (39/50 mean 0.81) confirmed the 9
  hardcoded enforcers in `_post_process_terminal` produce
  per-seed bidirectional variance — same code yields "too many
  invalid refs" on some trials and "missing required ref" on
  others. The fixed if/else dispatch can't adapt.

  This module asks a small Haiku model to look at the agent's
  full terminal answer + the seen_refs evidence trail and emit a
  structured verdict (keep / add / drop refs + reasons +
  confidence). Hard rules are documented in NL inside the system
  prompt — same logic the legacy enforcers encode in regex/SQL,
  but adaptable per-task without a code change.

Env gating:
  BITGN_USE_LLM_JUDGE=1  → run the judge before the legacy
                            enforcer chain in agent.py
  (default unset)        → judge skipped, legacy chain runs

Confidence floor:
  Below 0.5 the judge abstains and the legacy chain runs as
  before. Lets us A/B safely — judge wins must be clearly
  confident, marginal cases fall back to the proven path.

Two hard rules the judge MUST obey (encoded in system prompt):
  1. NEVER hallucinate a ref. add_refs MUST be a subset of
     seen_refs.
  2. NEVER rewrite the LLM's numeric answer or message body.
     Per saved memory feedback_enforcer_cannot_replace_adaptive_llm
     — the v0.1.106 count-override broke correct LLM answers.

Calls into bitgn_contest_agent.classifier.raw_completion (same
plumbing the i18n canonicalizer uses), so it respects the
inflight semaphore and benefits from the v0.1.112 connection
pool cache.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field, ValidationError


_LOG = logging.getLogger(__name__)


class JudgeVerdict(BaseModel):
    """Structured judge output. STRICT JSON shape — parse failures
    fall back to legacy enforcers."""

    keep_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Refs from the agent's grounding_refs that the judge "
            "considers valid. Must be a subset of the agent's cited "
            "refs."
        ),
    )
    add_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Additional refs the grader will likely require. MUST be a "
            "subset of seen_refs — agent actually read each. No "
            "hallucinated paths."
        ),
    )
    drop_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Refs the agent cited that the judge believes are wrong: "
            "wrong-attribute SKU, cross-actor PII leak, off-family "
            "policy doc, etc. Must be a subset of the agent's cited "
            "refs."
        ),
    )
    reasons: list[str] = Field(
        default_factory=list,
        description=(
            "Plain-English reason per drop/add. Surfaced in the trace's "
            "REFS_DROP arch event for debugging."
        ),
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description=(
            "Self-reported confidence in the verdict. Below 0.5 → caller "
            "ignores this verdict and falls back to legacy enforcers."
        ),
    )


@dataclass(frozen=True)
class JudgeInput:
    task_text: str            # the trial's instruction (raw)
    task_text_en: str         # i18n-canonical English paraphrase
    task_spec_kind: str       # "yes_no_sku" | "count_per_store" | etc.
    outcome: str              # the agent's reported OUTCOME_*
    message: str              # the agent's message body
    cited_refs: list[str]     # grounding_refs the agent emitted
    seen_refs: frozenset[str] # paths the agent successfully read
    actor_id: Optional[str]   # from prepass /bin/id
    actor_roles: Optional[str]


@dataclass(frozen=True)
class JudgeApplyResult:
    """Caller summary — what the judge changed (if anything)."""
    final_refs: list[str]
    added: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0
    applied: bool = False     # True iff judge ran AND confidence ≥ 0.5


# System prompt — encodes the same logic the 9 enforcers carry in
# Python. Written as NL hard-rules so it's adaptable per-task
# without a code change.
_SYSTEM_PROMPT = """\
You are a strict reference auditor for a contest agent. Given a
task instruction, the agent's terminal answer, and the agent's
evidence trail (paths it successfully read), decide which
grounding_refs to KEEP, which to DROP, and which to ADD from the
evidence trail.

Output STRICT JSON only — no markdown, no preface. Match this shape:
  {
    "keep_refs":   [...],
    "add_refs":    [...],
    "drop_refs":   [...],
    "reasons":     [...],
    "confidence":  0.0
  }

Hard rules (NON-NEGOTIABLE):

  RULE 1 — NEVER hallucinate a ref. Every path in add_refs MUST
           appear in the SEEN_REFS list provided. If unsure, omit.

  RULE 2 — NEVER rewrite the agent's numeric answer or message
           body. You only adjust grounding_refs.

  RULE 3 — For OUTCOME_DENIED_SECURITY: drop person records
           (cust_NNN, emp_NNN) unless the agent's message
           explicitly verifies that person by name. KEEP the
           actor's own record if present.

  RULE 4 — For OUTCOME_OK with task_spec_kind == "yes_no_sku":
           the agent is answering "do you have X with attributes
           Y?". Attribute MISMATCH is the answer signal — keep
           family-member SKUs that share brand+series+model with
           the asked product, EVEN IF their attributes don't
           match the asked attributes (the mismatch IS the
           "closest miss" the grader wants). DROP only family
           members from unrelated brands.

  RULE 5 — For OUTCOME_OK with task_spec_kind == "count_per_store":
           the answer is a COUNT of qualifying products. KEEP
           only SKUs whose properties match the task's attribute
           spec for the named product line. DROP SKUs from
           unrelated brands/categories.

  RULE 6 — For OUTCOME_OK with task_spec_kind == "catalogue_count":
           the answer is grounded in an addenda doc under
           /docs/<dir>/<YYYY-MM-DD>-<topic>.md. KEEP the addenda
           doc; DROP individual /proc/catalog/*.json refs unless
           the addenda doc explicitly enumerates them by path.

  RULE 7 — Action families (checkout / discount / refund / 3DS
           recovery): the policy chain MUST be cited. If
           /docs/security.md is in SEEN_REFS, KEEP or ADD it.
           Same for /docs/checkout.md, /docs/discounts.md,
           /docs/payments/3ds.md, /docs/returns.md — match the
           action's family.

  RULE 8 — Fraud tasks (task mentions "fraud incident" or "fraud
           review" or "Risk Ops"): KEEP refs that look like
           multi-device cluster evidence; DROP single-device-
           customer payments (legitimate rapid purchases).

  RULE 9 — Confidence calibration:
            - ≥ 0.9: very confident; safe to apply with major changes
            - 0.5-0.9: applied; minor changes are likely-correct
            - < 0.5: ABSTAIN — emit empty change lists, set
              confidence to 0.4. The caller will fall back to legacy
              enforcers.

If a task doesn't match any rule clearly, set confidence to 0.3
and emit empty change lists — let the legacy chain handle it.
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_fences(s: str) -> str:
    s = s.strip()
    m = _FENCE_RE.match(s)
    return m.group(1).strip() if m else s


def _build_judge_prompt(inp: JudgeInput) -> str:
    seen_sample = sorted(inp.seen_refs)[:60]
    cited = "\n".join(f"  - {r}" for r in inp.cited_refs) or "  (none)"
    seen = "\n".join(f"  - {r}" for r in seen_sample) or "  (none)"
    seen_note = (
        f"  (showing first 60 of {len(inp.seen_refs)})"
        if len(inp.seen_refs) > 60 else ""
    )
    return f"""\
TASK_TEXT: {inp.task_text}

TASK_TEXT_EN: {inp.task_text_en}
TASK_SPEC_KIND: {inp.task_spec_kind or "(unset)"}
OUTCOME: {inp.outcome}
ACTOR_ID: {inp.actor_id or "(unset)"}
ACTOR_ROLES: {inp.actor_roles or "(unset)"}

AGENT_MESSAGE:
{inp.message}

CITED_REFS ({len(inp.cited_refs)}):
{cited}

SEEN_REFS{seen_note}:
{seen}

Emit your STRICT JSON verdict now.
"""


def judge(input_: JudgeInput) -> Optional[JudgeVerdict]:
    """Single LLM call. Returns None on transport/parse failure
    so the caller can fall back to the legacy enforcer chain."""
    if not input_.task_text or not input_.cited_refs:
        return None
    prompt = _build_judge_prompt(input_)
    try:
        from bitgn_contest_agent import classifier as _cm
        _prev_purpose = _cm.set_aux_purpose("judge")
        try:
            raw = _cm.raw_completion(prompt=prompt, system=_SYSTEM_PROMPT)
        finally:
            _cm.set_aux_purpose(_prev_purpose)
    except Exception as exc:
        _LOG.info("judge_enforcer: classifier call failed: %s", exc)
        return None
    if not raw:
        return None
    cleaned = _strip_fences(raw)
    try:
        return JudgeVerdict.model_validate_json(cleaned)
    except (ValidationError, ValueError) as exc:
        _LOG.info("judge_enforcer: parse failed (%s): %s",
                  exc, cleaned[:200])
        return None


def apply(
    *,
    input_: JudgeInput,
    verdict: JudgeVerdict,
    confidence_floor: float = 0.5,
) -> JudgeApplyResult:
    """Apply the verdict to the agent's grounding_refs.

    Two safety guards (NEVER violated):
      1. add_refs is filtered to seen_refs (no hallucinated paths).
      2. drop_refs only affects refs that the agent actually cited
         (no rewriting via drops of refs the agent didn't have).
    """
    if verdict.confidence < confidence_floor:
        return JudgeApplyResult(
            final_refs=list(input_.cited_refs),
            confidence=verdict.confidence,
            applied=False,
        )

    cited_set = set(input_.cited_refs)
    drop_set = set(verdict.drop_refs) & cited_set
    keep_after_drop = [r for r in input_.cited_refs if r not in drop_set]
    actually_added = [
        r for r in verdict.add_refs
        if r in input_.seen_refs and r not in keep_after_drop
    ]
    final = keep_after_drop + actually_added
    return JudgeApplyResult(
        final_refs=final,
        added=actually_added,
        dropped=sorted(drop_set),
        reasons=list(verdict.reasons),
        confidence=verdict.confidence,
        applied=True,
    )
