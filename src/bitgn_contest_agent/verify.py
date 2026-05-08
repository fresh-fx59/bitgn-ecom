"""Pre-completion verification trigger (v1, 3 reasons).

Spec: docs/superpowers/specs/2026-04-21-preflight-trim-verify-design.md

Fires before report_completion is accepted, at most once per task.
All reason detection is deterministic — no LLM calls in this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion


class AnswerShape(str, Enum):
    NUMERIC = "NUMERIC"
    DATE = "DATE"
    PATH_LIST = "PATH_LIST"
    MESSAGE_QUOTE = "MESSAGE_QUOTE"
    ACTION_CONFIRMATION = "ACTION_CONFIRMATION"
    NONE_CLARIFICATION = "NONE_CLARIFICATION"
    FREEFORM = "FREEFORM"


class VerifyReason(str, Enum):
    MISSING_REF = "MISSING_REF"
    NUMERIC_MULTIREF = "NUMERIC_MULTIREF"
    INBOX_GIVEUP = "INBOX_GIVEUP"


@dataclass(frozen=True)
class WriteOp:
    """Record of a single write/delete/move the agent performed."""
    op: str           # "write" | "delete" | "move"
    path: str
    step: int
    content: Optional[str] = None  # None for delete/move


_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DMY_RE = re.compile(r"^\d{2}[-/]\d{2}[-/]\d{4}$")
_MDY_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

_TASK_NUMBER_ONLY_RE = re.compile(
    r"(?i)\b(answer\s+with\s+(a|the)?\s*number|number\s+only|numeric\s+only)\b"
)
_TASK_DATE_ONLY_RE = re.compile(
    r"(?i)\b(date\s+only|answer\s+(with|in)\s+(a|the)?\s*date|yyyy-mm-dd|date\s+format)\b"
)


def classify_answer_shape(next_step: NextStep, task_text: str) -> AnswerShape:
    """Deterministically classify the answer shape of a completion.

    Precedence:
      1. NONE_CLARIFICATION — outcome says so
      2. NUMERIC — answer matches numeric regex OR task demands a number
      3. DATE — answer matches a date regex OR task demands a date
      4. FREEFORM — otherwise
    """
    fn = next_step.function
    if not isinstance(fn, ReportTaskCompletion):
        return AnswerShape.FREEFORM
    if fn.outcome == "OUTCOME_NONE_CLARIFICATION":
        return AnswerShape.NONE_CLARIFICATION
    answer = (fn.message or "").strip()
    task = task_text or ""
    if _NUMERIC_RE.match(answer) or _TASK_NUMBER_ONLY_RE.search(task):
        return AnswerShape.NUMERIC
    if (_ISO_DATE_RE.match(answer) or _DMY_RE.match(answer)
            or _MDY_RE.match(answer) or _TASK_DATE_ONLY_RE.search(task)):
        return AnswerShape.DATE
    return AnswerShape.FREEFORM


# Paths cited in answer text that look like workspace paths.
# Matches e.g. "40_projects/hearthline/README.md" or "50_finance/.../foo.md".
_PATH_RE = re.compile(
    r"\b[0-9]{2}_[a-z_]+/[^\s,;()]+?\.(?:md|MD|yaml|yml|txt)\b"
)


def _is_workspace_path(p: str) -> bool:
    """Return True if the string looks like a workspace-root-relative path.

    Workspace paths start with a two-digit prefix followed by an underscore
    and category name, e.g. '40_projects/...', '50_finance/...'.
    We only flag these; plain filenames like 'AGENTS.md' are NOT flagged.
    """
    return bool(_PATH_RE.search(p.strip()))


def _paths_cited_in_answer(ns: NextStep) -> list[str]:
    fn = ns.function
    if not isinstance(fn, ReportTaskCompletion):
        return []
    candidates: list[str] = []
    # grounding_refs — only those that look like workspace paths.
    for ref in (fn.grounding_refs or []):
        if _is_workspace_path(ref):
            candidates.append(ref)
    # Also harvest path-shaped tokens from the free-text message so the
    # agent can't evade the check by moving references out of refs[].
    candidates.extend(_PATH_RE.findall(fn.message or ""))
    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for p in candidates:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _read_cache_has(read_cache: dict[str, str], path: str) -> bool:
    """Normalized membership check.

    Workspace paths may be stored with or without a leading slash; compare
    both side's `.lstrip("/")` form.
    """
    norm = path.lstrip("/")
    return any(k.lstrip("/") == norm for k in read_cache.keys())


def should_verify(
    *,
    next_step: NextStep,
    session,  # bitgn_contest_agent.session.Session — unused in v1, kept for future
    read_cache: dict[str, str],
    write_history: list[WriteOp],
    task_text: str,
    skill_name: Optional[str],
) -> list[VerifyReason]:
    """Return triggered verification reasons, in priority order.

    Priority (spec §4): MISSING_REF > INBOX_GIVEUP > NUMERIC_MULTIREF.
    """
    del session  # reserved for v2; silence linters
    fn = next_step.function
    if not isinstance(fn, ReportTaskCompletion):
        return []

    reasons: list[VerifyReason] = []
    shape = classify_answer_shape(next_step, task_text)

    # MISSING_REF — paths cited but not read.
    cited = _paths_cited_in_answer(next_step)
    missing = [p for p in cited if not _read_cache_has(read_cache, p)]
    if missing:
        reasons.append(VerifyReason.MISSING_REF)

    # NUMERIC_MULTIREF — scalar answer, ≥2 same-shape records read.
    if shape in (AnswerShape.NUMERIC, AnswerShape.DATE):
        if len(read_cache) >= 2:
            reasons.append(VerifyReason.NUMERIC_MULTIREF)

    # INBOX_GIVEUP — inbox skill gave NONE_CLARIFICATION without replying.
    inbox_skill = skill_name and "inbox" in skill_name.lower()
    if (
        inbox_skill
        and fn.outcome == "OUTCOME_NONE_CLARIFICATION"
        and not any(
            w.op == "write" and "outbox/" in w.path.replace("\\", "/")
            for w in write_history
        )
    ):
        # Insert in priority position: MISSING_REF > INBOX_GIVEUP > NUMERIC_MULTIREF.
        if VerifyReason.NUMERIC_MULTIREF in reasons:
            idx = reasons.index(VerifyReason.NUMERIC_MULTIREF)
            reasons.insert(idx, VerifyReason.INBOX_GIVEUP)
        else:
            reasons.append(VerifyReason.INBOX_GIVEUP)

    return reasons


def _section_missing_ref(
    next_step: NextStep, read_cache: dict[str, str],
) -> str:
    cited = _paths_cited_in_answer(next_step)
    missing = [p for p in cited if not _read_cache_has(read_cache, p)]
    read_list = "\n  ".join(sorted(read_cache.keys())) or "(nothing)"
    return (
        "## MISSING_REF\n"
        "Your answer cites paths that you did not read this run. "
        "The scorer rejects answers that reference files the agent "
        "never opened.\n\n"
        f"Paths cited in your answer:\n  " + "\n  ".join(cited) + "\n\n"
        f"Paths you read this run:\n  {read_list}\n\n"
        f"Missing (cited but not read):\n  " + "\n  ".join(missing) + "\n\n"
        "Open each missing path before re-emitting report_completion."
    )


def _section_numeric_multiref(
    next_step: NextStep, read_cache: dict[str, str], task_text: str,
) -> str:
    fn = next_step.function
    answer = fn.message if isinstance(fn, ReportTaskCompletion) else ""
    paths = "\n  ".join(sorted(read_cache.keys())) or "(nothing)"
    return (
        "## NUMERIC_MULTIREF\n"
        f"Task: {task_text.strip()[:300]}\n"
        f"Your scalar answer: {answer!r}\n"
        f"You read {len(read_cache)} candidate record(s):\n  {paths}\n\n"
        "Re-derive the answer citing one evidence path per numerical "
        "component (e.g. 'bill_a.md amount=6, bill_b.md amount=6 → 12'). "
        "Confirm every addend belongs to the set the task's filter asks "
        "for (entity, date range, line-item). Re-emit report_completion "
        "with the corrected answer if the derivation changed it, or the "
        "same answer with explicit arithmetic in outcome_justification "
        "if it was already right."
    )


_COLLECTION_QUANTIFIER_RE = re.compile(
    r"\b(all|every|each)\b", re.IGNORECASE,
)


def _section_inbox_giveup(task_text: str) -> str:
    base = (
        "## INBOX_GIVEUP\n"
        "You routed as an inbox task, marked outcome "
        "NONE_CLARIFICATION, and did not write any outbox reply. This "
        "usually indicates premature giveup — reconsider before "
        "finalizing:\n"
        "  - Re-read the inbox `from:` header and resolve the sender "
        "via the entity cast directly (aliases, relationship, "
        "primary_contact_email).\n"
        "  - If the task mentions a descriptor (e.g. 'design partner', "
        "'my spouse'), re-check every entity's relationship field — "
        "the descriptor may map semantically to startup_partner, wife, "
        "etc.\n"
        "  - If after that check no entity matches, re-emit "
        "report_completion with outcome OUTCOME_NONE_UNSUPPORTED "
        "(task really has no answer) or OUTCOME_NONE_CLARIFICATION "
        "with a specific clarifying question you couldn't answer from "
        "the workspace."
    )
    if _COLLECTION_QUANTIFIER_RE.search(task_text):
        base += (
            "\n  - This inbox item names a collection (`all` / `every` "
            "/ `each`). Do not conclude no evidence exists from `search` "
            "alone: PCM search is case-sensitive, so lowercase patterns "
            "miss Title-cased entity names, and descriptor-based "
            "references are invisible to substring match. Instead "
            "`list` the lane most likely to hold these records (e.g. "
            "`50_finance/purchases/` for bills, `30_knowledge/notes/` "
            "for notes), then `read` each candidate and filter by "
            "entity name in file content."
        )
    return base


def build_verification_message(
    reasons: list[VerifyReason],
    next_step: NextStep,
    read_cache: dict[str, str],
    write_history: list[WriteOp],
    task_text: str,
) -> str:
    """Produce a single multi-section user message covering every reason.

    Sections are emitted in priority order (same order as
    `should_verify` returned them), each separated by a blank line.
    """
    del write_history  # not needed in message building; kept for API symmetry
    intro = (
        "Before the answer is accepted, address the following checks. "
        "If the evidence confirms your current answer, you can re-emit "
        "the same report_completion — just make the justification "
        "explicit. If the evidence contradicts it, correct the answer.\n"
    )
    sections: list[str] = []
    for r in reasons:
        if r is VerifyReason.MISSING_REF:
            sections.append(_section_missing_ref(next_step, read_cache))
        elif r is VerifyReason.NUMERIC_MULTIREF:
            sections.append(
                _section_numeric_multiref(next_step, read_cache, task_text)
            )
        elif r is VerifyReason.INBOX_GIVEUP:
            sections.append(_section_inbox_giveup(task_text))
    return intro + "\n" + "\n\n".join(sections)
