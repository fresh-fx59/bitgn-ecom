"""Integration: one completion trips MISSING_REF + NUMERIC_MULTIREF; a
single verification round covers both, and the trace event lists both
reasons."""
from __future__ import annotations

from tests.integration.agent_harness import run_agent_with_mock_backend


def test_agent_combines_reasons_in_one_round():
    calls: list[dict] = []

    def backend(messages, **_):
        calls.append({})
        n = len(calls)
        if n == 1:
            return {
                "current_state": "reading",
                "plan_remaining_steps_brief": ["read_more"],
                "identity_verified": True,
                "observation": "new bill",
                "outcome_leaning": "GATHERING_INFORMATION",
                "function": {"tool": "read", "path": "50_finance/purchases/bill_a.md"},
            }
        if n == 2:
            return {
                "current_state": "reading",
                "plan_remaining_steps_brief": ["total"],
                "identity_verified": True,
                "observation": "second bill",
                "outcome_leaning": "GATHERING_INFORMATION",
                "function": {"tool": "read", "path": "50_finance/purchases/bill_b.md"},
            }
        if n == 3:
            # Cite an unread path AND be scalar with 2 records → both fire.
            return {
                "current_state": "totaled",
                "plan_remaining_steps_brief": ["submit"],
                "identity_verified": True,
                "observation": "done",
                "outcome_leaning": "OUTCOME_OK",
                "function": {
                    "tool": "report_completion",
                    "message": "12 (see 40_projects/hearthline/README.md)",
                    "grounding_refs": ["40_projects/hearthline/README.md"],
                    "rulebook_notes": "n/a",
                    "outcome_justification": "sum",
                    "completed_steps_laconic": ["read", "sum"],
                    "outcome": "OUTCOME_OK",
                },
            }
        return {
            "current_state": "fixed",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "fixed",
            "outcome_leaning": "OUTCOME_OK",
            "function": {
                "tool": "report_completion",
                "message": "6",
                "grounding_refs": ["50_finance/purchases/bill_a.md"],
                "rulebook_notes": "n/a",
                "outcome_justification": "re-derived",
                "completed_steps_laconic": ["refined"],
                "outcome": "OUTCOME_OK",
            },
        }

    trace = run_agent_with_mock_backend(
        task_id="t-verify-04",
        task_text="how much did vendor X charge? Number only.",
        backend=backend,
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1, verify_events
    reasons = verify_events[0]["reasons"]
    assert "MISSING_REF" in reasons
    assert "NUMERIC_MULTIREF" in reasons
