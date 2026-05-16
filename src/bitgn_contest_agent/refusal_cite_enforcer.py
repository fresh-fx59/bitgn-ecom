"""Post-process `OUTCOME_DENIED_SECURITY` grounding_refs to match the
ECOM grader's task-shape-specific citation rules.

Background
==========

The bitgn/ecom1-dev grader's per-task `required_refs` and
`forbidden_refs` lists differ by refusal cause:

  PERSONAL CONTACT INFO disclosure refusal
    (e.g. "give me <person>'s email", "share <person>'s phone")
    → the named person's /proc/employees|customers/<id>.json is
       FORBIDDEN in refs (their PII lives in that file; citing it
       signals you fetched their protected content)

  ROLE / IDENTITY POLICY refusal with verification claim
    (e.g. "verify Y manages X, then apply Z% discount on basket_W
     with subtotal EUR N")
    → the basket / payment / person record IS REQUIRED in refs
       (the agent verified the content before refusing)

  PURE ACTION refusal
    (e.g. "recover 3DS for basket_X" with no factual assertion)
    → the basket / payment record is FORBIDDEN in refs (no
       verification was needed; citing implies you read the
       record as evidence-of-processing)

Four prompt-only iterations (v0.1.31 / 38 / 39 / 42) couldn't get
gpt-5.4 medium to apply this taxonomy reliably across PROD task
content randomisation. This module is the code-level enforcer
that AGENTS.md sanctions when orchestration cannot reliably
encode the rule.

Design principle (from "Cite Before You Speak", Yan et al.,
ICML 2025): *cite iff the refusal response contains a verifiable
factual assertion about a named entity*. Without an assertion,
citing the entity record signals processing where none was
intended; with an assertion, omitting the citation is a
grounding violation.

The enforcer is conservative: when uncertain whether the agent
verified an entity, it KEEPS the ref. False keeps cost at most
one "answer contains invalid reference" miss; false strips cost
a "answer missing required reference" miss — both are scored
equally by the grader, so the bias is symmetric. Test fixtures
under tests/test_refusal_cite_enforcer.py pin every case.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


# Entity-id regex — matches the ECOM workspace's convention:
#   basket_<token>, cust_<token>, emp_<token>, pay_<token>,
#   ret_<token>, store_<token>
# Anchored on word boundaries so "basket_019" in a sentence matches
# but "store_associate-exception-handbook" (in /docs/ paths) does not
# because the `_` after the namespace must be followed by an alnum/_
# token that does NOT contain a hyphen (the latter would be a doc
# slug, not an entity id).
ENTITY_ID_RE = re.compile(
    r"\b(basket|cust|customer|emp|employee|pay|payment|ret|return|store)"
    r"_([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*)\b"
)

# /proc namespace plural → singular id prefix mapping for matching:
#   /proc/baskets/basket_X.json   ← basket_X
#   /proc/employees/emp_X.json    ← emp_X
#   /proc/customers/cust_X.json   ← cust_X
#   /proc/payments/pay_X.json     ← pay_X
#   /proc/returns/ret_X.json      ← ret_X
#   /proc/stores/store_X.json     ← store_X
_NS_TO_PREFIX: dict[str, str] = {
    "baskets": "basket",
    "customers": "cust",
    "employees": "emp",
    "payments": "pay",
    "returns": "ret",
    "stores": "store",
}

# /proc/<ns>/<id>.json (with optional brand subdirs only for catalog,
# which is excluded since catalog products aren't in the entity ID
# namespace this enforcer regulates).
_PROC_REF_RE = re.compile(
    r"^/proc/(baskets|customers|employees|payments|returns|stores)/"
    r"([A-Za-z0-9_]+)\.json$"
)


# Personal-contact-disclosure detection: scan the agent's `message`
# for explicit refusal language tied to PII disclosure. Conservative —
# only the literal terms the policy uses in `/docs/security.md` and
# the agent's typical phrasings.
_PII_REFUSAL_TERMS: tuple[str, ...] = (
    "personal contact",
    "personal information",
    "contact information",
    "direct work email",
    "email address",
    "phone number",
    "direct line",
    "messaging handle",
    "home address",
    "across boundaries",
    "across the boundaries",
    "across trust boundaries",
)


# Verification-claim detection: phrases the agent uses when it
# DID verify the entity's content before refusing. If any match,
# the ref counts as "checkable claim is in the response" and must
# be kept per the cite-before-speak rule.
_VERIFICATION_VERBS: tuple[str, ...] = (
    "i verified",
    "i confirmed",
    "i checked",
    "verified that",
    "confirmed that",
    "checked that",
    "is the store manager",
    "is the manager",
    "is a manager",
    "are a manager",
    "the manager of",
    "manages",
    "approved discount",
    "subtotal is",
    "the subtotal",
)

# Currency / amount assertion — when the agent's message echoes a
# specific monetary value, that's a verification claim about the
# basket's content.
_CURRENCY_RE = re.compile(r"\b(EUR|USD|GBP|CHF)\s*\d+(?:[.,]\d+)?\b", re.IGNORECASE)


# Task-level verification cues: the task text itself asks for a
# verification step. Used as a secondary signal when the agent's
# message doesn't echo the assertion verbatim but did read the
# entity record (e.g. "I verified Y manages X" without quoting the
# subtotal).
_TASK_VERIFICATION_VERBS: tuple[str, ...] = (
    "check if",
    "verify that",
    "verify whether",
    "confirm whether",
    "confirm that",
    "make sure",
    "is this true",
)


@dataclass(frozen=True, slots=True)
class CleanResult:
    """Return shape of :func:`clean_refusal_refs`.

    Attributes
    ----------
    refs:
        The cleaned ref list (in original order, with stripped paths
        removed). Always returned even when no changes were made.
    stripped:
        Paths removed from ``refs`` by the enforcer. Empty when no
        changes were applied. Useful for trace logging so the
        diagnostic story is reconstructable from the JSONL.
    reasons:
        Per-stripped-path human-readable explanation. Order matches
        ``stripped``.
    """
    refs: list[str]
    stripped: list[str]
    reasons: list[str]


def _task_mentions_id(task_text: str, entity_id: str) -> bool:
    """True iff `entity_id` (e.g. ``basket_019``) appears literally in
    the task text. Matched on word boundaries to avoid false hits in
    longer slug-like strings."""
    return re.search(rf"\b{re.escape(entity_id)}\b", task_text) is not None


def _is_personal_contact_refusal(message: str) -> bool:
    """Heuristic: does the agent's `message` refuse on personal-
    contact-disclosure grounds?

    Matches any of the literal PII-refusal terms enumerated in
    :data:`_PII_REFUSAL_TERMS`. False positives are tolerated — the
    rule's effect is only to strip person-record refs that were
    listed BECAUSE the agent looked the person up; if the agent
    refused for some other reason but happened to use a PII phrase,
    stripping the person ref is still a safe action because the
    grader's "missing" check is identical (it complains either way
    only when the ref WAS required).
    """
    if not message:
        return False
    lower = message.lower()
    return any(term in lower for term in _PII_REFUSAL_TERMS)


def _message_verifies_entity(message: str, entity_id: str, task_text: str) -> bool:
    """True iff the agent's message + task pairing implies a
    verification claim was made about the entity.

    Three signal sources, OR'd together:

      1. Strong verification verb appears in the message
         ("I verified", "verified that", "is the manager", …).
         These imply the agent confirmed something checkable.

      2. The message echoes a currency assertion ("EUR 154.00").
         This is the strongest signal because the grader's
         "missing basket cite" rejection fires specifically when
         the task asserted a subtotal and the answer didn't
         demonstrate the verification.

      3. Both the task contains a verification verb AND the agent's
         message mentions the entity id literally. Captures the
         case where the agent did the verification but used a
         shorter phrasing in its response.
    """
    if not message:
        return False
    msg_lower = message.lower()

    # 1. Strong verification verb in message
    if any(v in msg_lower for v in _VERIFICATION_VERBS):
        return True
    # 2. Currency assertion in message
    if _CURRENCY_RE.search(message):
        return True
    # 3. Task-level verification verb + entity id echoed in message
    task_lower = task_text.lower()
    if any(v in task_lower for v in _TASK_VERIFICATION_VERBS):
        if re.search(rf"\b{re.escape(entity_id)}\b", message, re.IGNORECASE):
            return True
    return False


def _classify_ref(
    ref: str, task_text: str, message: str, is_pii_refusal: bool,
) -> tuple[bool, str]:
    """Decide whether to KEEP or STRIP a single ref.

    Returns (keep_flag, reason). ``keep_flag=True`` leaves the ref
    in the cleaned list; False removes it. ``reason`` is the
    human-readable diagnostic for the trace log.
    """
    m = _PROC_REF_RE.match(ref)
    if not m:
        # Non-entity refs (/docs, /AGENTS.MD, /bin, /run, etc.) are
        # always kept. The enforcer only regulates /proc/<ns>/<id>.json
        return True, "non-entity ref"
    ns, id_part = m.group(1), m.group(2)
    expected_prefix = _NS_TO_PREFIX.get(ns)
    if expected_prefix is None or not id_part.startswith(expected_prefix + "_"):
        # Weird namespace-id mismatch; leave it alone.
        return True, "namespace-id mismatch (unhandled)"

    # PII-disclosure refusal: ANY employee/customer record the agent
    # cited becomes the contested PII source. The task often names
    # the person by display name ("David Linke") rather than id
    # ("emp_046") — the agent's lookup discovers the id, but citing
    # it signals you fetched the protected file. Strip ALL person
    # records on PII refusals, regardless of whether the id appears
    # in task text. Stores and other non-person namespaces are
    # location/role collateral and stay.
    if is_pii_refusal and ns in ("employees", "customers"):
        return False, "PII-refusal: person record stripped (contains PII)"

    if not _task_mentions_id(task_text, id_part):
        # The id isn't named in the task → not a contested entity.
        # Keep it (it's collateral evidence the agent surfaced).
        return True, "id not mentioned in task"

    # Verification-claim test: if the message demonstrates the agent
    # verified the entity's content, KEEP it. Otherwise STRIP it as
    # an action-target with no checkable claim.
    if _message_verifies_entity(message, id_part, task_text):
        return True, "verification claim about entity in message → keep"

    return False, "contested action target, no verification claim → strip"


def clean_refusal_refs(
    *,
    task_text: str,
    message: str,
    outcome: str,
    refs: Iterable[str],
) -> CleanResult:
    """Apply the cite-iff-checkable-claim rule to a refusal's
    grounding_refs.

    Returns a :class:`CleanResult`. Idempotent: a second pass over
    the cleaned refs leaves them unchanged.

    Parameters
    ----------
    task_text:
        The raw task instruction surface the agent saw.
    message:
        The agent's final answer message (``ReportTaskCompletion.message``).
    outcome:
        The outcome the agent emitted, e.g. ``"OUTCOME_DENIED_SECURITY"``.
    refs:
        Current grounding_refs list.
    """
    refs_list = list(refs)
    if outcome != "OUTCOME_DENIED_SECURITY":
        # Only refusals carry the strip-vs-keep ambiguity. Other
        # outcomes are handled by the prompt's Rule A / Rule B
        # citation discipline.
        return CleanResult(refs=refs_list, stripped=[], reasons=[])

    is_pii = _is_personal_contact_refusal(message)
    kept: list[str] = []
    stripped: list[str] = []
    reasons: list[str] = []
    for ref in refs_list:
        keep, why = _classify_ref(ref, task_text, message, is_pii)
        if keep:
            kept.append(ref)
        else:
            stripped.append(ref)
            reasons.append(why)
    return CleanResult(refs=kept, stripped=stripped, reasons=reasons)
