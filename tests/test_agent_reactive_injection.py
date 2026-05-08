"""Test that the agent loop injects reactive skill bodies mid-conversation."""
from __future__ import annotations

from pathlib import Path

from bitgn_contest_agent.backend.base import Message
from bitgn_contest_agent.reactive_router import load_reactive_router

FIX = Path(__file__).parent / "fixtures" / "reactive_skills"


def test_reactive_hook_builds_injection_message() -> None:
    """ReactiveRouter.evaluate() returns a decision; verify the
    agent loop would construct the correct injection message."""
    router = load_reactive_router(FIX)
    decision = router.evaluate(
        tool_name="read",
        tool_args={"tool": "read", "path": "/sandbox/test-inbox/msg.md"},
        tool_result_text="some content",
        already_injected=frozenset(),
    )
    assert decision is not None
    # Simulate what the agent loop does with the decision
    prefix = (
        f"REACTIVE SKILL CONTEXT (mid-task): {decision.skill_name}\n"
        f"Triggered by: read(/sandbox/test-inbox/msg.md)\n\n"
    )
    msg = Message(role="user", content=prefix + decision.body)
    assert msg.role == "user"
    assert "REACTIVE SKILL CONTEXT" in msg.content
    assert "test-reactive-read" in msg.content
    assert "Test Reactive Skill" in msg.content


def test_reactive_inject_once_prevents_double_injection() -> None:
    """After a skill is injected, subsequent matches are suppressed."""
    router = load_reactive_router(FIX)
    injected: set[str] = set()

    # First read from inbox — should trigger
    d1 = router.evaluate(
        tool_name="read",
        tool_args={"tool": "read", "path": "/sandbox/test-inbox/msg1.md"},
        tool_result_text="content1",
        already_injected=frozenset(injected),
    )
    assert d1 is not None
    injected.add(d1.skill_name)

    # Second read from inbox — should NOT trigger (already injected)
    d2 = router.evaluate(
        tool_name="read",
        tool_args={"tool": "read", "path": "/sandbox/test-inbox/msg2.md"},
        tool_result_text="content2",
        already_injected=frozenset(injected),
    )
    assert d2 is None
