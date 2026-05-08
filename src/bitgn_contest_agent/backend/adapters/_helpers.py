"""Salvage helpers usable by concrete adapters.

Each helper accepts a content ``str`` and returns ``NextStep | None``. All
guards are preserved from the pre-adapter ``_try_salvage_from_content`` — see
docs/superpowers/specs/2026-04-19-local-model-adapters-design.md §6.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional, Sequence, Tuple

from pydantic import ValidationError

from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion

if TYPE_CHECKING:
    from bitgn_contest_agent.session import Session

_LOG = logging.getLogger(__name__)

# Tokens that unambiguously indicate chat-template leakage or mid-reasoning
# output rather than a clean bare answer. Any match → refuse salvage.
# Covers: harmony channels, qwen/im tags, think-block tags, backticked
# code fences, and the `</tool_call>` fragment that caused the 2026-04-19
# GLM score=0 incident.
_UNSAFE_BARE_TOKENS: Tuple[str, ...] = (
    "<|", "</", "```",
    "<think>", "<think/>",
    "tool_call", "function_call",
)

# Lowercased substrings that suggest the model is still narrating its
# work, not terminating. If any are present the content is NOT a final
# answer and salvage must return None so the critique/retry path runs.
_BARE_ANSWER_CONTINUATION_MARKERS: Tuple[str, ...] = (
    "let me ", "let's ", "i need ", "i should ",
    "i'll ", "i will ", "first, ", "next, ",
    "next step", "thinking", "analysis:", "plan:",
)


def _sanitize_grounding_refs(merged: dict) -> None:
    """Strip non-path junk from ``merged["grounding_refs"]`` in-place.

    Envelope-salvage paths accept whatever the model emitted inside
    ``function.grounding_refs``. Local models occasionally pad the array
    with free-text tokens — the 2026-04-22 gpt-oss-120b PROD run saw
    t103 emit ``["AGENTS.MD", "...bill.md", "5", "5", "", "", ""]``.
    The grounding_ref validator then rejects the whole terminal on the
    junk tokens ("grounding_ref '5' never successfully read"), losing an
    otherwise-valid answer.

    Drop entries that are not strings, empty/whitespace, shorter than 3
    chars after strip, or contain neither ``/`` (path separator) nor
    ``.`` (extension). The remaining list is still validated later by
    ``verify.py`` against the actual read-success set.
    """
    refs = merged.get("grounding_refs")
    if not isinstance(refs, list):
        return
    cleaned = []
    for r in refs:
        if not isinstance(r, str):
            continue
        s = r.strip()
        if len(s) < 3:
            continue
        if "/" not in s and "." not in s:
            continue
        cleaned.append(s)
    merged["grounding_refs"] = cleaned


def try_gpt_oss_full_chain(content: str) -> Optional[NextStep]:
    """Delegate to the legacy ``_try_salvage_from_content``.

    Preserves byte-identical behavior for gpt-oss-20b (harmony → bare-name-
    arguments → envelope → envelope-terminal → bare-value) so the existing
    test corpus still passes. Consolidating into module-level functions is
    the next refactor; do it when we need another adapter that composes a
    subset of these branches beyond LFM2's bare-name-arguments case.
    """
    from bitgn_contest_agent.backend.openai_toolcalling import (
        _try_salvage_from_content,
    )

    return _try_salvage_from_content(content)


def try_bare_name_arguments(content: str) -> Optional[NextStep]:
    """Parse ``{"name": "<tool>", "arguments": {...}}`` from a content body.

    LFM2 is trained on the bare OpenAI tool-call shape and emits it as
    free text when the server doesn't honor ``tool_choice="required"``.
    Returns ``None`` for any other shape or on schema validation failure.
    """
    from bitgn_contest_agent.backend.openai_toolcalling import (
        _VALID_TOOL_NAMES,
        _build_next_step,
        _extract_first_json_object,
    )

    if not content:
        return None
    obj = _extract_first_json_object(content)
    if obj is None:
        return None
    name = obj.get("name")
    args = obj.get("arguments")
    if not isinstance(args, dict) or name not in _VALID_TOOL_NAMES:
        return None
    try:
        return _build_next_step(name, args)
    except ValidationError:
        return None


def try_envelope(content: str) -> Optional[NextStep]:
    """Parse the NextStep envelope shape from content body.

    Shape: ``{"current_state": ..., "function": {"tool": ..., ...}, ...}``.
    Observed on GLM-4.7-Flash when it declines ``tool_choice="required"``
    and emits the structured envelope as free-text content instead.

    Safe against chat-template leakage: requires a parseable JSON object
    with a ``function.tool`` that matches a registered tool name. Empty-
    string placeholder injection for ``rulebook_notes`` /
    ``outcome_justification`` / ``message`` preserves the guard from the
    pre-adapter envelope branch.

    Falls back to envelope-terminal synthesis when the envelope has a
    terminal ``outcome_leaning`` but no ``function`` key.
    """
    from bitgn_contest_agent.backend.openai_toolcalling import (
        _ENVELOPE_FIELDS,
        _VALID_TOOL_NAMES,
        _build_next_step,
        _extract_first_json_object,
        _maybe_salvage_envelope_terminal,
    )

    if not content:
        return None
    obj = _extract_first_json_object(content)
    if obj is None:
        return None
    if "function" in obj and isinstance(obj["function"], dict):
        func = obj["function"]
        tool_name = func.get("tool")
        if tool_name in _VALID_TOOL_NAMES:
            merged = {}
            for key in _ENVELOPE_FIELDS:
                if key in obj:
                    merged[key] = obj[key]
            for key, val in func.items():
                if key != "tool":
                    merged[key] = val
            for placeholder in ("rulebook_notes", "outcome_justification", "message"):
                if merged.get(placeholder) == "":
                    merged[placeholder] = "—"
            _sanitize_grounding_refs(merged)
            try:
                return _build_next_step(tool_name, merged)
            except ValidationError:
                return None
    return _maybe_salvage_envelope_terminal(obj)


def try_qwen_bare_answer(content: str) -> Optional[NextStep]:
    """Salvage a short bare-text content as a terminal report_completion.

    Evidence (2026-04-19 qwen3.5-35b-a3b PROD run): 12 cases where qwen
    emitted the final answer as plain text instead of a tool_call —
    numbers ("1170", "650", "380"), dates ("03-02-2026"), short file-
    path lists. 6 of those tasks failed because the content was not
    salvaged; the circuit breaker eventually fired OUTCOME_NONE_UNSUPPORTED.

    This helper synthesizes a ``report_completion(OUTCOME_OK)`` with the
    stripped content verbatim as ``message``. The grader decides correctness —
    a wrong bare answer still fails, but a right one passes instead of
    being lost to circuit-breaker synthesis.

    Guards (ALL must pass, else return None so the critique loop runs):
      - content is non-empty after strip
      - stripped length ≤ 500 chars (long enough for a short file list;
        too short for mid-exploration prose)
      - no JSON/array prefix (``{`` / ``[``): envelope/name-arguments
        salvage handles those shapes
      - no ``_UNSAFE_BARE_TOKENS`` (chat-template leakage guard — this is
        the 2026-04-19 GLM score=0 rule, inherited)
      - no ``_BARE_ANSWER_CONTINUATION_MARKERS`` (model is narrating,
        not terminating)

    Wired only from ``QwenA3bAdapter``. Other adapters must not chain
    this — GLM's content is chat-template leakage (never a real answer),
    and gpt-oss already owns the legacy bare-value branch with a
    different (tighter) guard set.
    """
    from bitgn_contest_agent.backend.openai_toolcalling import _build_next_step

    if not content:
        return None
    stripped = content.strip()
    if not stripped or len(stripped) > 500:
        return None
    if stripped[0] in "{[":
        return None
    if any(tok in stripped for tok in _UNSAFE_BARE_TOKENS):
        return None
    lowered = stripped.lower()
    if any(tok in lowered for tok in _BARE_ANSWER_CONTINUATION_MARKERS):
        return None
    try:
        ns = _build_next_step(
            "report_completion",
            {
                "message": stripped,
                "grounding_refs": [],
                "rulebook_notes": "—",
                "outcome_justification": "—",
                "completed_steps_laconic": [],
                "outcome": "OUTCOME_OK",
                "outcome_leaning": "OUTCOME_OK",
            },
        )
    except ValidationError:
        return None
    _LOG.info(
        "qwen_bare_answer_salvage: synthesized OUTCOME_OK from bare "
        "content=%r (len=%d)",
        stripped[:120],
        len(stripped),
    )
    return ns


# ----------------------------------------------------------------------
# Gpt-oss per-model behavioral helpers (shared by GptOssAdapter and
# GptOssRemoteAdapter). See 2026-04-23 PROD analysis for evidence.
# ----------------------------------------------------------------------

# Phrases that indicate an inbox-processing task but that the global
# tier1 regex for inbox-processing does not catch. Observed 2026-04-23
# gpt-oss-120b PROD run (v0.1.24): 4 tasks (t014/t021/t046/t072) were
# routed to inbox-security only, R7 never fired, all 4 failed with
# "missing file delete". Keep lowercase; matching is case-insensitive.
_GPT_OSS_INBOX_PROCESSING_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\binbound\s+note\b", re.IGNORECASE),
    re.compile(r"\binvoice[-\s]request\b", re.IGNORECASE),
    re.compile(r"\bbundle[-\s]request\b", re.IGNORECASE),
    re.compile(r"\bnext\s+inbox\s+item\b", re.IGNORECASE),
    re.compile(r"\bprocess\s+the\s+next\b.*\binbox\b", re.IGNORECASE),
    re.compile(r"\bact\s+on\s+(?:it|the\s+next)\b", re.IGNORECASE),
)


def gpt_oss_extra_reactive_skills(task_text: str) -> frozenset[str]:
    """Return additional skills to load for gpt-oss when tier1 regex misses.

    Currently adds ``inbox-processing`` when the task text matches any of
    ``_GPT_OSS_INBOX_PROCESSING_PATTERNS``. Returns an empty set on no match
    so the loader injects nothing.
    """
    if not task_text:
        return frozenset()
    for pat in _GPT_OSS_INBOX_PROCESSING_PATTERNS:
        if pat.search(task_text):
            return frozenset({"inbox-processing"})
    return frozenset()


def gpt_oss_filter_hallucinated_refs(
    fn: ReportTaskCompletion,
    session: "Session",
) -> ReportTaskCompletion:
    """Drop grounding_refs the agent never read (success or verified-absent).

    Gpt-oss-120b frequently cites paths it never actually opened, making
    R1_UNSEEN_REF fire and the whole terminal get rejected. The
    structural sanitizer in ``_sanitize_grounding_refs`` only removes
    obvious junk (short strings, tokens without path separators); real-
    looking hallucinated paths pass through.

    2026-04-23 v0.1.24 PROD evidence: 15 R1 events across 7 tasks.
    Filtering hallucinated refs at adapter boundary lets the rest of the
    terminal survive — judge correctness is decided on ``message``,
    not on ``grounding_refs``.

    Case-insensitive match against ``session.seen_refs`` ∪
    ``session.verified_absent``. Empty result list is fine; R1 only
    rejects when cited refs were never read, never when the list is empty.
    """
    refs = getattr(fn, "grounding_refs", None) or []
    if not refs:
        return fn
    seen_lower = {r.lower() for r in session.seen_refs}
    absent_lower = {r.lower() for r in session.verified_absent}
    kept = [r for r in refs if r.lower() in seen_lower or r.lower() in absent_lower]
    if len(kept) == len(refs):
        return fn
    dropped = [r for r in refs if r not in kept]
    _LOG.info(
        "gpt_oss_filter_hallucinated_refs: dropped %d of %d refs (%r)",
        len(dropped),
        len(refs),
        dropped[:5],
    )
    # ``ReportTaskCompletion`` is a pydantic model — ``model_copy`` is
    # the supported clone-with-update path. Avoids silent validation
    # bypass that a bare attribute assignment would cause on frozen
    # configs.
    return fn.model_copy(update={"grounding_refs": kept})


# Rule codes that map cleanly to imperative, tool-call-shaped nudges.
# Ordered: the first matching reason wins when multiple are present.
# Everything else falls through to the default descriptive critique.
#
# Originally tuned for gpt-oss (descriptive critiques re-worded
# justification instead of changing tool choice). The same pattern hits
# qwen3.6 — 2026-05-01 qwen3.6/neuraldeep PROD run: 4× R7_INBOX_CLEANUP,
# 2× R6_MUTATION_DISCIPLINE, 2× R0_MIN_EXPLORE all terminated via
# submit_anyway after re-emitting the same shape. Reusable across any
# instruction-following local model.
_GPT_OSS_IMPERATIVE_RULES: Tuple[Tuple[str, str], ...] = (
    (
        "R7_INBOX_CLEANUP",
        "Your previous report_completion was rejected because you did not "
        "delete the consumed inbox file.\n"
        "Your NEXT tool_call MUST be exactly:\n"
        "    function.tool = \"delete\"\n"
        "    function.path = \"<the inbox trigger file you read at task start, "
        "e.g. '00_inbox/000_*.md'>\"\n"
        "Do NOT emit report_completion again until AFTER that delete has "
        "succeeded. Re-emit report_completion only in the turn AFTER the "
        "delete tool_result arrives."
    ),
    (
        "R0_MIN_EXPLORE",
        "Your previous report_completion was rejected because you tried to "
        "finish too early.\n"
        "Your NEXT tool_call MUST NOT be report_completion. Instead, take at "
        "least one concrete exploration step (tree, read, list, or search) "
        "that materially progresses the task. Only after at least 3 total "
        "steps may you consider terminating."
    ),
    (
        "R6_MUTATION_DISCIPLINE",
        "Your previous report_completion was rejected because you mutated "
        "files (write/delete/move) while still in GATHERING_INFORMATION. "
        "Mutations must only happen AFTER the task plan is clear and the "
        "outcome is known.\n"
        "If the task is read-only (a question that just needs an answer): "
        "your NEXT tool_call MUST be a fresh ``report_completion`` with "
        "outcome=OUTCOME_OK and no further mutations.\n"
        "If the task genuinely requires mutations: your NEXT tool_call MUST "
        "advance the actual mutation plan (e.g. write to the correct target "
        "path) — do NOT mutate exploratory scratch files. Re-read the task "
        "prompt before acting."
    ),
    (
        "grounding_ref",  # substring match — covers R1 reason phrasings
        "Your previous report_completion was rejected because one or more "
        "grounding_refs cite files you never actually read.\n"
        "Your NEXT tool_call MUST be either:\n"
        "  (a) a ``read`` tool call on one of the cited-but-unread paths, OR\n"
        "  (b) a new ``report_completion`` whose grounding_refs list ONLY "
        "      contains paths you have already read successfully in this task.\n"
        "Do not invent paths. If you are not sure a path exists, call ``read`` "
        "on it first."
    ),
    (
        "outbox attachment",  # R5 phrasing
        "Your previous report_completion was rejected because an outbox "
        "email attachment was never read.\n"
        "Your NEXT tool_call MUST be a ``read`` on the unread attachment "
        "path. Only after the read succeeds may you re-emit "
        "report_completion."
    ),
)


def gpt_oss_format_retry_critique(reasons: Sequence[str]) -> str:
    """Rewrite validator-rejection feedback as imperative tool-call prescriptions.

    Gpt-oss (both 20b and 120b) treats descriptive "the file must be
    removed" phrasing as narrative correction and rewords its
    ``outcome_justification`` instead of changing tool choice.
    2026-04-23 v0.1.24 PROD run: 15 inbox tasks hit R7 twice, emitted
    zero delete ops, and terminated via ``submit_anyway``.

    This helper matches each reason against ``_GPT_OSS_IMPERATIVE_RULES``
    and, when it hits, returns an imperative prescription naming the
    exact next tool_call. Falls back to the generic ``critique_injection``
    so non-matching rejections (ValidationError, mutation integrity,
    leaning mismatch) still see the default guidance.
    """
    from bitgn_contest_agent.prompts import critique_injection

    if not reasons:
        return critique_injection(list(reasons))

    # Find the first reason that matches an imperative rule. If none,
    # fall back to the generic critique untouched.
    imperative_body: Optional[str] = None
    matched_code: Optional[str] = None
    for reason in reasons:
        for code, body in _GPT_OSS_IMPERATIVE_RULES:
            if code in reason:
                imperative_body = body
                matched_code = code
                break
        if imperative_body is not None:
            break
    if imperative_body is None:
        return critique_injection(list(reasons))

    # Still surface the full reason list so the model sees any other
    # rejections; just put the imperative prescription on top where
    # gpt-oss will weight it correctly.
    others = [r for r in reasons if matched_code not in r]
    trailer = ""
    if others:
        body = "\n".join(f"  - {r}" for r in others)
        trailer = (
            "\n\nAdditional validator reasons (address each before your "
            f"next report_completion):\n{body}"
        )
    return imperative_body + trailer
