"""Reactive routing — mid-task skill injection based on tool dispatch.

Complements the pre-task Router (spec §5.3) with a second routing
stage that fires after each non-terminal tool call.  Tier 1 is a
regex match on tool name + file path (fast, deterministic).  Tier 2
is a lightweight LLM classifier that evaluates the tool result content
when tier 1 misses — same pattern as the pre-task router.

Reactive skills live in ``skills/reactive/`` and use flat frontmatter
keys ``reactive_tool`` and ``reactive_path`` instead of
``matcher_patterns``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from bitgn_contest_agent import classifier, router_config

if TYPE_CHECKING:
    from bitgn_contest_agent.backend.base import Backend
from bitgn_contest_agent.skill_loader import (
    SkillFormatError,
    _parse_frontmatter,
    _split_frontmatter,
)

_LOG = logging.getLogger(__name__)

_REACTIVE_REQUIRED_KEYS = (
    "name", "description", "type", "category",
    "reactive_tool", "reactive_path",
)
_VALID_TYPES = ("rigid", "flexible")

# Truncate tool result content sent to the classifier to keep
# token cost bounded.  2000 chars ≈ 500 tokens — enough for the
# classifier to identify the content shape.
_CLASSIFIER_CONTENT_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class ReactiveSkill:
    name: str
    description: str
    type: str
    category: str
    reactive_tool: str
    reactive_path: str
    body: str


@dataclass(frozen=True, slots=True)
class ReactiveDecision:
    skill_name: str
    category: str
    source: str  # "regex" | "classifier"
    confidence: float
    body: str


def load_reactive_skill(path: Path) -> ReactiveSkill:
    """Parse a reactive skill file and return a ReactiveSkill.

    Raises SkillFormatError on any format violation.
    """
    text = Path(path).read_text(encoding="utf-8")
    frontmatter_text, body = _split_frontmatter(text, path)
    parsed = _parse_frontmatter(frontmatter_text, path)
    _validate_reactive(parsed, path)
    return ReactiveSkill(
        name=parsed["name"],
        description=parsed["description"],
        type=parsed["type"],
        category=parsed["category"],
        reactive_tool=parsed["reactive_tool"],
        reactive_path=parsed["reactive_path"],
        body=body.strip() + "\n",
    )


def _validate_reactive(parsed: dict, path: Path) -> None:
    for key in _REACTIVE_REQUIRED_KEYS:
        if key not in parsed:
            raise SkillFormatError(
                f"{path}: missing required frontmatter key `{key}`"
            )
    if parsed["type"] not in _VALID_TYPES:
        raise SkillFormatError(
            f"{path}: type must be one of rigid|flexible, got {parsed['type']!r}"
        )


class ReactiveRouter:
    """Evaluates tool dispatch results against reactive skill triggers.

    Two-tier evaluation (mirrors the pre-task Router):

    - **Tier 1 — regex:** instant match on tool name + file path.
      Returns immediately with ``confidence=1.0``.
    - **Tier 2 — LLM classifier:** when tier 1 misses but the tool
      name matches a reactive skill's trigger, sends the tool result
      content to ``gpt-5.4-mini`` for classification.  Returns with
      the classifier's confidence; below threshold → no injection.

    Stateless — injection tracking is owned by the caller via the
    ``already_injected`` parameter.  Safe to share across concurrent
    tasks.
    """

    def __init__(self, skills: List[ReactiveSkill]) -> None:
        self._skills: List[tuple[ReactiveSkill, re.Pattern]] = []
        self._by_category: Dict[str, ReactiveSkill] = {}
        self._trigger_tools: set[str] = set()
        for s in skills:
            try:
                compiled = re.compile(s.reactive_path)
            except re.error as exc:
                raise SkillFormatError(
                    f"reactive skill {s.name}: invalid regex in reactive_path: {exc}"
                ) from exc
            self._skills.append((s, compiled))
            self._by_category[s.category] = s
            self._trigger_tools.add(s.reactive_tool)

    def evaluate(
        self,
        tool_name: str,
        tool_args: dict,
        tool_result_text: str,
        already_injected: frozenset[str] = frozenset(),
        backend: Optional[Backend] = None,
    ) -> Optional[ReactiveDecision]:
        """Check if a tool dispatch triggers a reactive skill injection.

        Tier 1 (regex) is tried first.  On miss, tier 2 (LLM) is
        called if the tool name matches any reactive skill's trigger.
        Returns a ReactiveDecision if a skill matches, None otherwise.

        The caller should add the returned ``skill_name`` to its
        tracking set to prevent duplicate injection (inject-once
        semantics).
        """
        # Fast exit: tool doesn't match any reactive trigger.
        if tool_name not in self._trigger_tools:
            return None

        path = tool_args.get("path") or tool_args.get("root") or ""

        # Tier 1 — regex on tool name + path.
        for skill, pattern in self._skills:
            if skill.reactive_tool != tool_name:
                continue
            if skill.name in already_injected:
                continue
            if not pattern.search(path):
                continue
            return ReactiveDecision(
                skill_name=skill.name,
                category=skill.category,
                source="regex",
                confidence=1.0,
                body=skill.body,
            )

        # Tier 2 — LLM classifier on tool result content (shared module).
        # Only called when tier 1 misses but the tool type matches.
        if not self._skills:
            return None

        # Collect eligible categories (not already injected).
        eligible = [
            s for s, _ in self._skills
            if s.reactive_tool == tool_name and s.name not in already_injected
        ]
        if not eligible:
            return None

        system = _reactive_classifier_system_prompt(
            [s.category for s in eligible],
        )
        user = _reactive_classifier_user_msg(
            tool_name, path, tool_result_text,
        )
        try:
            if backend is not None:
                raw = classifier.classify_structured(
                    backend, system=system, user=user,
                )
            else:
                raw = classifier.classify(system=system, user=user)
        except Exception as exc:  # noqa: BLE001 — reactive router never breaks the main path
            _LOG.warning("reactive classifier failed, skipping: %s", exc)
            return None

        category, confidence = classifier.parse_response(
            raw, valid_categories=set(self._by_category),
        )

        if category is None or confidence < router_config.confidence_threshold():
            return None

        skill = self._by_category[category]
        if skill.name in already_injected:
            return None

        return ReactiveDecision(
            skill_name=skill.name,
            category=category,
            source="classifier",
            confidence=confidence,
            body=skill.body,
        )


def load_reactive_router(skills_dir: Path | str) -> ReactiveRouter:
    """Load all reactive skills from a directory and return a ReactiveRouter."""
    skills: List[ReactiveSkill] = []
    p = Path(skills_dir)
    if p.exists() and p.is_dir():
        for md in sorted(p.glob("*.md")):
            try:
                skills.append(load_reactive_skill(md))
            except SkillFormatError as exc:
                _LOG.error("reactive skill %s failed to load: %s", md, exc)
                raise
    return ReactiveRouter(skills=skills)


def _reactive_classifier_system_prompt(categories: List[str]) -> str:
    """Build the system prompt for the reactive tier-2 classifier."""
    category_list = classifier.build_category_list(categories, fallback="NONE")
    return (
        "You classify the content of a tool result from a sandbox agent.\n"
        "The agent just executed a tool call and received a result.\n"
        "Based on the tool name, file path, and content, classify it into "
        "one of these categories:\n"
        f"{category_list}\n"
        "\n"
        "Return ONLY a JSON object of the form:\n"
        '  {"category": "<one of above>", "confidence": <0.0-1.0>}\n'
        "No prose. No markdown fences."
    )


def _reactive_classifier_user_msg(
    tool_name: str, tool_path: str, tool_content: str,
) -> str:
    """Build the user message for the reactive tier-2 classifier."""
    content_preview = tool_content[:_CLASSIFIER_CONTENT_LIMIT]
    if len(tool_content) > _CLASSIFIER_CONTENT_LIMIT:
        content_preview += "\n[... truncated ...]"
    return (
        f"Tool: {tool_name}\n"
        f"Path: {tool_path}\n"
        f"Content:\n{content_preview}"
    )
