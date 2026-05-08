"""Integration: after verification fires, a re-emitted report_completion
that would trigger the same reason again must NOT fire a second verify
round."""
from __future__ import annotations

from tests.integration.agent_harness import run_agent_with_mock_backend


def test_verify_caps_at_one_round_per_task():
    calls: list[dict] = []

    def backend(messages, **_):
        calls.append({})
        # Both calls return the same bad completion (cites unread path).
        return {
            "current_state": "done",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "citing",
            "outcome_leaning": "OUTCOME_OK",
            "function": {
                "tool": "report_completion",
                "message": "see 40_projects/x/README.md",
                "grounding_refs": ["40_projects/x/README.md"],
                "rulebook_notes": "n/a",
                "outcome_justification": "cited",
                "completed_steps_laconic": ["done"],
                "outcome": "OUTCOME_OK",
            },
        }

    trace = run_agent_with_mock_backend(
        task_id="t-verify-05",
        task_text="tell me when",
        backend=backend,
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1, f"too many verify rounds: {verify_events}"
    assert verify_events[0]["changed"] is False
    # Exactly 2 backend calls: first attempt + verify retry.
    assert len(calls) == 2, calls
