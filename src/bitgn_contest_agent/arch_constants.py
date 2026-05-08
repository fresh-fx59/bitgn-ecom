# src/bitgn_contest_agent/arch_constants.py
"""Architecture observability enums — single source of truth.

Shared by: logic branches (validator.py, agent.py), JSONL schema
(trace_schema.TraceArch), log line formatter (arch_log.py), and
analyser CLI (scripts/arch_report.py). Renaming a member here
propagates to every consumer.
"""
from __future__ import annotations

from enum import StrEnum


class ArchCategory(StrEnum):
    SKILL_ROUTER = "SKILL_ROUTER"
    REACTIVE = "REACTIVE"
    VALIDATOR_T1 = "VALIDATOR_T1"
    VALIDATOR_T2 = "VALIDATOR_T2"
    TERMINAL = "TERMINAL"
    TERMINAL_R4 = "TERMINAL_R4"
    LOOP_NUDGE = "LOOP_NUDGE"
    FORMAT_VALIDATOR = "FORMAT_VALIDATOR"
    BODY_PRESERVATION = "BODY_PRESERVATION"
    TASK_START = "TASK_START"
    FORMAT_PRE_WRITE_REJECT = "FORMAT_PRE_WRITE_REJECT"


class ValidatorT1Rule(StrEnum):
    CONTRADICTION_OK_NEG = "contradiction_ok_neg"
    CONTRADICTION_CLAR_POS = "contradiction_clar_pos"
    DANGEROUS_DENIED_TO_OK = "dangerous_denied_to_ok"
    MUTATION_GUARD = "mutation_guard"


class ValidatorT2Trigger(StrEnum):
    FIRST_TRANSITION = "first_transition"
    CLARIFICATION = "clarification"
    INBOX_READ = "inbox_read"
    PROGRESS_CHECK = "progress_check"
    ENTITY_FINANCE_SEARCH = "entity_finance_search"


class ArchResult(StrEnum):
    OK = "OK"
    CORRECTED = "CORRECTED"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    MISMATCH = "MISMATCH"
    CONSISTENT = "CONSISTENT"


class RouterSource(StrEnum):
    TIER1_REGEX = "tier1_regex"
    TIER2_LLM = "tier2_llm"
    # Adapter-extra: per-model ModelAdapter.extra_reactive_skills hook
    # loaded this skill at task start. Distinct from tier1/tier2 because
    # the adapter hook runs after the global router and only for models
    # whose tier1 regex is known to miss. See gpt-oss v0.1.25.
    ADAPTER_EXTRA = "adapter_extra"
    NONE = "none"
