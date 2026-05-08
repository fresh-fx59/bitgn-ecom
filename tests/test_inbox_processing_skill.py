"""Tests for the inbox-processing routed skill."""
from __future__ import annotations

from pathlib import Path

from bitgn_contest_agent.router import load_router

SKILLS_DIR = Path(__file__).parent.parent / "src" / "bitgn_contest_agent" / "skills"


class TestInboxProcessingRouting:
    def test_skill_file_loads_without_error(self) -> None:
        router = load_router(skills_dir=SKILLS_DIR)
        body = router.skill_body_for("inbox-processing")
        assert body is not None
        assert "completeness" in body.lower() or "ALL" in body

    def test_routes_work_oldest_inbox(self) -> None:
        router = load_router(skills_dir=SKILLS_DIR)
        decision = router.route("Work the oldest inbox message.")
        assert decision.skill_name == "inbox-processing"
        assert decision.category == "INBOX_PROCESSING"
        assert decision.source == "regex"

    def test_routes_process_next_inbox(self) -> None:
        router = load_router(skills_dir=SKILLS_DIR)
        decision = router.route("Process the next inbox item.")
        assert decision.skill_name == "inbox-processing"

    def test_routes_handle_oldest_message(self) -> None:
        router = load_router(skills_dir=SKILLS_DIR)
        decision = router.route("Handle the oldest inbox message.")
        assert decision.skill_name == "inbox-processing"

    def test_routes_first_inbox_item(self) -> None:
        router = load_router(skills_dir=SKILLS_DIR)
        decision = router.route("Work on the first inbox item please.")
        assert decision.skill_name == "inbox-processing"

    def test_does_not_route_unrelated_task(self) -> None:
        router = load_router(skills_dir=SKILLS_DIR)
        decision = router.route(
            "How much did Acme charge me for widgets 51 days ago?"
        )
        assert decision.skill_name != "inbox-processing"

    def test_does_not_route_project_start_date(self) -> None:
        router = load_router(skills_dir=SKILLS_DIR)
        decision = router.route(
            "Need the start date for Northstar Ledger."
        )
        assert decision.skill_name != "inbox-processing"
