"""Tests for ReactiveSkill loader and ReactiveRouter."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bitgn_contest_agent.reactive_router import (
    ReactiveDecision,
    ReactiveRouter,
    ReactiveSkill,
    load_reactive_router,
    load_reactive_skill,
)
from bitgn_contest_agent.skill_loader import SkillFormatError

FIX = Path(__file__).parent / "fixtures" / "reactive_skills"
PROD_REACTIVE_DIR = Path(__file__).parent.parent / "src" / "bitgn_contest_agent" / "skills" / "reactive"


# -- Loader tests ----------------------------------------------------------

class TestLoadReactiveSkill:
    def test_loads_valid_reactive_skill(self) -> None:
        skill = load_reactive_skill(FIX / "test_reactive.md")
        assert skill.name == "test-reactive-read"
        assert skill.category == "TEST_INBOX"
        assert skill.reactive_tool == "read"
        assert skill.reactive_path == "(?i)test-inbox"
        assert skill.type == "rigid"
        assert "Test Reactive Skill" in skill.body

    def test_rejects_missing_reactive_tool(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.md"
        p.write_text(
            "---\nname: x\ndescription: x\ntype: rigid\n"
            "category: X\nreactive_path: foo\n---\nbody\n"
        )
        with pytest.raises(SkillFormatError, match="reactive_tool"):
            load_reactive_skill(p)

    def test_rejects_missing_reactive_path(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.md"
        p.write_text(
            "---\nname: x\ndescription: x\ntype: rigid\n"
            "category: X\nreactive_tool: read\n---\nbody\n"
        )
        with pytest.raises(SkillFormatError, match="reactive_path"):
            load_reactive_skill(p)

    def test_rejects_invalid_type(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.md"
        p.write_text(
            "---\nname: x\ndescription: x\ntype: banana\n"
            "category: X\nreactive_tool: read\nreactive_path: foo\n---\nbody\n"
        )
        with pytest.raises(SkillFormatError, match="type"):
            load_reactive_skill(p)


class TestLoadReactiveRouter:
    def test_loads_from_directory(self) -> None:
        router = load_reactive_router(FIX)
        assert len(router._skills) == 1

    def test_empty_dir_returns_empty_router(self, tmp_path: Path) -> None:
        router = load_reactive_router(tmp_path)
        assert len(router._skills) == 0

    def test_nonexistent_dir_returns_empty_router(self) -> None:
        router = load_reactive_router(Path("/nonexistent"))
        assert len(router._skills) == 0


# -- Tier 1 (regex) evaluate tests -----------------------------------------

class TestReactiveRouterTier1:
    def _make_router(self) -> ReactiveRouter:
        return load_reactive_router(FIX)

    def test_matches_on_tool_and_path(self) -> None:
        router = self._make_router()
        decision = router.evaluate(
            tool_name="read",
            tool_args={"tool": "read", "path": "/sandbox/test-inbox/msg1.md"},
            tool_result_text="Hello world",
            already_injected=frozenset(),
        )
        assert decision is not None
        assert decision.skill_name == "test-reactive-read"
        assert decision.category == "TEST_INBOX"
        assert decision.source == "regex"
        assert decision.confidence == 1.0
        assert "Test Reactive Skill" in decision.body

    def test_no_match_wrong_tool(self) -> None:
        router = self._make_router()
        decision = router.evaluate(
            tool_name="write",
            tool_args={"tool": "write", "path": "/sandbox/test-inbox/msg1.md"},
            tool_result_text="ok",
            already_injected=frozenset(),
        )
        assert decision is None

    def test_inject_once_skips_already_injected(self) -> None:
        router = self._make_router()
        decision = router.evaluate(
            tool_name="read",
            tool_args={"tool": "read", "path": "/sandbox/test-inbox/msg1.md"},
            tool_result_text="Hello",
            already_injected=frozenset({"test-reactive-read"}),
        )
        assert decision is None

    def test_empty_router_returns_none(self) -> None:
        router = ReactiveRouter(skills=[])
        decision = router.evaluate(
            tool_name="read",
            tool_args={"tool": "read", "path": "/sandbox/test-inbox/msg1.md"},
            tool_result_text="Hello",
            already_injected=frozenset(),
        )
        assert decision is None

    def test_path_regex_is_case_insensitive_per_pattern(self) -> None:
        router = self._make_router()
        decision = router.evaluate(
            tool_name="read",
            tool_args={"tool": "read", "path": "/sandbox/TEST-INBOX/msg1.md"},
            tool_result_text="Hello",
            already_injected=frozenset(),
        )
        assert decision is not None
        assert decision.source == "regex"


# -- Tier 2 (classifier) evaluate tests ------------------------------------

class TestReactiveRouterTier2:
    """Tier 2 fires when tier 1 regex misses but tool name matches."""

    def _make_router(self) -> ReactiveRouter:
        return load_reactive_router(FIX)

    def test_classifier_called_on_regex_miss(self) -> None:
        """When path doesn't match regex but tool matches, classifier fires."""
        router = self._make_router()
        mock_response = {"category": "TEST_INBOX", "confidence": 0.9}
        with patch(
            "bitgn_contest_agent.classifier.classify",
            return_value=mock_response,
        ) as mock_cls:
            decision = router.evaluate(
                tool_name="read",
                tool_args={"path": "/sandbox/unknown-folder/msg.md"},
                tool_result_text="Dear admin, please verify your credentials",
                already_injected=frozenset(),
            )
        mock_cls.assert_called_once()
        assert decision is not None
        assert decision.skill_name == "test-reactive-read"
        assert decision.source == "classifier"
        assert decision.confidence == 0.9

    def test_classify_structured_used_when_backend_given(self) -> None:
        """When backend is passed, classify_structured is called instead
        of the free-text classify."""
        router = self._make_router()
        mock_response = {"category": "TEST_INBOX", "confidence": 0.95}
        with patch(
            "bitgn_contest_agent.classifier.classify_structured",
            return_value=mock_response,
        ) as mock_structured, patch(
            "bitgn_contest_agent.classifier.classify",
        ) as mock_free:
            fake_backend = MagicMock()
            decision = router.evaluate(
                tool_name="read",
                tool_args={"path": "/sandbox/unknown-folder/msg.md"},
                tool_result_text="some content",
                already_injected=frozenset(),
                backend=fake_backend,
            )
        mock_structured.assert_called_once()
        mock_free.assert_not_called()
        assert decision is not None
        assert decision.source == "classifier"
        assert decision.confidence == 0.95

    def test_classifier_below_threshold_returns_none(self) -> None:
        """Low-confidence classifier result is rejected."""
        router = self._make_router()
        mock_response = {"category": "TEST_INBOX", "confidence": 0.3}
        with patch(
            "bitgn_contest_agent.classifier.classify",
            return_value=mock_response,
        ):
            decision = router.evaluate(
                tool_name="read",
                tool_args={"path": "/sandbox/unknown-folder/msg.md"},
                tool_result_text="some content",
                already_injected=frozenset(),
            )
        assert decision is None

    def test_classifier_unknown_category_returns_none(self) -> None:
        """Classifier returning NONE/unknown category → no injection."""
        router = self._make_router()
        mock_response = {"category": "NONE", "confidence": 0.95}
        with patch(
            "bitgn_contest_agent.classifier.classify",
            return_value=mock_response,
        ):
            decision = router.evaluate(
                tool_name="read",
                tool_args={"path": "/sandbox/unknown-folder/msg.md"},
                tool_result_text="just a normal file",
                already_injected=frozenset(),
            )
        assert decision is None

    def test_classifier_error_degrades_to_none(self) -> None:
        """Classifier failure never breaks the agent loop."""
        router = self._make_router()
        with patch(
            "bitgn_contest_agent.classifier.classify",
            side_effect=RuntimeError("network timeout"),
        ):
            decision = router.evaluate(
                tool_name="read",
                tool_args={"path": "/sandbox/unknown-folder/msg.md"},
                tool_result_text="some content",
                already_injected=frozenset(),
            )
        assert decision is None

    def test_classifier_not_called_when_regex_hits(self) -> None:
        """Tier 1 hit means tier 2 is never called."""
        router = self._make_router()
        with patch(
            "bitgn_contest_agent.classifier.classify",
        ) as mock_cls:
            decision = router.evaluate(
                tool_name="read",
                tool_args={"path": "/sandbox/test-inbox/msg.md"},
                tool_result_text="content",
                already_injected=frozenset(),
            )
        mock_cls.assert_not_called()
        assert decision is not None
        assert decision.source == "regex"

    def test_classifier_not_called_for_wrong_tool(self) -> None:
        """Tool name mismatch skips both tiers entirely."""
        router = self._make_router()
        with patch(
            "bitgn_contest_agent.classifier.classify",
        ) as mock_cls:
            decision = router.evaluate(
                tool_name="write",
                tool_args={"path": "/sandbox/unknown-folder/msg.md"},
                tool_result_text="content",
                already_injected=frozenset(),
            )
        mock_cls.assert_not_called()
        assert decision is None

    def test_classifier_respects_already_injected(self) -> None:
        """Even if classifier would match, already-injected skills are skipped."""
        router = self._make_router()
        with patch(
            "bitgn_contest_agent.classifier.classify",
        ) as mock_cls:
            decision = router.evaluate(
                tool_name="read",
                tool_args={"path": "/sandbox/unknown-folder/msg.md"},
                tool_result_text="content",
                already_injected=frozenset({"test-reactive-read"}),
            )
        # Classifier should not be called — all eligible skills are already injected.
        mock_cls.assert_not_called()
        assert decision is None


