"""Validator tests — Tier 1 rules + terminal checks (migrated from test_enforcer.py)."""
from __future__ import annotations

from bitgn_contest_agent.validator import StepValidator, Verdict
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion
from bitgn_contest_agent.session import Session


def _mk_step(
    function: dict,
    *,
    observation: str = "step observation",
    outcome_leaning: str = "GATHERING_INFORMATION",
) -> NextStep:
    return NextStep(
        current_state="x",
        plan_remaining_steps_brief=["do", "report"],
        identity_verified=True,
        observation=observation,
        outcome_leaning=outcome_leaning,
        function=function,
    )


def _mk_terminal(outcome: str, refs: list[str]) -> NextStep:
    return NextStep(
        current_state="done",
        plan_remaining_steps_brief=["report"],
        identity_verified=True,
        observation="completed analysis",
        outcome_leaning=outcome if outcome != "OUTCOME_ERR_INTERNAL" else "OUTCOME_OK",
        function=ReportTaskCompletion(
            tool="report_completion",
            message="all good",
            grounding_refs=refs,
            rulebook_notes="n",
            outcome_justification="j",
            completed_steps_laconic=["read AGENTS.md"],
            outcome=outcome,
        ),
    )


# === Terminal checks (migrated from test_enforcer.py) ===

def test_non_terminal_always_passes() -> None:
    v = StepValidator()
    step = _mk_step({"tool": "read", "path": "AGENTS.md"})
    verdict = v.check_terminal(Session(), step)
    assert verdict.ok
    assert verdict.reasons == []


