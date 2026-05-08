"""Integration: numeric answer + multiple candidate reads → NUMERIC_MULTIREF."""
from __future__ import annotations

from tests.integration.agent_harness import run_agent_with_mock_backend


def test_agent_fires_numeric_multiref_with_two_bills():
    """Agent reads 2 bills and returns a numeric answer → verify fires."""
    calls: list[dict] = []

    def backend(messages, **_):
        calls.append({})
        n = len(calls)
        if n == 1:
            return {
                "current_state": "reading first bill",
                "plan_remaining_steps_brief": ["read_another", "total"],
                "identity_verified": True,
                "observation": "new bill",
                "outcome_leaning": "GATHERING_INFORMATION",
                "function": {
                    "tool": "read",
                    "path": "50_finance/purchases/bill_a.md",
                },
            }
        if n == 2:
            return {
                "current_state": "reading second bill",
                "plan_remaining_steps_brief": ["total"],
                "identity_verified": True,
                "observation": "new bill",
                "outcome_leaning": "GATHERING_INFORMATION",
                "function": {
                    "tool": "read",
                    "path": "50_finance/purchases/bill_b.md",
                },
            }
        # Call 3: scalar completion on 2 candidates → verify should fire.
        if n == 3:
            return {
                "current_state": "totaled",
                "plan_remaining_steps_brief": ["submit"],
                "identity_verified": True,
                "observation": "have the number",
                "outcome_leaning": "OUTCOME_OK",
                "function": {
                    "tool": "report_completion",
                    "message": "12",
                    "grounding_refs": [
                        "50_finance/purchases/bill_a.md",
                        "50_finance/purchases/bill_b.md",
                    ],
                    "rulebook_notes": "none",
                    "outcome_justification": "summed",
                    "completed_steps_laconic": ["read", "sum"],
                    "outcome": "OUTCOME_OK",
                },
            }
        # Call 4: re-emit after verification nudge (same or different answer).
        return {
            "current_state": "verified",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "re-derived",
            "outcome_leaning": "OUTCOME_OK",
            "function": {
                "tool": "report_completion",
                "message": "6",
                "grounding_refs": [
                    "50_finance/purchases/bill_a.md",
                ],
                "rulebook_notes": "none",
                "outcome_justification": "one bill matched the filter",
                "completed_steps_laconic": ["re-derived"],
                "outcome": "OUTCOME_OK",
            },
        }

    # NB: _Adapter in the harness doesn't return real read content; if
    # the agent's cache key is the read path (even with empty content),
    # the trigger's len(read_cache) >= 2 still fires. If the real agent
    # only caches on non-empty content, adjust the harness's fake tool
    # dispatcher accordingly.
    trace = run_agent_with_mock_backend(
        task_id="t-verify-02",
        task_text="how much did vendor X charge in total? Number only.",
        backend=backend,
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1
    assert "NUMERIC_MULTIREF" in verify_events[0]["reasons"]
