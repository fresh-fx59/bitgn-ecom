"""Tests for the outbox-writing reactive skill."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bitgn_contest_agent.reactive_router import load_reactive_router

PROD_REACTIVE_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "bitgn_contest_agent"
    / "skills"
    / "reactive"
)


class TestOutboxWritingSkillLoads:
    def test_skill_file_loads_without_error(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        names = [s.name for s, _ in router._skills]
        assert "outbox-writing" in names

    def test_skill_has_no_hardcoded_paths(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        for skill, _ in router._skills:
            if skill.name == "outbox-writing":
                assert "60_outbox" not in skill.body
                assert "eml_" not in skill.body
                break
        else:
            pytest.fail("outbox-writing skill not found")

    def test_skill_mentions_attachment_verification(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        for skill, _ in router._skills:
            if skill.name == "outbox-writing":
                body = skill.body.lower()
                assert "attachment" in body
                assert "verif" in body  # verify/verification
                break
        else:
            pytest.fail("outbox-writing skill not found")


class TestOutboxWritingRouting:
    def test_matches_on_write_to_outbox_path(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        decision = router.evaluate(
            tool_name="write",
            tool_args={"path": "/sandbox/60_outbox/outbox/eml_2026-03-30.md"},
            tool_result_text="ok",
            already_injected=frozenset(),
        )
        assert decision is not None
        assert decision.skill_name == "outbox-writing"
        assert decision.source == "regex"

    def test_no_match_on_read_tool(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        decision = router.evaluate(
            tool_name="read",
            tool_args={"path": "/sandbox/60_outbox/outbox/eml_2026-03-30.md"},
            tool_result_text="content",
            already_injected=frozenset(),
        )
        # read tool should not match outbox-writing (it triggers inbox-security only)
        assert decision is None or decision.skill_name != "outbox-writing"

    def test_no_match_on_write_to_inbox(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        with patch(
            "bitgn_contest_agent.classifier.classify",
            return_value={"category": "NONE", "confidence": 0.9},
        ):
            decision = router.evaluate(
                tool_name="write",
                tool_args={"path": "/sandbox/00_inbox/note.md"},
                tool_result_text="ok",
                already_injected=frozenset(),
            )
        assert decision is None or decision.skill_name != "outbox-writing"

    def test_inject_once(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        decision = router.evaluate(
            tool_name="write",
            tool_args={"path": "/sandbox/60_outbox/outbox/eml.md"},
            tool_result_text="ok",
            already_injected=frozenset({"outbox-writing"}),
        )
        assert decision is None
