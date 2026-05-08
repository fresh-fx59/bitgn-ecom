# tests/test_arch_constants.py
"""Enums drive logic, schema, log strings, and analyser filters."""
from __future__ import annotations

from bitgn_contest_agent.arch_constants import (
    ArchCategory,
    ValidatorT1Rule,
    ValidatorT2Trigger,
    ArchResult,
    RouterSource,
)


def test_arch_category_members() -> None:
    names = {c.name for c in ArchCategory}
    assert names == {
        "SKILL_ROUTER", "REACTIVE", "VALIDATOR_T1", "VALIDATOR_T2",
        "TERMINAL", "TERMINAL_R4", "LOOP_NUDGE",
        "FORMAT_VALIDATOR", "BODY_PRESERVATION", "TASK_START",
        "FORMAT_PRE_WRITE_REJECT",
    }


def test_arch_category_value_equals_name() -> None:
    for cat in ArchCategory:
        assert cat.value == cat.name


def test_validator_t1_rule_values() -> None:
    assert ValidatorT1Rule.CONTRADICTION_OK_NEG.value == "contradiction_ok_neg"
    assert ValidatorT1Rule.CONTRADICTION_CLAR_POS.value == "contradiction_clar_pos"
    assert ValidatorT1Rule.DANGEROUS_DENIED_TO_OK.value == "dangerous_denied_to_ok"
    assert ValidatorT1Rule.MUTATION_GUARD.value == "mutation_guard"


def test_validator_t2_trigger_values() -> None:
    assert ValidatorT2Trigger.FIRST_TRANSITION.value == "first_transition"
    assert ValidatorT2Trigger.CLARIFICATION.value == "clarification"
    assert ValidatorT2Trigger.INBOX_READ.value == "inbox_read"
    assert ValidatorT2Trigger.PROGRESS_CHECK.value == "progress_check"
    assert ValidatorT2Trigger.ENTITY_FINANCE_SEARCH.value == "entity_finance_search"


def test_arch_result_values() -> None:
    assert ArchResult.OK.value == "OK"
    assert ArchResult.CORRECTED.value == "CORRECTED"
    assert ArchResult.ACCEPT.value == "ACCEPT"
    assert ArchResult.REJECT.value == "REJECT"
    assert ArchResult.MISMATCH.value == "MISMATCH"
    assert ArchResult.CONSISTENT.value == "CONSISTENT"


def test_router_source_values() -> None:
    assert RouterSource.TIER1_REGEX.value == "tier1_regex"
    assert RouterSource.TIER2_LLM.value == "tier2_llm"
    assert RouterSource.NONE.value == "none"


def test_all_are_str_subclass() -> None:
    # StrEnum members are str — logs and JSON serialize without .value
    assert isinstance(ArchCategory.VALIDATOR_T1, str)
    assert f"{ArchCategory.VALIDATOR_T1}" == "VALIDATOR_T1"