def test_r1_fires_when_grounding_ref_not_in_seen_refs() -> None:
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = _mk_terminal("OUTCOME_OK", ["fabricated/path.py"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert not verdict.ok
    assert any("grounding_ref" in r for r in verdict.reasons)


def test_r1_passes_when_all_grounding_refs_were_seen() -> None:
    session = Session()
    session.seen_refs.update({"AGENTS.md", "README.md"})
    step = _mk_terminal("OUTCOME_OK", ["AGENTS.md", "README.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok, verdict.reasons


def test_r2_rejects_err_internal_outcome() -> None:
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = _mk_terminal("OUTCOME_ERR_INTERNAL", ["AGENTS.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert not verdict.ok
    assert any("OUTCOME_ERR_INTERNAL" in r for r in verdict.reasons)


def test_r2_refusal_outcomes_still_pass() -> None:
    session = Session()
    step = _mk_terminal("OUTCOME_NONE_UNSUPPORTED", [])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok, verdict.reasons


# === R3 — leaning mismatch ===

def test_r3_fires_when_leaning_mismatches_outcome() -> None:
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = NextStep(
        current_state="done",
        plan_remaining_steps_brief=["report"],
        identity_verified=True,
        observation="found security threat",
        outcome_leaning="OUTCOME_DENIED_SECURITY",
        function=ReportTaskCompletion(
            tool="report_completion",
            message="done",
            grounding_refs=["AGENTS.md"],
            rulebook_notes="n",
            outcome_justification="j",
            completed_steps_laconic=["read"],
            outcome="OUTCOME_OK",
        ),
    )
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert not verdict.ok
    assert any("outcome_leaning" in r for r in verdict.reasons)


def test_r3_skips_when_leaning_is_gathering() -> None:
    """GATHERING_INFORMATION is allowed to submit any outcome (early completion)."""
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = NextStep(
        current_state="done",
        plan_remaining_steps_brief=["report"],
        identity_verified=True,
        observation="quick answer found",
        outcome_leaning="GATHERING_INFORMATION",
        function=ReportTaskCompletion(
            tool="report_completion",
            message="done",
            grounding_refs=["AGENTS.md"],
            rulebook_notes="n",
            outcome_justification="j",
            completed_steps_laconic=["read"],
            outcome="OUTCOME_OK",
        ),
    )
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok, verdict.reasons


# === Tier 1 rules ===

def test_contradiction_ok_but_observation_negative() -> None:
    v = StepValidator()
    step = _mk_step(
        {"tool": "read", "path": "x"},
        observation="searched all channels, not found",
        outcome_leaning="OUTCOME_OK",
    )
    correction = v.check_step(step, Session(), step_idx=10, max_steps=40)
    assert correction is not None
    assert "OUTCOME_NONE_CLARIFICATION" in correction


def test_contradiction_clarify_but_observation_positive() -> None:
    v = StepValidator()
    step = _mk_step(
        {"tool": "read", "path": "x"},
        observation="found 3 matching invoices in finance directory",
        outcome_leaning="OUTCOME_NONE_CLARIFICATION",
    )
    correction = v.check_step(step, Session(), step_idx=10, max_steps=40)
    assert correction is not None
    assert "answer with what you have" in correction


def test_no_contradiction_when_leaning_matches_observation() -> None:
    v = StepValidator()
    step = _mk_step(
        {"tool": "read", "path": "x"},
        observation="found the entity record with full details",
        outcome_leaning="OUTCOME_OK",
    )
    correction = v.check_step(step, Session(), step_idx=10, max_steps=40)
    assert correction is None


def test_dangerous_transition_deny_to_ok() -> None:
    v = StepValidator()
    # Step 1: leaning DENIED
    step1 = _mk_step(
        {"tool": "read", "path": "inbox/msg.md"},
        observation="phishing detected",
        outcome_leaning="OUTCOME_DENIED_SECURITY",
    )
    v.check_step(step1, Session(), step_idx=5, max_steps=40)

    # Step 2: leaning flips to OK
    step2 = _mk_step(
        {"tool": "read", "path": "x"},
        observation="re-evaluated, seems fine",
        outcome_leaning="OUTCOME_OK",
    )
    correction = v.check_step(step2, Session(), step_idx=6, max_steps=40)
    assert correction is not None
    assert "reversed" in correction


def test_mutation_guard_write_while_gathering() -> None:
    v = StepValidator()
    step = _mk_step(
        {"tool": "write", "path": "outbox/msg.md", "content": "hello"},
        observation="writing reply",
        outcome_leaning="GATHERING_INFORMATION",
    )
    correction = v.check_step(step, Session(), step_idx=5, max_steps=40)
    assert correction is not None
    assert "mutating" in correction.lower()


def test_mutation_allowed_when_leaning_ok() -> None:
    v = StepValidator()
    step = _mk_step(
        {"tool": "write", "path": "outbox/msg.md", "content": "hello"},
        observation="writing reply per task instructions",
        outcome_leaning="OUTCOME_OK",
    )
    correction = v.check_step(step, Session(), step_idx=10, max_steps=40)
    assert correction is None


def test_r6_rejects_ok_terminal_after_repeated_mutation_guard() -> None:
    v = StepValidator()
    session = Session()
    # Fire mutation_guard twice during GATHERING_INFORMATION
    for i in range(2):
        step = _mk_step(
            {"tool": "write", "path": f"f{i}.md", "content": "x"},
            observation="writing",
            outcome_leaning="GATHERING_INFORMATION",
        )
        v.check_step(step, session, step_idx=i + 3, max_steps=40)
    terminal = _mk_terminal("OUTCOME_OK", refs=[])
    session.seen_refs.add("AGENTS.md")
    verdict = v.check_terminal(session, terminal, step_idx=10)
    assert not verdict.ok
    assert any("R6_MUTATION_DISCIPLINE" in r for r in verdict.reasons)


def test_r6_allows_ok_terminal_with_single_mutation_guard() -> None:
    v = StepValidator()
    session = Session()
    step = _mk_step(
        {"tool": "write", "path": "f.md", "content": "x"},
        observation="writing",
        outcome_leaning="GATHERING_INFORMATION",
    )
    v.check_step(step, session, step_idx=3, max_steps=40)
    terminal = _mk_terminal("OUTCOME_OK", refs=[])
    verdict = v.check_terminal(session, terminal, step_idx=10)
    assert verdict.ok


def test_r6_skips_non_ok_outcome_after_repeated_guards() -> None:
    v = StepValidator()
    session = Session()
    for i in range(3):
        step = _mk_step(
            {"tool": "write", "path": f"f{i}.md", "content": "x"},
            observation="writing",
            outcome_leaning="GATHERING_INFORMATION",
        )
        v.check_step(step, session, step_idx=i + 3, max_steps=40)
    terminal = _mk_terminal("OUTCOME_DENIED_SECURITY", refs=[])
    verdict = v.check_terminal(session, terminal, step_idx=10)
    assert verdict.ok


# === R7 — inbox-processing cleanup ===
#
# Evidence: 2026-04-23 gpt-oss-120b PROD run, 16/36 failures terminated
# OUTCOME_OK without deleting the consumed trigger. Rule is keyed on
# skill identity ("inbox-processing") and the presence of any delete in
# session.mutations — path-agnostic.


def test_r7_rejects_ok_when_inbox_processing_skipped_delete() -> None:
    v = StepValidator()
    session = Session()
    session.skills_loaded.add("inbox-processing")
    session.mutations.append(("write", "anywhere/x.md"))  # wrote but didn't delete
    terminal = _mk_terminal("OUTCOME_OK", refs=[])
    verdict = v.check_terminal(session, terminal, step_idx=10)
    assert not verdict.ok
    assert any("R7_INBOX_CLEANUP" in r for r in verdict.reasons)


def test_r7_allows_ok_when_delete_present() -> None:
    v = StepValidator()
    session = Session()
    session.skills_loaded.add("inbox-processing")
    session.mutations.append(("write", "anywhere/x.md"))
    session.mutations.append(("delete", "anywhere/trigger.md"))
    terminal = _mk_terminal("OUTCOME_OK", refs=[])
    verdict = v.check_terminal(session, terminal, step_idx=10)
    assert verdict.ok, verdict.reasons


def test_r7_skips_when_inbox_processing_not_loaded() -> None:
    """Rule keys on skill identity; other skills aren't subject to it."""
    v = StepValidator()
    session = Session()
    session.skills_loaded.add("finance-lookup")
    # No delete performed — but also not an inbox-processing task.
    terminal = _mk_terminal("OUTCOME_OK", refs=[])
    verdict = v.check_terminal(session, terminal, step_idx=10)
    assert verdict.ok, verdict.reasons


def test_r7_skips_for_non_ok_outcomes() -> None:
    """CLARIFICATION / DENIED_SECURITY / UNSUPPORTED mean no work was
    done, so no cleanup is owed."""
    v = StepValidator()
    session = Session()
    session.skills_loaded.add("inbox-processing")
    for outcome in (
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_UNSUPPORTED",
    ):
        terminal = _mk_terminal(outcome, refs=[])
        verdict = v.check_terminal(session, terminal, step_idx=10)
        assert verdict.ok, (outcome, verdict.reasons)


def test_r7_any_delete_satisfies_rule() -> None:
    """The rule is path-agnostic: a move or write does not count, but
    any ``delete`` does — the skill's Step 4 is the only thing graded."""
    v = StepValidator()
    session = Session()
    session.skills_loaded.add("inbox-processing")
    # A move is a mutation but not a delete — still rejects.
    session.mutations.append(("move", "a.md"))
    terminal = _mk_terminal("OUTCOME_OK", refs=[])
    verdict = v.check_terminal(session, terminal, step_idx=10)
    assert not verdict.ok
    assert any("R7_INBOX_CLEANUP" in r for r in verdict.reasons)


def test_stale_gathering_disabled() -> None:
    """Stale gathering rule was disabled — Tier 2 progress check at 60%
    covers this with LLM judgment instead of a dumb threshold."""
    v = StepValidator()
    step = _mk_step(
        {"tool": "read", "path": "x"},
        observation="still looking",
        outcome_leaning="GATHERING_INFORMATION",
    )
    # Even past 40% threshold, no correction fires
    correction = v.check_step(step, Session(), step_idx=17, max_steps=40)
    assert correction is None


def test_correction_budget_exhaustion() -> None:
    v = StepValidator(max_corrections=2)
    step = _mk_step(
        {"tool": "read", "path": "x"},
        observation="not found anything",
        outcome_leaning="OUTCOME_OK",
    )
    # First two fire
    assert v.check_step(step, Session(), step_idx=10, max_steps=40) is not None
    assert v.check_step(step, Session(), step_idx=11, max_steps=40) is not None
    # Third is budget-exhausted
    assert v.check_step(step, Session(), step_idx=12, max_steps=40) is None
    assert v.corrections_emitted == 2


def test_no_correction_returns_none() -> None:
    v = StepValidator()
    step = _mk_step(
        {"tool": "read", "path": "AGENTS.md"},
        observation="read workspace rules, 450 bytes",
        outcome_leaning="GATHERING_INFORMATION",
    )
    correction = v.check_step(step, Session(), step_idx=3, max_steps=40)
    assert correction is None
    assert v.corrections_emitted == 0


# === Tier 2 trigger structure tests ===

def test_trigger_first_transition_fires_once(monkeypatch) -> None:
    """First transition from GATHERING fires at most once."""
    import bitgn_contest_agent.classifier as cls_mod

    calls = []
    def fake_classify(*, system, user):
        calls.append(1)
        return {"category": "PREMATURE", "confidence": 0.8}

    monkeypatch.setattr(cls_mod, "classify", fake_classify)

    v = StepValidator()
    # Step 1: still gathering
    s1 = _mk_step({"tool": "read", "path": "x"}, observation="exploring", outcome_leaning="GATHERING_INFORMATION")
    v.check_step(s1, Session(), step_idx=3, max_steps=40)

    # Step 2: transitions to OK — should trigger
    s2 = _mk_step({"tool": "read", "path": "x"}, observation="found it", outcome_leaning="OUTCOME_OK")
    corr = v.check_step(s2, Session(), step_idx=4, max_steps=40)
    assert corr is not None
    assert "committed" in corr.lower()
    assert len(calls) == 1

    # Step 3: another transition — should NOT trigger again
    s3 = _mk_step({"tool": "read", "path": "x"}, observation="re-exploring", outcome_leaning="GATHERING_INFORMATION")
    v.check_step(s3, Session(), step_idx=5, max_steps=40)
    s4 = _mk_step({"tool": "read", "path": "x"}, observation="found again", outcome_leaning="OUTCOME_OK")
    v.check_step(s4, Session(), step_idx=6, max_steps=40)
    assert len(calls) == 1  # no second call


def test_trigger_classifier_failure_is_swallowed(monkeypatch) -> None:
    """Classifier errors don't crash the validator."""
    import bitgn_contest_agent.classifier as cls_mod

    def fail_classify(*, system, user):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(cls_mod, "classify", fail_classify)

    v = StepValidator()
    s1 = _mk_step({"tool": "read", "path": "x"}, observation="exploring", outcome_leaning="GATHERING_INFORMATION")
    v.check_step(s1, Session(), step_idx=3, max_steps=40)
    s2 = _mk_step({"tool": "read", "path": "x"}, observation="found", outcome_leaning="OUTCOME_OK")
    corr = v.check_step(s2, Session(), step_idx=4, max_steps=40)
    assert corr is None  # error swallowed, no correction


def test_observation_window_limited_to_5() -> None:
    """Observations window stays at most 5 entries."""
    v = StepValidator()
    for i in range(10):
        step = _mk_step(
            {"tool": "read", "path": "x"},
            observation=f"obs {i}",
            outcome_leaning="GATHERING_INFORMATION",
        )
        v.check_step(step, Session(), step_idx=i + 1, max_steps=40)
    assert len(v._observations) == 5
    assert v._observations[0] == "obs 5"


def test_r4_mutation_mismatch_rejects(monkeypatch) -> None:
    """R4: agent claims 2 deletes but session only has 1 → reject."""
    import bitgn_contest_agent.classifier as cls_mod

    def fake_classify(*, system, user):
        return {"category": "MISMATCH", "confidence": 0.9}

    monkeypatch.setattr(cls_mod, "classify", fake_classify)

    session = Session()
    session.seen_refs.add("AGENTS.md")
    session.seen_refs.add("50_finance/receipt_a.md")
    session.mutations.append(("delete", "50_finance/receipt_a.md"))

    step = NextStep(
        current_state="done",
        plan_remaining_steps_brief=["report"],
        identity_verified=True,
        observation="deleted both receipts",
        outcome_leaning="OUTCOME_OK",
        function=ReportTaskCompletion(
            tool="report_completion",
            message="deleted both",
            grounding_refs=["AGENTS.md", "50_finance/receipt_a.md"],
            rulebook_notes="n",
            outcome_justification="deleted receipt_a and receipt_b",
            completed_steps_laconic=[
                "read receipt_a", "delete receipt_a",
                "read receipt_b", "delete receipt_b",
            ],
            outcome="OUTCOME_OK",
        ),
    )
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert not verdict.ok
    assert any("mutation" in r.lower() for r in verdict.reasons)


def test_r4_skipped_when_no_mutations_claimed(monkeypatch) -> None:
    """R4 should not fire when there are no mutations at all."""
    import bitgn_contest_agent.classifier as cls_mod

    calls = []
    def fake_classify(*, system, user):
        calls.append(1)
        return {"category": "CONSISTENT", "confidence": 0.9}

    monkeypatch.setattr(cls_mod, "classify", fake_classify)

    session = Session()
    session.seen_refs.add("AGENTS.md")

    step = _mk_terminal("OUTCOME_OK", ["AGENTS.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok
    assert len(calls) == 0  # no LLM call — no mutations to check


def test_r4_passes_when_mutations_consistent(monkeypatch) -> None:
    """R4 accepts when claimed steps match actual mutations."""
    import bitgn_contest_agent.classifier as cls_mod

    def fake_classify(*, system, user):
        return {"category": "CONSISTENT", "confidence": 0.9}

    monkeypatch.setattr(cls_mod, "classify", fake_classify)

    session = Session()
    session.seen_refs.add("AGENTS.md")
    session.mutations.append(("delete", "50_finance/receipt.md"))

    step = NextStep(
        current_state="done",
        plan_remaining_steps_brief=["report"],
        identity_verified=True,
        observation="deleted receipt",
        outcome_leaning="OUTCOME_OK",
        function=ReportTaskCompletion(
            tool="report_completion",
            message="deleted receipt",
            grounding_refs=["AGENTS.md"],
            rulebook_notes="n",
            outcome_justification="deleted the receipt",
            completed_steps_laconic=["read receipt", "delete receipt"],
            outcome="OUTCOME_OK",
        ),
    )
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok


# === Tier 2 trigger: entity-graph search ===

def test_trigger_entity_search_fires_on_finance_name_search(monkeypatch) -> None:
    """Trigger fires when agent searches finance dirs by what looks like a person name."""
    import bitgn_contest_agent.classifier as cls_mod

    def fake_classify(*, system, user):
        return {"category": "PERSON_NAME", "confidence": 0.85}

    monkeypatch.setattr(cls_mod, "classify", fake_classify)

    v = StepValidator()
    # Step 1: gathering info
    s1 = _mk_step({"tool": "read", "path": "x"}, observation="exploring", outcome_leaning="GATHERING_INFORMATION")
    v.check_step(s1, Session(), step_idx=3, max_steps=40)

    # Step 2: agent searches finance dir with what looks like a person name
    s2 = _mk_step(
        {"tool": "search", "root": "50_finance", "pattern": "John Smith", "limit": 10},
        observation="searched 50_finance for John Smith, 0 matches",
        outcome_leaning="GATHERING_INFORMATION",
    )
    correction = v.check_step(s2, Session(), step_idx=4, max_steps=40)
    assert correction is not None
    assert "entity" in correction.lower() or "identifier" in correction.lower() or "canonical" in correction.lower()


def test_trigger_entity_search_does_not_fire_for_non_finance(monkeypatch) -> None:
    """Trigger should not fire for searches outside finance directories."""
    import bitgn_contest_agent.classifier as cls_mod

    calls = []
    def fake_classify(*, system, user):
        calls.append(1)
        return {"category": "PERSON_NAME", "confidence": 0.85}

    monkeypatch.setattr(cls_mod, "classify", fake_classify)

    v = StepValidator()
    s1 = _mk_step({"tool": "read", "path": "x"}, observation="exploring", outcome_leaning="GATHERING_INFORMATION")
    v.check_step(s1, Session(), step_idx=3, max_steps=40)

    # Search in a non-finance directory — should NOT trigger
    s2 = _mk_step(
        {"tool": "search", "root": "30_knowledge", "pattern": "John Smith", "limit": 10},
        observation="searched knowledge base for John Smith",
        outcome_leaning="GATHERING_INFORMATION",
    )
    correction = v.check_step(s2, Session(), step_idx=4, max_steps=40)
    assert correction is None
    assert len(calls) == 0  # no classifier call


def test_trigger_entity_search_fires_only_once(monkeypatch) -> None:
    """Entity search trigger fires at most once per run."""
    import bitgn_contest_agent.classifier as cls_mod

    calls = []
    def fake_classify(*, system, user):
        calls.append(1)
        return {"category": "PERSON_NAME", "confidence": 0.85}

    monkeypatch.setattr(cls_mod, "classify", fake_classify)

    v = StepValidator()
    s1 = _mk_step({"tool": "read", "path": "x"}, observation="exploring", outcome_leaning="GATHERING_INFORMATION")
    v.check_step(s1, Session(), step_idx=3, max_steps=40)

    # First finance search — should trigger
    s2 = _mk_step(
        {"tool": "search", "root": "50_finance", "pattern": "Jane Doe", "limit": 10},
        observation="searched finance for Jane Doe",
        outcome_leaning="GATHERING_INFORMATION",
    )
    corr1 = v.check_step(s2, Session(), step_idx=4, max_steps=40)
    assert corr1 is not None
    assert len(calls) == 1

    # Second finance search — should NOT trigger again
    s3 = _mk_step(
        {"tool": "search", "root": "50_finance/invoices", "pattern": "Bob Jones", "limit": 10},
        observation="searched invoices for Bob Jones",
        outcome_leaning="GATHERING_INFORMATION",
    )
    corr2 = v.check_step(s3, Session(), step_idx=5, max_steps=40)
    # Should be None (trigger already fired) — or if another rule fires, that's OK too
    # The key assertion is no second classifier call
    assert len(calls) == 1


def test_validator_t1_rule_emits_arch_record(tmp_path) -> None:
    """Tier 1 mutation_guard rule writes a TraceArch via context writer."""
    from bitgn_contest_agent.arch_log import set_task_context, reset_task_context
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    from bitgn_contest_agent.arch_constants import (
        ArchCategory, ValidatorT1Rule,
    )
    from bitgn_contest_agent.validator import StepValidator
    from bitgn_contest_agent.schemas import NextStep
    from bitgn_contest_agent.session import Session

    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    token = set_task_context(
        task_id="t1", run_index=0, trace_name="t.jsonl", writer=writer,
    )
    try:
        v = StepValidator()
        step = _mk_step(
            {"tool": "write", "path": "outbox/foo.md", "content": "x"},
            observation="about to write",
            outcome_leaning="GATHERING_INFORMATION",
        )
        sess = Session()
        v.check_step(step, sess, step_idx=2, max_steps=20,
                     reactive_injected_this_step=False)
    finally:
        reset_task_context(token)
        writer.close()

    arch = [r for r in load_jsonl(p) if isinstance(r, TraceArch)]
    assert any(
        r.category == ArchCategory.VALIDATOR_T1
        and r.rule == ValidatorT1Rule.MUTATION_GUARD
        for r in arch
    )


def test_validator_terminal_emits_arch_record(tmp_path) -> None:
    from bitgn_contest_agent.arch_log import set_task_context, reset_task_context
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    from bitgn_contest_agent.arch_constants import ArchCategory, ArchResult
    from bitgn_contest_agent.validator import StepValidator
    from bitgn_contest_agent.schemas import NextStep
    from bitgn_contest_agent.session import Session

    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    token = set_task_context(
        task_id="t1", run_index=0, trace_name="t.jsonl", writer=writer,
    )
    try:
        v = StepValidator()
        step = _mk_terminal("OUTCOME_OK", [])
        sess = Session()
        v.check_terminal(sess, step)
    finally:
        reset_task_context(token)
        writer.close()

    arch = [r for r in load_jsonl(p) if isinstance(r, TraceArch)]
    assert any(
        r.category == ArchCategory.TERMINAL and r.result == ArchResult.ACCEPT
        for r in arch
    )


# === R1 correctness fixes: case-insensitive match + verified-absent ===

def test_r1_is_case_insensitive_on_filename() -> None:
    """AGENTS.MD in grounding_refs must match AGENTS.md in seen_refs.

    Regression: 18 of 20 terminal REJECTs on trace
    logs/20260414_184041 were this exact false positive.
    """
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = _mk_terminal("OUTCOME_OK", ["AGENTS.MD"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok, verdict.reasons


def test_r1_is_case_insensitive_on_nested_path() -> None:
    session = Session()
    session.seen_refs.add("10_entities/cast/Renate.md")
    step = _mk_terminal("OUTCOME_OK", ["10_entities/cast/renate.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok, verdict.reasons


def test_r1_accepts_verified_absent_as_negative_evidence() -> None:
    """Agent cites file-not-found result as grounding_ref. Legitimate."""
    session = Session()
    session.seen_refs.add("AGENTS.md")
    session.attempted_reads.add("00_inbox/556_next-task.md")
    session.verified_absent.add("00_inbox/556_next-task.md")
    step = _mk_terminal(
        "OUTCOME_OK", ["AGENTS.md", "00_inbox/556_next-task.md"],
    )
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok, verdict.reasons


def test_r1_rejects_ref_never_attempted_and_not_seen() -> None:
    """Baseline stays: pure fabrication still rejected."""
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = _mk_terminal("OUTCOME_OK", ["fabricated/never-touched.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert not verdict.ok
    assert any("grounding_ref" in r for r in verdict.reasons)


def test_r1_attempted_but_not_verified_absent_still_rejects() -> None:
    """Attempt without file-not-found (e.g. permission denied) is NOT grounding."""
    session = Session()
    session.seen_refs.add("AGENTS.md")
    session.attempted_reads.add("private/locked.md")
    # Note: NOT in verified_absent (different error, e.g. permission)
    step = _mk_terminal("OUTCOME_OK", ["AGENTS.md", "private/locked.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert not verdict.ok
    assert any("grounding_ref" in r for r in verdict.reasons)


# === R5: outbox attachment grounding ===

def test_r5_rejects_unread_outbox_attachment() -> None:
    """PROD t097 2026-04-20: agent attached 4 invoices but only read 1.
    R5 forces agent to read every attached file before terminal."""
    session = Session()
    session.seen_refs.add("AGENTS.md")
    session.seen_refs.add("50_finance/invoices/inv_0004.md")
    session.outbox_attachments = {
        "50_finance/invoices/inv_0001.md",
        "50_finance/invoices/inv_0002.md",
        "50_finance/invoices/inv_0003.md",
        "50_finance/invoices/inv_0004.md",
    }
    step = _mk_terminal("OUTCOME_OK", ["AGENTS.md", "50_finance/invoices/inv_0004.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert not verdict.ok
    # Should flag the 3 unread attachments
    att_reasons = [r for r in verdict.reasons if "outbox attachment" in r]
    assert len(att_reasons) == 3
    assert any("inv_0001" in r for r in att_reasons)
    assert any("inv_0002" in r for r in att_reasons)
    assert any("inv_0003" in r for r in att_reasons)


def test_r5_passes_when_all_attachments_read() -> None:
    """R5 passes when agent has read every outbox attachment."""
    session = Session()
    session.seen_refs.update([
        "AGENTS.md",
        "50_finance/invoices/inv_0001.md",
        "50_finance/invoices/inv_0002.md",
        "50_finance/invoices/inv_0003.md",
        "50_finance/invoices/inv_0004.md",
    ])
    session.outbox_attachments = {
        "50_finance/invoices/inv_0001.md",
        "50_finance/invoices/inv_0002.md",
        "50_finance/invoices/inv_0003.md",
        "50_finance/invoices/inv_0004.md",
    }
    step = _mk_terminal("OUTCOME_OK", list(session.seen_refs))
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok


def test_r5_no_outbox_attachments_passes() -> None:
    """R5 is a no-op when there are no outbox writes."""
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = _mk_terminal("OUTCOME_OK", ["AGENTS.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok


# -- R0: minimum exploration ------------------------------------------------

def test_r0_rejects_early_outcome_ok() -> None:
    """report_completion with OUTCOME_OK at step 1 must be rejected."""
    session = Session()
    step = _mk_terminal("OUTCOME_OK", [])
    v = StepValidator()
    verdict = v.check_terminal(session, step, step_idx=1)
    assert not verdict.ok
    assert any("R0_MIN_EXPLORE" in r for r in verdict.reasons)


def test_r0_allows_early_denied_security() -> None:
    """DENIED_SECURITY is valid at any step — immediate refusal is fine."""
    session = Session()
    step = _mk_terminal("OUTCOME_DENIED_SECURITY", [])
    v = StepValidator()
    verdict = v.check_terminal(session, step, step_idx=0)
    assert verdict.ok


def test_r0_allows_early_err_internal() -> None:
    """ERR_INTERNAL at step 0 is valid — crash recovery."""
    session = Session()
    step = _mk_terminal("OUTCOME_ERR_INTERNAL", [])
    v = StepValidator()
    # R2 will reject ERR_INTERNAL for a different reason, so just check R0
    verdict = v.check_terminal(session, step, step_idx=0)
    assert not any("R0_MIN_EXPLORE" in r for r in verdict.reasons)


def test_r0_allows_ok_after_min_steps() -> None:
    """OUTCOME_OK at step 3+ is allowed (past the floor)."""
    session = Session()
    step = _mk_terminal("OUTCOME_OK", [])
    v = StepValidator()
    verdict = v.check_terminal(session, step, step_idx=3)
    assert verdict.ok


def test_r0_rejects_clarification_at_step_0() -> None:
    """OUTCOME_NONE_CLARIFICATION at step 0 should also be blocked."""
    session = Session()
    step = _mk_terminal("OUTCOME_NONE_CLARIFICATION", [])
    v = StepValidator()
    verdict = v.check_terminal(session, step, step_idx=0)
    assert not verdict.ok
    assert any("R0_MIN_EXPLORE" in r for r in verdict.reasons)