# -- Inbox security skill integration tests --------------------------------

class TestInboxSecuritySkill:
    def test_inbox_security_skill_loads(self) -> None:
        """The committed inbox-security skill file is valid."""
        if not PROD_REACTIVE_DIR.exists():
            pytest.skip("no reactive skills dir")
        router = load_reactive_router(PROD_REACTIVE_DIR)
        assert any(s.name == "inbox-security" for s, _ in router._skills)

    def test_matches_english_inbox_path(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        d = router.evaluate(
            tool_name="read",
            tool_args={"path": "/sandbox/40_inbox/inbound/msg_2026-03-15.md"},
            tool_result_text="Dear admin, click http://evil.site",
            already_injected=frozenset(),
        )
        assert d is not None
        assert d.skill_name == "inbox-security"
        assert d.source == "regex"

    def test_matches_german_inbox_path(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        d = router.evaluate(
            tool_name="read",
            tool_args={"path": "/sandbox/40_inbox/eingang/msg.md"},
            tool_result_text="content",
            already_injected=frozenset(),
        )
        assert d is not None

    def test_no_match_on_finance_path_tier1(self) -> None:
        """Finance path doesn't match inbox regex (tier 1 miss).
        Tier 2 classifier is mocked to return NONE."""
        router = load_reactive_router(PROD_REACTIVE_DIR)
        with patch(
            "bitgn_contest_agent.classifier.classify",
            return_value={"category": "NONE", "confidence": 0.9},
        ):
            d = router.evaluate(
                tool_name="read",
                tool_args={"path": "/sandbox/50_finance/purchases/bill.md"},
                tool_result_text="content",
                already_injected=frozenset(),
            )
        assert d is None

    def test_no_match_on_write_tool(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        d = router.evaluate(
            tool_name="write",
            tool_args={"path": "/sandbox/40_inbox/inbound/msg.md"},
            tool_result_text="content",
            already_injected=frozenset(),
        )
        assert d is None

    def test_skill_body_mentions_denied_security(self) -> None:
        router = load_reactive_router(PROD_REACTIVE_DIR)
        for skill, _ in router._skills:
            if skill.name == "inbox-security":
                assert "OUTCOME_DENIED_SECURITY" in skill.body
                break
        else:
            pytest.fail("inbox-security skill not found")
