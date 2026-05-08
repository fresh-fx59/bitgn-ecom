"""Task router — regex tier 1, classifier tier 2, UNKNOWN tier 3.

Spec §5.3. Called once per task at the top of the agent loop. On a
non-UNKNOWN hit the caller injects the matching bitgn skill body as a
`role=user` message after the task text. Never breaks the main path:
classifier failures, network errors, and malformed JSON all degrade
to UNKNOWN.

Non-English task instructions are normalised to English before tier-1
regex matching via a cheap classifier LLM call, so regex patterns only
need English variants.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from bitgn_contest_agent import classifier, router_config
from bitgn_contest_agent.skill_loader import BitgnSkill, SkillFormatError, load_skill

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    category: str
    source: str  # "regex" | "classifier" | "unknown"
    confidence: float
    extracted: Dict[str, str] = field(default_factory=dict)
    skill_name: Optional[str] = None
    task_text: str = ""


_UNKNOWN = RoutingDecision(
    category="UNKNOWN",
    source="unknown",
    confidence=0.0,
    extracted={},
    skill_name=None,
)

def _normalize_to_english(task_text: str) -> Optional[str]:
    """Translate task text to English via a light LLM call.

    Returns the English translation, or None if the call fails.
    The caller decides when to invoke this (typically after tier-1
    regex misses on the original text).
    """
    try:
        result = classifier.classify(
            system=(
                "Translate the user's text to English. Return ONLY a JSON "
                'object: {"english": "<translated text>"}\n'
                "Preserve all proper names, numbers, quoted strings, and "
                "technical terms exactly as they appear. If the text is "
                "already in English, return it unchanged."
            ),
            user=task_text,
        )
        english = result.get("english", "")
        if isinstance(english, str) and english.strip():
            _LOG.info("normalised task text → %s", english[:120])
            return english.strip()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("task text normalisation failed: %s", exc)

    return None


@dataclass
class _CompiledSkill:
    skill: BitgnSkill
    patterns: List[re.Pattern]


class Router:
    def __init__(self, skills: List[BitgnSkill]) -> None:
        self._compiled: List[_CompiledSkill] = []
        self._by_category: Dict[str, BitgnSkill] = {}
        for s in skills:
            patterns = [re.compile(p) for p in s.matcher_patterns]
            self._compiled.append(_CompiledSkill(skill=s, patterns=patterns))
            self._by_category[s.category] = s

    def _try_regex(self, text: str, task_text: str) -> Optional[RoutingDecision]:
        """Try tier-1 regex matchers against *text*. Returns None on miss."""
        for c in self._compiled:
            for pat in c.patterns:
                m = pat.search(text)
                if m is None:
                    continue
                extracted: Dict[str, str] = {}
                for k, v in m.groupdict().items():
                    if v is not None:
                        extracted[k] = v
                for i, g in enumerate(m.groups(), start=1):
                    if g is not None:
                        extracted.setdefault(f"group_{i}", g)
                return RoutingDecision(
                    category=c.skill.category,
                    source="regex",
                    confidence=1.0,
                    extracted=extracted,
                    skill_name=c.skill.name,
                    task_text=task_text,
                )
        return None

    def route(self, task_text: str) -> RoutingDecision:
        if not router_config.router_enabled():
            return _UNKNOWN
        if not task_text:
            return _UNKNOWN

        # Tier 1a — regex on original text (free, instant).
        hit = self._try_regex(task_text, task_text)
        if hit is not None:
            return hit

        # Tier 1b — regex miss: normalise to English and retry.
        # This catches non-English tasks (Chinese, French, German, …)
        # without needing language-specific regex patterns.
        normalised = _normalize_to_english(task_text)
        if normalised is not None and normalised != task_text:
            hit = self._try_regex(normalised, task_text)
            if hit is not None:
                return hit

        # Tier 2 — classifier LLM (shared module).
        # Uses normalised text if available for better classification.
        if not self._compiled:
            return _UNKNOWN
        classifier_text = normalised or task_text
        skill_meta = [
            (c.skill.category, c.skill.classifier_hint or c.skill.description)
            for c in self._compiled
        ]
        try:
            raw = classifier.classify(
                system=_classifier_system_prompt(skill_meta),
                user=classifier_text,
            )
        except Exception as exc:  # noqa: BLE001 — router never breaks the main path
            _LOG.warning("classifier failed, degrading to UNKNOWN: %s", exc)
            return _UNKNOWN

        category, confidence = classifier.parse_response(
            raw, valid_categories=set(self._by_category),
        )
        extracted = (raw.get("extracted") or {}) if isinstance(raw, dict) else {}
        if not isinstance(extracted, dict):
            extracted = {}

        if category is None or confidence < router_config.confidence_threshold():
            return RoutingDecision(
                category="UNKNOWN",
                source="classifier",
                confidence=confidence,
                extracted={},
                skill_name=None,
                task_text=task_text,
            )

        skill = self._by_category[category]
        return RoutingDecision(
            category=category,
            source="classifier",
            confidence=confidence,
            extracted={k: str(v) for k, v in extracted.items()},
            skill_name=skill.name,
            task_text=task_text,
        )

    def skill_body_for(self, skill_name: str) -> Optional[str]:
        for c in self._compiled:
            if c.skill.name == skill_name:
                return c.skill.body
        return None

    def skills_by_name(self) -> Dict[str, BitgnSkill]:
        """Read-only mapping of skill name -> BitgnSkill.

        Provides external read access to the compiled skill list without
        reaching into the router's internal _compiled list.
        """
        return {c.skill.name: c.skill for c in self._compiled}


def load_router(skills_dir: Path | str) -> Router:
    skills: List[BitgnSkill] = []
    p = Path(skills_dir)
    if p.exists() and p.is_dir():
        for md in sorted(p.glob("*.md")):
            try:
                skills.append(load_skill(md))
            except SkillFormatError as exc:
                _LOG.error("skill %s failed to load: %s", md, exc)
                raise
    return Router(skills=skills)


# Module-level singleton + legacy route() convenience wrapper.
_ROUTER_SINGLETON: Optional[Router] = None
_DEFAULT_SKILLS_DIR = (
    Path(__file__).parent / "skills"
)


def _get_default_router() -> Router:
    global _ROUTER_SINGLETON
    if _ROUTER_SINGLETON is None:
        _ROUTER_SINGLETON = load_router(_DEFAULT_SKILLS_DIR)
    return _ROUTER_SINGLETON


def route(task_text: str) -> RoutingDecision:
    return _get_default_router().route(task_text)


def _classifier_system_prompt(skill_meta: list[tuple[str, str]]) -> str:
    """Build the system prompt for the pre-task tier-2 classifier.

    skill_meta: [(category, hint_or_description), ...]

    The `extracted.query` field is consumed by the post-router preflight
    dispatcher: it picks the matching `preflight_*` tool for the chosen
    category and uses `query` as its primary search string. A noisy or
    missing query degrades to no preflight (skill body fall-through).
    """
    lines = [f"  - {cat}: {hint}" for cat, hint in skill_meta]
    lines.append("  - UNKNOWN: task does not match any known category")
    category_block = "\n".join(lines)
    return (
        "You classify bitgn benchmark tasks into one of these categories:\n"
        f"{category_block}\n"
        "\n"
        "Return ONLY a JSON object of the form:\n"
        '  {"category": "<one of above>", "confidence": <0.0-1.0>, '
        '"extracted": {"query": "<short canonical identifier from the task>"}}\n'
        "\n"
        'The "query" field should be the most specific identifier the task '
        "hinges on — a vendor name, item description, person reference, "
        "project hint, or destination system. When the task uses an indirect "
        "descriptor (e.g. 'the founder I talk product with', 'the client at "
        "the tax firm'), preserve the FULL descriptor phrase as the query — "
        "do NOT abbreviate or paraphrase it. "
        'Omit "query" only if the task has no such identifier (e.g. inbox '
        'tasks like "take the next inbox item").\n'
        "\n"
        "No prose. No markdown fences."
    )
