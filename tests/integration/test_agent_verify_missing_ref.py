"""Integration: agent cites an unread path → verification round fires,
model re-emits after reading."""
from __future__ import annotations

import json

from tests.integration.agent_harness import (
    run_agent_with_mock_backend,
)


def test_agent_fires_missing_ref_and_records_trace():
    """If the first completion cites a path not in read_cache, verify
    injects a MISSING_REF message and records a trace event."""
    calls: list[dict] = []

    def backend(messages, **_):
        # Call 1: report_completion citing an unread path.
        # Call 2: (after MISSING_REF nudge) report_completion with no
        # unread-path citation so the second verify returns [].
        calls.append({"role": "call", "n": len(calls) + 1})
        if len(calls) == 1:
            return {
                "current_state": "done",
                "plan_remaining_steps_brief": ["submit"],
                "identity_verified": True,
                "observation": "cited",
                "outcome_leaning": "OUTCOME_OK",
                "function": {
                    "tool": "report_completion",
                    "message": "see 40_projects/foo/README.md for detail",
                    "grounding_refs": ["40_projects/foo/README.md"],
                    "rulebook_notes": "none",
                    "outcome_justification": "cited",
                    "completed_steps_laconic": ["done"],
                    "outcome": "OUTCOME_OK",
                },
            }
        return {
            "current_state": "done",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "cited",
            "outcome_leaning": "OUTCOME_OK",
            "function": {
                "tool": "report_completion",
                "message": "answer refined",
                "grounding_refs": [],
                "rulebook_notes": "none",
                "outcome_justification": "refined",
                "completed_steps_laconic": ["done"],
                "outcome": "OUTCOME_OK",
            },
        }

    trace = run_agent_with_mock_backend(
        task_id="t-verify-01",
        task_text="when did project X start?",
        backend=backend,
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1
    assert "MISSING_REF" in verify_events[0]["reasons"]
    assert verify_events[0]["changed"] is True
