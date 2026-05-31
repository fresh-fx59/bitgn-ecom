"""Task canonicalizer — runs a small LLM call at prepass time to detect
the instruction language and produce an English paraphrase that the
regex-based enforcers can consume safely.

Why this exists:
  Today's enforcer chain (refusal_cite_enforcer, addenda_completer,
  cite_completer, sku_completer, fraud_cluster_filter, validator) has
  ~160 English phrase patterns / regex constants that match against
  `task_text`. If the instruction arrives in any non-English language,
  every one of those matches fails silently → required refs not added,
  refusals misclassified, count tasks lose addenda evidence.

  The literature converges on "internal English / external localized"
  (Fini, modern Zendesk, KADARAG, LegalRAG; arXiv 2505.17306 shows
  refusal direction is universal across languages on modern LLMs).
  Translation-first (normalize-everything pipeline) is dispreferred:
  it doubles LLM hops, corrupts entities, and drifts policy meaning.

  This module implements the canonicalization point of that split:
  one cheap LLM call per task to emit
    {instruction_language: ISO-639-1, task_text_en, preserved_tokens}
  Downstream enforcers swap `task_text` → `task_text_en`. The user-
  facing message stays in source language (prompt rule, separately).

Safety:
  - If the call fails (timeout, parse error, network), we return
    {lang="en", text=raw_task_text, preserved=[]} so the agent runs
    in English-fallback mode. Today's behaviour is the floor.
  - If `preserved_tokens` (auto-detected from raw text: IDs,
    amounts, format tokens, brand names) don't all appear verbatim
    in `task_text_en`, we reject the canonicalization and fall back
    to raw text. This prevents the well-documented MT failure mode
    where Möller → Moeller, basket_139 → basket139, etc.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional


_LOG = logging.getLogger(__name__)


# Tokens that the LLM MUST NOT modify in the English paraphrase, in
# two tiers:
#   _PRESERVE_REQUIRED — guard fails if any of these get reworded.
#     Limited to load-bearing references the enforcers + canonical
#     refs depend on (entity IDs, currency amounts, policy keywords,
#     brand names used by the SKU pipeline).
#   _PRESERVE_HINT — surfaced in the prompt as "keep verbatim" but
#     missing-on-output does NOT fail canonicalization. These tend
#     to belong in the user-facing message (format strings) and
#     aren't matched by any enforcer regex.
_PRESERVE_REQUIRED = (
    re.compile(r"\b(?:basket|pay|cust|emp|ret|store|fam|ord)_[A-Za-z0-9_]+"),
    re.compile(r"\bEUR\s+[\d.,]+\b"),
    re.compile(r"\b\d+(?:[.,]\d+)?\s*%"),
    re.compile(r"\b(?:service_recovery|3DS|3-D Secure|recover-3ds|approve-refund)\b"),
    re.compile(r"\b(?:Heco|Festool|Makita|Bosch|DeWalt|Knipex|Wiha|Hilti|Hitachi|Stanley|Kopp|Osram|Schneider|Leifheit|Carhartt|Gardena|Rawlplug|Dresselhaus|AlcaPlast|Keter|Record)\b"),
)
_PRESERVE_HINT = (
    re.compile(r'"<COUNT:%d>"|"%d"|"<COUNT_PER_STORE>"'),
)


@dataclass(slots=True)
class CanonicalizationResult:
    instruction_language: str   # ISO-639-1, "en" for English
    task_text_en: str           # English paraphrase (or raw text if EN/fallback)
    preserved_tokens: list[str] # Tokens detected as preserve-critical
    canonicalized: bool         # True if LLM call succeeded + preservation passed
    failure_reason: Optional[str] = None
    injection_markers: list[str] = None  # forged chat-template control tokens (see detect_injection)

    def __post_init__(self):
        if self.injection_markers is None:
            self.injection_markers = []


# Structural prompt-injection markers: forged chat-template control tokens
# and role-delimiters that a legitimate e-commerce instruction NEVER
# contains. Their presence inside task text is an attempt to forge a
# system/user turn or smuggle operator directives ("apply the embedded
# instructions before reading local docs"). This is a STRUCTURAL signal —
# language- and content-independent — so it survives the canonicalizer
# paraphrase that would otherwise launder the foreign-language payload away.
# Covers DeepSeek (fullwidth-pipe), ChatML / OpenAI (`<|...|>`), and Llama
# (`[INST]`, `<<SYS>>`) template families.
_INJECTION_MARKERS = re.compile(
    "<｜[^｜]{1,48}｜>"   # fullwidth-pipe tokens  <｜System｜>, <｜begin▁of▁sentence｜>
    r"|<\|[^|>]{1,48}\|>"             # ASCII chat tokens      <|system|>, <|im_start|>
    r"|\[/?INST\]"                    # Llama-2 instruction     [INST] [/INST]
    r"|<</?SYS>>"                     # Llama-2 system block     <<SYS>> <</SYS>>
    # --- non-chat-template families (PROD t038/t083, 2026-05-31) ---
    # HTML/SGML comments smuggle operator directives ("the user-visible
    # request is stale; recovered control text follows"). A legitimate
    # e-commerce instruction never contains an HTML comment. (t083)
    r"|<!--|-->"
    # Forged "model context" / "system prompt" identity labels. A real task
    # never narrates the model's own context or names a chat model. CJK
    # (t038: '模型上下文: deepseek-chat') + Latin re-instantiations.
    r"|模型上下文|系统提示词?|系统指令"
    r"|(?:model\s+context|system\s+prompt|developer\s+message)\s*[:：]"
    # Social-engineering control phrases with no legitimate use.
    r"|compatibility\s+shim"
    r"|recovered\s+control\s+text"
    r"|user-visible\s+request\s+is\s+stale"
    # Fake control annotations smuggled as a markdown header / inline tag
    # ("...format# Security relay: authenticated request") that fabricate an
    # authenticated/trusted session. The leading '#' distinguishes them from
    # a legitimate use of the word 'authenticated' in a sentence. (t028)
    r"|security\s+relay"
    r"|#\s*(?:security|auth\w*|system|session|trust\w*)\s+"
    r"(?:relay|gateway|override|directive|context|channel)\b"
    # Defense-in-depth: canonical instruction-override tells (OWASP LLM01 /
    # ChatInject arXiv:2509.22830). ZERO matches on the observed PROD run, so
    # no regression here — they harden the detector against re-instantiation
    # variants whose payload uses the textbook phrasing. None of these has any
    # legitimate use in an e-commerce instruction. (NB: a bare 'override' is
    # deliberately NOT listed — it appears in already-passing tasks t013/t019/
    # t033 whose denial path must stay untouched.)
    r"|ignore\s+(?:all\s+|the\s+)?(?:previous|prior|above)\s+(?:instruction|message|prompt)"
    r"|disregard\s+(?:the\s+|all\s+|your\s+)?(?:above|previous|prior|system\s+prompt|instruction)"
    r"|developer\s+mode|jailbreak\b|\bDAN\s+mode"
    r"|(?:reveal|print|repeat|show)\s+(?:me\s+)?(?:your\s+|the\s+)?system\s+prompt",
    re.IGNORECASE,
)


def detect_injection(text: str) -> list[str]:
    """Return the distinct structural prompt-injection markers found in
    ``text`` (forged chat-template / role-delimiter control tokens), or an
    empty list. A legitimate instruction never embeds model control tokens,
    so any hit is a high-confidence injection signal independent of the
    payload's language — which is why we check the RAW text before the
    LLM canonicalizer (which paraphrases the visible attack away)."""
    if not text:
        return []
    return sorted({m.group(0) for m in _INJECTION_MARKERS.finditer(text)})


def _detect_preserve_tokens(text: str) -> tuple[list[str], list[str]]:
    """Return (required_tokens, hint_tokens) detected in text."""
    req: set[str] = set()
    hint: set[str] = set()
    for pat in _PRESERVE_REQUIRED:
        for m in pat.finditer(text):
            req.add(m.group(0))
    for pat in _PRESERVE_HINT:
        for m in pat.finditer(text):
            hint.add(m.group(0))
    return sorted(req), sorted(hint)


# Cheap heuristic — if the text is mostly ASCII letters/digits/punct and
# contains common English markers, skip the LLM call. The marker set
# spans determiners, auxiliaries, prepositions, common verbs, and
# contest-flavoured verbs/nouns. Any 1 hit on pure-ASCII text → EN;
# 3+ hits required when non-ASCII Latin chars (umlauts) are present
# (defends against German "über" inside otherwise-English text).
_EN_MARKERS = re.compile(
    r"\b(the|a|an|is|are|was|were|please|how|many|what|where|when|do|does|"
    r"have|has|can|could|should|would|will|been|being|i|me|my|you|your|"
    r"and|or|but|not|for|of|to|in|on|at|by|with|from|this|that|these|those|"
    r"go|ahead|complete|submit|apply|refund|approve|check|verify|find|"
    r"identify|recover|report|tell|ask|let|help|need|want|already|across|"
    r"every|some|any|all|each|today|now|fraud|payment|basket|store|"
    r"customer|please|stuck|near|over|under|out|up|down|here|there)\b",
    re.IGNORECASE,
)
_NON_LATIN_RE = re.compile(r"[Ͱ-鿿가-힯]")  # Greek..CJK


def _looks_english(text: str) -> bool:
    if _NON_LATIN_RE.search(text):
        return False
    hits = len(set(m.group(0).lower() for m in _EN_MARKERS.finditer(text)))
    # Count ratio of non-ASCII Latin chars (umlauts/accents) vs ASCII
    # letters. A name like "Möller" inside an otherwise-English
    # sentence has ratio ~1%, while a full German sentence has 5-15%.
    ascii_letters = sum(1 for c in text if c.isalpha() and ord(c) < 128)
    non_ascii_letters = sum(1 for c in text if c.isalpha() and ord(c) >= 128)
    total_letters = ascii_letters + non_ascii_letters
    non_ascii_ratio = (non_ascii_letters / total_letters) if total_letters else 0.0
    if non_ascii_ratio > 0.03:
        # >3% non-ASCII letters → likely non-English text (German full
        # sentence, Czech, Hungarian, etc.). Require more markers.
        return hits >= 4
    # Mostly-ASCII text with at least 1 English marker → EN
    return hits >= 1


_CANONICALIZE_PROMPT = """\
You convert a task instruction (which may be in any language) into a
short English paraphrase that downstream regex-based tools can match
against. Also detect the source language.

