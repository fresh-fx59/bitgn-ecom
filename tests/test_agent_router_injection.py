"""End-to-end check that bitgn skill bodies are injected into the message
sequence when the router hits."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bitgn_contest_agent.router import load_router


FIX = Path(__file__).parent / "fixtures" / "router_skills"


def test_router_decision_shape_for_known_task() -> None:
    r = load_router(skills_dir=FIX)
    decision = r.route("Please TEST-ROUTE this")
    assert decision.skill_name == "test-valid"


def test_skill_body_retrievable_by_name() -> None:
    r = load_router(skills_dir=FIX)
    body = r.skill_body_for("test-valid")
    assert body is not None
    assert "# Test Valid Skill" in body


def test_agent_loop_injects_skill_body_when_router_hits() -> None:
    """When router.route() returns a non-UNKNOWN decision, the agent
    loop prepends a user message with the skill body before the
    existing task_hints injection."""
    from bitgn_contest_agent.agent import _build_initial_messages

    r = load_router(skills_dir=FIX)
    task_text = "Please TEST-ROUTE this"
    messages, decision = _build_initial_messages(task_text=task_text, router=r)
    assert decision is not None and decision.skill_name == "test-valid"
    # Expected message sequence:
    #   [0] system (system_prompt)
    #   [1] user   (task_text)
    #   [2] user   (skill body, prefixed with "SKILL CONTEXT ...")
    assert len(messages) == 3
    assert messages[0].role == "system"
    assert messages[1].role == "user"
    assert messages[1].content == task_text
    assert messages[2].role == "user"
    assert "SKILL CONTEXT" in messages[2].content
    assert "test-valid" in messages[2].content
    assert "# Test Valid Skill" in messages[2].content


def test_agent_loop_no_injection_on_unknown() -> None:
    from bitgn_contest_agent.agent import _build_initial_messages

    r = load_router(skills_dir=FIX)
    task_text = "Totally unrelated task"
    # Patch classifier to raise — router degrades to UNKNOWN.
    with patch(
        "bitgn_contest_agent.classifier.classify",
        side_effect=RuntimeError("network"),
    ):
        messages, decision = _build_initial_messages(task_text=task_text, router=r)
    assert decision is not None and decision.skill_name is None
    # Only system + task text; no skill injection.
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[1].content == task_text


def test_router_decision_updates_task_context_skill_and_category(tmp_path) -> None:
    """After router fires, skill+category are injected into ContextVar."""
    import logging
    from bitgn_contest_agent.arch_log import (
        set_task_context, reset_task_context, TaskContextFilter,
    )
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.agent import _build_initial_messages
    from bitgn_contest_agent.router import load_router
    from pathlib import Path

    skills_dir = (Path(__file__).parent.parent
                  / "src" / "bitgn_contest_agent" / "skills")
    router = load_router(skills_dir=skills_dir)
    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    token = set_task_context(
        task_id="t100", run_index=0, trace_name="t100__run0.jsonl",
        writer=writer,
    )
    try:
        _build_initial_messages(
            task_text=(
                "How much did Müller charge me for pen refills 51 days ago?"
            ),
            task_id="t100",
            router=router,
        )
        f = TaskContextFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="m", args=(), exc_info=None,
        )
        f.filter(rec)
        assert rec.skill == "finance-lookup"
        assert rec.category == "FINANCE_LOOKUP"
    finally:
        reset_task_context(token)
        writer.close()


def test_router_no_match_sets_category_unknown(tmp_path) -> None:
    import logging
    from bitgn_contest_agent.arch_log import (
        set_task_context, reset_task_context, TaskContextFilter,
    )
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.agent import _build_initial_messages
    from bitgn_contest_agent.router import load_router
    from pathlib import Path

    skills_dir = (Path(__file__).parent.parent
                  / "src" / "bitgn_contest_agent" / "skills")
    router = load_router(skills_dir=skills_dir)
    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    token = set_task_context(
        task_id="tx", run_index=0, trace_name="tx.jsonl", writer=writer,
    )
    try:
        with patch(
            "bitgn_contest_agent.classifier.classify",
            side_effect=RuntimeError("network"),
        ):
            _build_initial_messages(
                task_text="handle the next inbox item please",
                task_id="tx",
                router=router,
            )
        f = TaskContextFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="m", args=(), exc_info=None,
        )
        f.filter(rec)
        # Inbox task now routes to inbox-processing via regex.
        assert rec.skill == "inbox-processing"
        assert rec.category == "INBOX_PROCESSING"
    finally:
        reset_task_context(token)
        writer.close()
