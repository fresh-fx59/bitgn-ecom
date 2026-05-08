"""Unit tests for router.route() — triage hybrid.

Tier 1: regex matchers loaded from bitgn skill files.
Tier 2: GPT-mini classifier LLM (stubbed in task 0.6; real in 0.7).
Tier 3: UNKNOWN fallback — caller uses base prompt without injection.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bitgn_contest_agent.router import (
    RoutingDecision,
    route,
    load_router,
)


FIX = Path(__file__).parent / "fixtures" / "router_skills"


def test_classifier_system_prompt_requests_query_field() -> None:
    """The tier2 classifier prompt must instruct the model to populate
    extracted.query with a short canonical identifier from the task —
    used by the post-router preflight dispatcher."""
    from bitgn_contest_agent.router import _classifier_system_prompt
    sys_prompt = _classifier_system_prompt([("FINANCE_LOOKUP", "finance task")])
    lower = sys_prompt.lower()
    assert "query" in lower, "classifier prompt must mention `query`"
    assert "extracted" in lower, "classifier prompt must mention `extracted`"


def test_empty_skill_dir_returns_unknown() -> None:
    r = load_router(skills_dir=FIX / "nonexistent")
    decision = r.route("irrelevant task text")
    assert decision.category == "UNKNOWN"
    assert decision.source == "unknown"
    assert decision.skill_name is None


def test_regex_tier1_hit_returns_skill_name() -> None:
    r = load_router(skills_dir=FIX)
    decision = r.route("Please TEST-ROUTE this task")
    assert decision.category == "TEST_CATEGORY"
    assert decision.source == "regex"
    assert decision.confidence == 1.0
    assert decision.skill_name == "test-valid"


def test_regex_tier1_captures_variables() -> None:
    r = load_router(skills_dir=FIX)
    # Second matcher_pattern captures (\w+)
    decision = r.route("test FOO route")
    assert decision.category == "TEST_CATEGORY"
    assert decision.extracted.get("group_1") == "FOO"


def test_classifier_tier2_hit_when_no_regex_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    r = load_router(skills_dir=FIX)
    # Task has no regex match; classifier is called.
    stub_response = {
        "category": "TEST_CATEGORY",
        "confidence": 0.9,
        "extracted": {"target_name": "DORA"},
    }
    with patch(
        "bitgn_contest_agent.classifier.classify",
        return_value=stub_response,
    ):
        decision = r.route("unrelated task that classifier thinks is test-category")
    assert decision.category == "TEST_CATEGORY"
    assert decision.source == "classifier"
    assert decision.confidence == 0.9
    assert decision.extracted == {"target_name": "DORA"}
    assert decision.skill_name == "test-valid"


def test_classifier_low_confidence_falls_back_to_unknown() -> None:
    r = load_router(skills_dir=FIX)
    stub_response = {
        "category": "TEST_CATEGORY",
        "confidence": 0.3,
        "extracted": {},
    }
    with patch(
        "bitgn_contest_agent.classifier.classify",
        return_value=stub_response,
    ):
        decision = r.route("some task")
    assert decision.category == "UNKNOWN"
    assert decision.source == "classifier"


def test_classifier_network_error_returns_unknown() -> None:
    r = load_router(skills_dir=FIX)
    with patch(
        "bitgn_contest_agent.classifier.classify",
        side_effect=RuntimeError("network down"),
    ):
        decision = r.route("some task")
    assert decision.category == "UNKNOWN"
    assert decision.source == "unknown"


def test_classifier_malformed_json_returns_unknown() -> None:
    r = load_router(skills_dir=FIX)
    with patch(
        "bitgn_contest_agent.classifier.classify",
        return_value="not a dict",
    ):
        decision = r.route("some task")
    assert decision.category == "UNKNOWN"


def test_router_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_ROUTER_ENABLED", "0")
    r = load_router(skills_dir=FIX)
    decision = r.route("Please TEST-ROUTE this task")
    assert decision.category == "UNKNOWN"


def test_classifier_prompt_format_and_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """classifier.classify should POST a classification prompt and parse the
    JSON response."""
    from bitgn_contest_agent import classifier as cls_mod

    captured_messages: list = []

    class _FakeClient:
        class _Chat:
            class _Completions:
                @staticmethod
                def create(*, model, messages, temperature, timeout, **kwargs):
                    captured_messages.append(messages)

                    class _Resp:
                        class _Choice:
                            class _Msg:
                                content = '{"category": "TEST_CATEGORY", "confidence": 0.88, "extracted": {"target_name": "FOO"}}'
                            message = _Msg()
                        choices = [_Choice()]

                    return _Resp()

            completions = _Completions()

        chat = _Chat()

    monkeypatch.setattr(cls_mod, "_get_openai_client", lambda: _FakeClient())
    result = cls_mod.classify(
        system="Classify into: TEST_CATEGORY, OTHER, UNKNOWN",
        user="Some task text",
    )
    assert isinstance(result, dict)
    assert result["category"] == "TEST_CATEGORY"
    assert result["confidence"] == 0.88
    assert result["extracted"] == {"target_name": "FOO"}