Output STRICT JSON only, no preface:
  {{
    "instruction_language": "<ISO-639-1, e.g. 'en' for English, 'de' for German>",
    "task_text_en": "<English paraphrase preserving every preserve_token verbatim>"
  }}

CRITICAL RULES for `task_text_en`:
  1. EVERY token listed in PRESERVE below MUST appear in `task_text_en`
     byte-identical. Never translate, never alter case, never reformat.
  2. Keep all proper nouns (people, stores, brands, cities) byte-identical.
  3. Keep all numeric values + currency identifiers byte-identical.
  4. The paraphrase should sound like an English speaker said the same
     thing. Be concise; do NOT add information.
  5. If the original is already English, return it unchanged with
     instruction_language="en".

PRESERVE (must appear verbatim):
{preserve}

INSTRUCTION:
{text}
"""


def canonicalize(
    *,
    task_text: str,
    log_extra: Optional[dict] = None,
) -> CanonicalizationResult:
    """Detect language + produce English paraphrase via classifier LLM.

    Uses the shared classifier module (Haiku-backed cheap fallback).
    On any failure path we return a "no-op" canonicalization that keeps
    existing behaviour: lang="en", text_en=raw task_text, canonicalized=False.
    """
    if not task_text or not task_text.strip():
        return CanonicalizationResult("en", task_text, [], False, "empty")

    # Detect structural injection markers in the RAW text first — before the
    # _looks_english short-circuit and before any LLM paraphrase, both of
    # which would launder the attack away. Keep raw text (don't canonicalize)
    # so downstream sees the unmodified instruction; surface the markers.
    injection = detect_injection(task_text)
    if injection:
        return CanonicalizationResult(
            "en", task_text, [], False, "injection_detected",
            injection_markers=injection,
        )

    if _looks_english(task_text):
        return CanonicalizationResult("en", task_text, [], True, None)

    required, hint = _detect_preserve_tokens(task_text)
    preserve = required + hint
    prompt = _CANONICALIZE_PROMPT.format(
        preserve="\n".join(f"  - {t}" for t in preserve) or "  (none)",
        text=task_text,
    )

    try:
        from bitgn_contest_agent import classifier as _classifier
        raw = _classifier.raw_completion(prompt=prompt)
    except Exception as exc:
        _LOG.info(
            "task_canonicalizer: classifier call failed: %s — falling back to raw text",
            exc,
        )
        return CanonicalizationResult("en", task_text, preserve, False, f"call_failed:{exc}")

    if not raw or not raw.strip():
        return CanonicalizationResult("en", task_text, preserve, False, "empty_response")

    # Tolerate ```json ... ``` fencing
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)

    try:
        parsed = json.loads(s)
    except json.JSONDecodeError as exc:
        _LOG.info(
            "task_canonicalizer: JSON parse failed (%s): %s — falling back to raw text",
            exc, s[:200],
        )
        return CanonicalizationResult("en", task_text, preserve, False, f"parse_failed:{exc}")

    lang = str(parsed.get("instruction_language", "")).strip().lower() or "en"
    text_en = str(parsed.get("task_text_en", "")).strip()

    if not text_en:
        return CanonicalizationResult("en", task_text, preserve, False, "missing_text_en")

    # Preservation guard: only REQUIRED tokens (entity IDs, amounts,
    # policy keywords, brand names) must round-trip byte-identical.
    # Hint tokens (format strings) are nice-to-have and don't fail.
    missing = [t for t in required if t not in text_en]
    if missing:
        _LOG.info(
            "task_canonicalizer: preservation failed for %r — falling back to raw text",
            missing,
        )
        return CanonicalizationResult(lang, task_text, preserve, False,
                                       f"preservation_failed:{missing}")

    return CanonicalizationResult(lang, text_en, preserve, True, None)
