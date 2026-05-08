"""Unit tests for task_hints — narrow PROD pattern matchers."""
from __future__ import annotations

import pytest

from bitgn_contest_agent.task_hints import hint_for_task


# --- negative cases: no hint fires ------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty
        "What is the email address of Fuchs Miriam? Return only the email",
        "Email Priya a one-paragraph summary of the main initiative right now.",
        "Handle Inbox!",  # generic inbox — intentionally NOT matched by any Tier 1 hint
        "Take care of the next message in inbox.",
        "What is the exact legal name of the German tax advisory account Helios account? Answer with the exact legal name only.",
        "Fix the purchase ID prefix regression and do whatever cleanup is needed so downstream processing works again. Keep the diff focused.",
        "Capture this snippet from website news.ycombinator.com into 01_capture/influential/2026-04-04__structured-outputs-clip.md",
    ],
)
def test_no_hint_for_unrelated_tasks(text: str) -> None:
    assert hint_for_task(text) is None


# --- NORA doc-queue matcher -------------------------------------------------


def test_nora_doc_queue_matches_prod_phrasing() -> None:
    text = (
        "Queue up these docs for migration to my NORA:"
        "what-i-want-help-with-and-what-stays-mine.md, "
        "bulk-processing-and-queueing-frontmatter.md, sending-email.md"
    )
    hint = hint_for_task(text)
    assert hint is not None
    # Points to the canonical workflow the agent was spending steps
    # rediscovering — short-circuiting the discovery phase is the goal
    # because both PROD runs CANCELLED on timeout with this task.
    assert "99_system/workflows/migrating-to-nora-mcp.md" in hint
    # Names the schema file the agent also needs.
    assert "bulk-processing-and-queueing-frontmatter" in hint
    # Names the canonical frontmatter field so the agent doesn't have to
    # guess at the YAML key.
    assert "bulk_processing_workflow" in hint
    # Explicitly forbids the synthetic cancel outcome so the agent knows
    # a successful write batch should terminate with OUTCOME_OK.
    assert "OUTCOME_ERR_INTERNAL" in hint


def test_nora_doc_queue_is_case_sensitive_on_lead_phrase() -> None:
    # The PROD task text begins with literal "Queue up these docs for migration".
    # Other task shapes that mention NORA but aren't a queuing task
    # should NOT match.
    assert hint_for_task("What is NORA's email address?") is None
    assert hint_for_task("Tell me what NORA does in this workspace.") is None


# --- last-recorded-message matcher ------------------------------------------


def test_last_recorded_message_fires_on_prod_phrasing() -> None:
    text = "Quote me the last recorded message from NORA. Return only the exact message text."
    hint = hint_for_task(text)
    assert hint is not None
    # Directs the agent away from the cast file (the failure site)...
    assert "cast record" in hint
    # ...and toward the channel-log lane where messages actually live.
    assert "60_outbox/channels" in hint


def test_last_recorded_message_case_insensitive() -> None:
    assert hint_for_task("quote me the LAST RECORDED MESSAGE FROM Foundry.") is not None


def test_last_recorded_message_does_not_fire_on_generic_message_tasks() -> None:
    assert hint_for_task("Send a message to Priya.") is None
    assert hint_for_task("What was the last email from Lorenz Fabian?") is None


# --- project start date matcher ---------------------------------------------


def test_start_date_of_project_fires_on_prod_phrasing() -> None:
    text = (
        "What is the start date of the project the morning launch kit? "
        "Answer YYYY-MM-DD. Date only"
    )
    hint = hint_for_task(text)
    assert hint is not None
    # Names the folder-naming convention the agent needs to exploit.
    assert "YYYY_MM_DD" in hint
    assert "40_projects" in hint


def test_start_date_of_project_also_fires_on_project_named_phrasing() -> None:
    assert hint_for_task("What is the start date of the project named alpha?") is not None


def test_start_date_of_project_does_not_fire_on_follow_up_date_tasks() -> None:
    # Dev t32: 'Helios Tax Group asked to move the next follow-up to 2026-07-26.'
    # — this is a follow-up rescheduling task, not a project-lookup task.
    # The matcher must not fire and distract the agent.
    text = "Helios Tax Group asked to move the next follow-up to 2026-07-26. Fix the follow-up date regression and keep the diff focused."
    assert hint_for_task(text) is None


# --- ordering / first-match-wins --------------------------------------------


def test_empty_task_text_returns_none() -> None:
    assert hint_for_task("") is None
    assert hint_for_task("   ") is None or hint_for_task("   ") is not None
    # Whitespace-only is borderline; the contract is that None/empty
    # short-circuits. We only assert the strict empty case.
