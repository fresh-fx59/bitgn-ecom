"""Integration: inbox task + NONE_CLARIFICATION + no outbox write → INBOX_GIVEUP."""
from __future__ import annotations

from tests.integration.agent_harness import run_agent_with_mock_backend


def test_agent_fires_inbox_giveup():
    calls: list[dict] = []

    def backend(messages, **_):
        calls.append({})
        if len(calls) == 1:
            return {
                "current_state": "stuck",
                "plan_remaining_steps_brief": ["submit"],
                "identity_verified": True,
                "observation": "sender unclear",
                "outcome_leaning": "OUTCOME_NONE_CLARIFICATION",
                "function": {
                    "tool": "report_completion",
                    "message": "Cannot resolve the sender; need more info.",
                    "grounding_refs": [],
                    "rulebook_notes": "n/a",
                    "outcome_justification": "sender unknown",
                    "completed_steps_laconic": ["read"],
                    "outcome": "OUTCOME_NONE_CLARIFICATION",
                },
            }
        # Call 2 after verify nudge: re-emit same outcome.
        return {
            "current_state": "still stuck",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "sender still unclear",
            "outcome_leaning": "OUTCOME_NONE_CLARIFICATION",
            "function": {
                "tool": "report_completion",
                "message": "Cannot resolve the sender.",
                "grounding_refs": [],
                "rulebook_notes": "n/a",
                "outcome_justification": "sender unknown",
                "completed_steps_laconic": ["read", "re-checked"],
                "outcome": "OUTCOME_NONE_CLARIFICATION",
            },
        }

    trace = run_agent_with_mock_backend(
        task_id="t-verify-03",
        task_text="take care of the next message in inbox",
        backend=backend,
        skill_name="inbox-processing",   # harness must accept + forward this
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1
    assert "INBOX_GIVEUP" in verify_events[0]["reasons"]
