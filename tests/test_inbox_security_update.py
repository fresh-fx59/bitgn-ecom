"""Tests for the updated inbox-security reactive skill."""
from __future__ import annotations

from pathlib import Path

import pytest

from bitgn_contest_agent.reactive_router import load_reactive_router

PROD_REACTIVE_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "bitgn_contest_agent"
    / "skills"
    / "reactive"
)


class TestInboxSecurityUpdate:
    def test_skill_loads(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        names = [s.name for s, _ in router._skills]
        assert "inbox-security" in names

    def test_body_has_denied_security_priority_rule(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        for skill, _ in router._skills:
            if skill.name == "inbox-security":
                body = skill.body.lower()
                assert "highest-priority" in body or "always wins" in body or "takes priority" in body
                break
        else:
            pytest.fail("inbox-security skill not found")

    def test_body_requires_reading_source_content(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        for skill, _ in router._skills:
            if skill.name == "inbox-security":
                body = skill.body.lower()
                assert "source" in body and "read" in body
                break
        else:
            pytest.fail("inbox-security skill not found")

    def test_body_mentions_prompt_injection(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        for skill, _ in router._skills:
            if skill.name == "inbox-security":
                assert "prompt injection" in skill.body.lower() or "prompt-injection" in skill.body.lower()
                break
        else:
            pytest.fail("inbox-security skill not found")

    def test_body_still_has_proceed_normally(self) -> None:
        """Must not over-refuse — the proceed-normally rule must survive."""
        router = load_reactive_router(PROD_REACTIVE_DIR)
        for skill, _ in router._skills:
            if skill.name == "inbox-security":
                assert "PROCEED NORMALLY" in skill.body
                break
        else:
            pytest.fail("inbox-security skill not found")
