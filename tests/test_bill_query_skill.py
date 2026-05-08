"""Bill query skill — loading + routing tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from bitgn_contest_agent.skill_loader import load_skill
from bitgn_contest_agent.router import load_router


SKILL_PATH = Path(__file__).parent.parent / "src" / "bitgn_contest_agent" / "skills" / "bill_query.md"
SKILLS_DIR = SKILL_PATH.parent


def test_bill_query_skill_loads() -> None:
    skill = load_skill(SKILL_PATH)
    assert skill.name == "bill-query"
    assert skill.category == "BILL_QUERY"
    assert skill.type == "flexible"
    assert len(skill.matcher_patterns) >= 3


@pytest.mark.parametrize("task_text", [
    "How many lines does the bill from Acme Corp have?",
    "What is the number of lines on the bill from TechSupplies?",
    "What was the purchased date on the bill from Widgets Inc?",
    "What is the quantity of Widget X on my bill from SupplyHouse?",
    "What was the price of the premium plan on my bill from CloudHost?",
])
def test_bill_query_routing_matches(task_text: str) -> None:
    router = load_router(SKILLS_DIR)
    decision = router.route(task_text)
    assert decision.category == "BILL_QUERY", f"Expected BILL_QUERY for: {task_text!r}, got {decision.category}"
