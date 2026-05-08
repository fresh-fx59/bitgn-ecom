from bitgn_contest_agent.verify import (
    VerifyReason, WriteOp, should_verify,
)
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion
from bitgn_contest_agent.session import Session


def _completion(message: str, refs=None, outcome="OUTCOME_OK") -> NextStep:
    return NextStep(
        current_state="done",
        plan_remaining_steps_brief=["submit"],
        identity_verified=True,
        observation="ready",
        outcome_leaning=outcome,
        function=ReportTaskCompletion(
            tool="report_completion",
            message=message,
            grounding_refs=list(refs or []),
            rulebook_notes="n/a",
            outcome_justification="n/a",
            completed_steps_laconic=["done"],
            outcome=outcome,
        ),
    )


# ── MISSING_REF ──────────────────────────────────────────────────────

def test_missing_ref_fires_when_answer_cites_unread_path():
    ns = _completion(
        message="see 40_projects/hearthline/README.md for details",
        refs=["40_projects/hearthline/README.md"],
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},  # never read that path
        write_history=[],
        task_text="when did the project start?",
        skill_name="project-involvement",
    )
    assert VerifyReason.MISSING_REF in reasons


def test_missing_ref_quiet_when_path_was_read():
    ns = _completion(
        message="see 40_projects/hearthline/README.md",
        refs=["40_projects/hearthline/README.md"],
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={"40_projects/hearthline/README.md": "…"},
        write_history=[],
        task_text="when did the project start?",
        skill_name="project-involvement",
    )
    assert VerifyReason.MISSING_REF not in reasons


def test_missing_ref_quiet_on_freeform_no_paths():
    ns = _completion(message="Nothing to cite.")
    reasons = should_verify(
        next_step=ns, session=Session(), read_cache={},
        write_history=[], task_text="describe", skill_name=None,
    )
    assert VerifyReason.MISSING_REF not in reasons


# ── NUMERIC_MULTIREF ─────────────────────────────────────────────────

def test_numeric_multiref_fires_on_scalar_with_many_records():
    ns = _completion(message="12")
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={
            "50_finance/purchases/bill_a.md": "amount: 6",
            "50_finance/purchases/bill_b.md": "amount: 6",
        },
        write_history=[],
        task_text="how much did vendor X charge for relay modules? Number only.",
        skill_name="finance-lookup",
    )
    assert VerifyReason.NUMERIC_MULTIREF in reasons


def test_numeric_multiref_quiet_with_single_record():
    ns = _completion(message="6")
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={"50_finance/purchases/bill_a.md": "amount: 6"},
        write_history=[],
        task_text="how much did vendor X charge? Number only.",
        skill_name="finance-lookup",
    )
    assert VerifyReason.NUMERIC_MULTIREF not in reasons


def test_numeric_multiref_quiet_on_freeform():
    ns = _completion(message="about half the sum")
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={
            "50_finance/purchases/bill_a.md": "amount: 6",
            "50_finance/purchases/bill_b.md": "amount: 6",
        },
        write_history=[],
        task_text="summarize the billing", skill_name=None,
    )
    assert VerifyReason.NUMERIC_MULTIREF not in reasons


# ── priority ordering ───────────────────────────────────────────────

def test_reasons_return_in_priority_order():
    # Contrive a completion that trips both MISSING_REF and NUMERIC_MULTIREF.
    ns = _completion(
        message="12 (see 50_finance/purchases/bill_a.md and bill_b.md)",
        refs=["50_finance/purchases/bill_a.md",
              "50_finance/purchases/bill_b.md"],
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={
            # Only bill_a was read; cited bill_b unread → MISSING_REF
            "50_finance/purchases/bill_a.md": "amount: 6",
        },
        write_history=[],
        task_text="how much did vendor X charge? Number only.",
        skill_name="finance-lookup",
    )
    # Spec §4: MISSING_REF ranks higher than NUMERIC_MULTIREF.
    assert reasons[0] == VerifyReason.MISSING_REF


def test_inbox_giveup_fires_on_none_clarification_without_write():
    ns = _completion(
        message="I need more info.",
        outcome="OUTCOME_NONE_CLARIFICATION",
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},
        write_history=[],
        task_text="take care of the next message in inbox",
        skill_name="inbox-processing",
    )
    assert VerifyReason.INBOX_GIVEUP in reasons


def test_inbox_giveup_quiet_when_outbox_write_exists():
    from bitgn_contest_agent.verify import WriteOp
    ns = _completion(
        message="I need more info.",
        outcome="OUTCOME_NONE_CLARIFICATION",
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},
        write_history=[
            WriteOp(op="write", path="60_outbox/outbox/eml_x.md",
                    step=2, content="reply body"),
        ],
        task_text="take care of the next message in inbox",
        skill_name="inbox-processing",
    )
    assert VerifyReason.INBOX_GIVEUP not in reasons


def test_inbox_giveup_quiet_on_non_inbox_skill():
    ns = _completion(
        message="I need more info.",
        outcome="OUTCOME_NONE_CLARIFICATION",
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},
        write_history=[],
        task_text="how much did vendor X charge?",
        skill_name="finance-lookup",
    )
    assert VerifyReason.INBOX_GIVEUP not in reasons


def test_inbox_giveup_quiet_on_ok_outcome():
    ns = _completion(
        message="done",
        outcome="OUTCOME_OK",
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},
        write_history=[],
        task_text="take care of the next message in inbox",
        skill_name="inbox-processing",
    )
    assert VerifyReason.INBOX_GIVEUP not in reasons
