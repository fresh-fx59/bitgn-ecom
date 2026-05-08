"""Tests for the v0.1.25 per-model behavioral hooks.

Three hooks were added to ``ModelAdapter`` (base.py) with identity/empty
defaults so non-gpt-oss adapters remain byte-identical:

  * ``format_retry_critique`` — rewrites validator-rejection feedback
  * ``post_process_terminal`` — last-chance mutation of a terminal before
     the validator runs
  * ``extra_reactive_skills`` — per-model skill names to load on top of
     whatever the global ``Router`` returned

Only ``GptOssAdapter`` and ``GptOssRemoteAdapter`` override them; qwen,
glm, lfm2 adapters should return the generic defaults. These tests pin
that contract + the imperative/filter/regex logic inside the gpt-oss
helpers (see 2026-04-23 v0.1.24 PROD evidence).
"""
from __future__ import annotations

from bitgn_contest_agent.backend.adapters.glm_flash import GlmFlashAdapter
from bitgn_contest_agent.backend.adapters.gpt_oss import GptOssAdapter
from bitgn_contest_agent.backend.adapters.gpt_oss_remote import (
    GptOssRemoteAdapter,
)
from bitgn_contest_agent.backend.adapters.lfm2 import Lfm2Adapter
from bitgn_contest_agent.backend.adapters.qwen_a3b import QwenA3bAdapter
from bitgn_contest_agent.backend.adapters.qwen_a3b_remote import (
    QwenA3bRemoteAdapter,
)
from bitgn_contest_agent.schemas import ReportTaskCompletion
from bitgn_contest_agent.session import Session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mk_terminal(refs: list[str]) -> ReportTaskCompletion:
    """Construct a minimally-valid ReportTaskCompletion for hook tests."""
    return ReportTaskCompletion(
        tool="report_completion",
        message="done",
        grounding_refs=refs,
        rulebook_notes="—",
        outcome_justification="—",
        completed_steps_laconic=["x"],
        outcome="OUTCOME_OK",
    )


# ---------------------------------------------------------------------------
# Default hook behavior — adapters that opt out of the imperative critique
# (glm, lfm2, qwen3.5 local) must still see byte-identical defaults.
#
# QwenA3bRemoteAdapter (qwen3.6/neuraldeep) intentionally opts IN to the
# imperative critique helper as of 2026-05-01; it is therefore tested
# alongside the gpt-oss adapters below, not in this default group.
# ---------------------------------------------------------------------------


def _default_hook_adapters():
    return [
        GlmFlashAdapter(),
        Lfm2Adapter(),
        QwenA3bAdapter(),
    ]


def test_default_format_retry_critique_is_generic_for_default_adapters() -> None:
    """Default ``format_retry_critique`` MUST delegate to
    ``prompts.critique_injection`` verbatim for adapters that opted out
    of the imperative rewrite. If a future adapter overrides this
    without meaning to, validator retries would start carrying
    imperative wording to models that haven't been validated against it
    — a behavioral regression that is hard to spot in bench numbers.
    """
    session = Session()
    reasons = ["R7_INBOX_CLEANUP: missing delete of 00_inbox/000_foo.md"]
    for adapter in _default_hook_adapters():
        out = adapter.format_retry_critique(reasons, session)
        assert "Revise and retry" in out, (
            f"{type(adapter).__name__} unexpectedly rewrote the critique"
        )
        assert "Your NEXT tool_call MUST be" not in out, (
            f"{type(adapter).__name__} leaked imperative wording"
        )


def test_default_post_process_terminal_is_identity_for_default_adapters() -> None:
    """Default ``post_process_terminal`` MUST return the exact ``fn`` it was
    given (same object, no mutation). Otherwise, a terminal that was
    already valid would be mutated for models that never asked for it.
    QwenA3bRemoteAdapter is included here — it only overrides the
    critique hook, not the terminal post-processor."""
    session = Session()
    session.seen_refs.add("real.md")
    fn = _mk_terminal(["real.md", "hallucinated.md"])
    adapters = [*_default_hook_adapters(), QwenA3bRemoteAdapter()]
    for adapter in adapters:
        out = adapter.post_process_terminal(fn, session)
        assert out is fn, (
            f"{type(adapter).__name__} mutated the terminal"
        )
        assert out.grounding_refs == ["real.md", "hallucinated.md"]


def test_default_extra_reactive_skills_is_empty_for_default_adapters() -> None:
    """Default ``extra_reactive_skills`` MUST be the empty frozenset even
    on task text that gpt-oss would match. Otherwise the ``inbox-processing``
    skill body would start being injected for qwen/glm/lfm2 runs, blowing
    the bitgn prompt cache and changing their behavior.
    QwenA3bRemoteAdapter is included here — it only overrides the
    critique hook, not the reactive-skills hook."""
    task_text = "Review the next inbound note and act on it."
    adapters = [*_default_hook_adapters(), QwenA3bRemoteAdapter()]
    for adapter in adapters:
        out = adapter.extra_reactive_skills(task_text)
        assert out == frozenset(), (
            f"{type(adapter).__name__} unexpectedly returned {out!r}"
        )


# ---------------------------------------------------------------------------
# GptOssAdapter.format_retry_critique — imperative rewrite
# ---------------------------------------------------------------------------


def _gpt_oss_adapters():
    return [GptOssAdapter(), GptOssRemoteAdapter()]


def test_gpt_oss_critique_rewrites_r7_as_imperative_delete_prescription() -> None:
    """The primary 2026-04-23 v0.1.24 failure: 15 R7 inbox tasks retried
    into another report_completion instead of issuing the delete. The
    imperative rewrite must name the next tool_call (``function.tool =
    "delete"``) so gpt-oss treats it as a tool choice rather than a
    narrative correction."""
    session = Session()
    reasons = ["R7_INBOX_CLEANUP: consumed inbox file was not deleted"]
    for adapter in _gpt_oss_adapters():
        out = adapter.format_retry_critique(reasons, session)
        assert "Your NEXT tool_call MUST be exactly" in out
        assert 'function.tool = "delete"' in out
        assert "00_inbox/" in out  # the example path guidance


def test_gpt_oss_critique_rewrites_r0_min_explore() -> None:
    """R0 fires when the model tries to terminate with too few real
    exploration steps. The imperative rewrite must forbid
    report_completion on the next turn."""
    session = Session()
    reasons = ["R0_MIN_EXPLORE: terminated after 1 step"]
    for adapter in _gpt_oss_adapters():
        out = adapter.format_retry_critique(reasons, session)
        assert "NEXT tool_call MUST NOT be report_completion" in out


def test_gpt_oss_critique_rewrites_r1_grounding_ref() -> None:
    """R1 fires when grounding_refs cite unread paths. The rewrite must
    either force a ``read`` of the cited path OR restrict refs to already-
    read paths. Match on ``grounding_ref`` substring so R1 reason phrasing
    variation is tolerated."""
    session = Session()
    reasons = ["grounding_ref 'fake/path.md' never successfully read"]
    for adapter in _gpt_oss_adapters():
        out = adapter.format_retry_critique(reasons, session)
        assert "read" in out.lower()
        assert "grounding_refs" in out


def test_gpt_oss_critique_rewrites_r5_outbox_attachment() -> None:
    """R5 fires when an outbox write cites an unread attachment. The rewrite
    must force a ``read`` on the unread path."""
    session = Session()
    reasons = ["R5: outbox attachment 'x.md' was never read"]
    for adapter in _gpt_oss_adapters():
        out = adapter.format_retry_critique(reasons, session)
        assert "read" in out.lower()
        assert "attachment" in out.lower()


def test_gpt_oss_critique_falls_back_for_unknown_rule_code() -> None:
    """Non-matching rule codes (ValidationError, mutation integrity,
    leaning mismatch) must still see the generic critique — otherwise a
    retry on a new validator rule would produce an empty imperative."""
    session = Session()
    reasons = ["ValidationError: message cannot be empty"]
    for adapter in _gpt_oss_adapters():
        out = adapter.format_retry_critique(reasons, session)
        assert "Revise and retry" in out
        assert "NEXT tool_call MUST" not in out


def test_gpt_oss_critique_preserves_other_reasons_when_imperative_matches() -> None:
    """If R7 is the first match but another reason is also present, the
    imperative must come first (gpt-oss weights position) while the other
    reasons still appear in a trailer — otherwise we lose validator signal."""
    session = Session()
    reasons = [
        "R7_INBOX_CLEANUP: consumed inbox file was not deleted",
        "R2: some other rule fired too",
    ]
    for adapter in _gpt_oss_adapters():
        out = adapter.format_retry_critique(reasons, session)
        assert out.startswith("Your previous report_completion was rejected")
        assert "Additional validator reasons" in out
        assert "R2: some other rule fired too" in out


def test_gpt_oss_critique_empty_reasons_falls_back_to_default() -> None:
    """Zero reasons must still produce a non-empty critique (prevents an
    empty user message that would confuse the model)."""
    session = Session()
    for adapter in _gpt_oss_adapters():
        out = adapter.format_retry_critique([], session)
        assert out  # non-empty
        assert "Revise and retry" in out


# ---------------------------------------------------------------------------
# GptOssAdapter.post_process_terminal — hallucinated ref filtering
# ---------------------------------------------------------------------------


def test_gpt_oss_post_process_filters_hallucinated_refs() -> None:
    """Drops refs not present in seen_refs ∪ verified_absent. Keeps
    real-read and verified-absent refs. Model_copy is used so the
    original ``fn`` is untouched."""
    session = Session()
    session.seen_refs.update({"real.md", "another.md"})
    session.verified_absent.add("known_missing.md")
    fn = _mk_terminal(
        ["real.md", "hallucinated.md", "another.md", "known_missing.md"]
    )
    for adapter in _gpt_oss_adapters():
        out = adapter.post_process_terminal(fn, session)
        assert out is not fn  # new instance
        assert set(out.grounding_refs) == {
            "real.md", "another.md", "known_missing.md",
        }
        # Original unchanged
        assert "hallucinated.md" in fn.grounding_refs


def test_gpt_oss_post_process_case_insensitive_match() -> None:
    """seen_refs may store paths in any case; matching must be case-
    insensitive so a ref that differs only in case isn't spuriously
    dropped and rejected by R1."""
    session = Session()
    session.seen_refs.add("Real.MD")
    fn = _mk_terminal(["real.md"])
    for adapter in _gpt_oss_adapters():
        out = adapter.post_process_terminal(fn, session)
        # case-insensitive match kept the ref
        assert out.grounding_refs == ["real.md"]


def test_gpt_oss_post_process_returns_identity_on_no_change() -> None:
    """When every ref is already real, return the exact ``fn`` — no
    wasted model_copy allocation, identity check protects callers that
    peek at object identity (e.g. agent loop's ``fn is not step_obj.function``
    branch)."""
    session = Session()
    session.seen_refs.update({"a.md", "b.md"})
    fn = _mk_terminal(["a.md", "b.md"])
    for adapter in _gpt_oss_adapters():
        out = adapter.post_process_terminal(fn, session)
        assert out is fn


def test_gpt_oss_post_process_empty_refs_returns_identity() -> None:
    """Empty grounding_refs list passes through untouched — R1 doesn't
    reject on empty, so the filter has nothing to do."""
    session = Session()
    fn = _mk_terminal([])
    for adapter in _gpt_oss_adapters():
        out = adapter.post_process_terminal(fn, session)
        assert out is fn


def test_gpt_oss_post_process_all_hallucinated_returns_empty_list() -> None:
    """When every ref is fake, the filter returns a terminal with an
    empty list — not None. The terminal survives and gets judged on
    ``message`` alone."""
    session = Session()
    fn = _mk_terminal(["fake1.md", "fake2.md"])
    for adapter in _gpt_oss_adapters():
        out = adapter.post_process_terminal(fn, session)
        assert out.grounding_refs == []


# ---------------------------------------------------------------------------
# GptOssAdapter.extra_reactive_skills — inbox-processing regex
# ---------------------------------------------------------------------------


def test_gpt_oss_extra_reactive_skills_catches_failing_prod_phrasings() -> None:
    """The 4 tasks (t014/t021/t046/t072) the 2026-04-23 v0.1.24 run
    missed. The global tier1 regex routed them to ``inbox-security``
    only; the adapter-extra hook must catch them so ``inbox-processing``
    gets loaded and R7 can fire."""
    # Phrasings drawn from the failing task corpus.
    phrasings = [
        "Review the next inbound note and act on it.",
        "Handle the invoice-request in the inbox.",
        "Process the next bundle-request waiting in 00_inbox.",
        "Please handle the next inbox item.",
        "Process the next queued item in the inbox.",
        "Act on the next one that landed today.",
    ]
    for phrasing in phrasings:
        for adapter in _gpt_oss_adapters():
            out = adapter.extra_reactive_skills(phrasing)
            assert out == frozenset({"inbox-processing"}), (
                f"{type(adapter).__name__} missed phrasing: {phrasing!r}"
            )


def test_gpt_oss_extra_reactive_skills_misses_unrelated_tasks() -> None:
    """Must NOT fire on unrelated task text — otherwise every task
    would get inbox-processing loaded, polluting context and the
    prompt cache."""
    unrelated = [
        "Find the contract signed by Acme Corp in 2024.",
        "Summarize the Q3 finance report.",
        "Update the rulebook to reflect the new policy.",
        "",  # edge: empty task text
    ]
    for text in unrelated:
        for adapter in _gpt_oss_adapters():
            out = adapter.extra_reactive_skills(text)
            assert out == frozenset(), (
                f"{type(adapter).__name__} false-positive on {text!r}"
            )


def test_gpt_oss_extra_reactive_skills_is_case_insensitive() -> None:
    """Tasks that arrive in mixed case (e.g. pasted from an email)
    must still trigger so we don't miss valid inbox work."""
    for text in [
        "REVIEW THE NEXT INBOUND NOTE AND ACT ON IT",
        "Review The Next Inbound Note",
    ]:
        for adapter in _gpt_oss_adapters():
            out = adapter.extra_reactive_skills(text)
            assert out == frozenset({"inbox-processing"}), (
                f"{type(adapter).__name__} missed case-variation: {text!r}"
            )


# ---------------------------------------------------------------------------
# QwenA3bRemoteAdapter (qwen3.6 / neuraldeep) — opts in to the imperative
# critique helper. Local QwenA3bAdapter (qwen3.5 / LM Studio) does NOT.
# ---------------------------------------------------------------------------


def test_qwen36_remote_uses_imperative_critique_for_r7() -> None:
    """qwen3.6 hits the same R7 failure mode as gpt-oss
    (re-emits report_completion under descriptive feedback). The remote
    adapter must rewrite the critique imperatively — same body as
    gpt-oss for consistency. 2026-05-01 qwen3.6/neuraldeep PROD run:
    4× R7_INBOX_CLEANUP all terminated via submit_anyway."""
    session = Session()
    reasons = ["R7_INBOX_CLEANUP: consumed inbox file was not deleted"]
    out = QwenA3bRemoteAdapter().format_retry_critique(reasons, session)
    assert "Your NEXT tool_call MUST be exactly" in out
    assert 'function.tool = "delete"' in out


def test_qwen36_remote_uses_imperative_critique_for_r6_mutation() -> None:
    """R6_MUTATION_DISCIPLINE: 2× rejections in qwen3.6 PROD run where
    the model mutated during GATHERING_INFORMATION on a read-only task.
    The imperative must steer it back to a fresh report_completion."""
    session = Session()
    reasons = ["R6_MUTATION_DISCIPLINE: mutation_guard fired 2× during GATHERING_INFORMATION"]
    out = QwenA3bRemoteAdapter().format_retry_critique(reasons, session)
    assert "NEXT tool_call MUST" in out
    assert "GATHERING_INFORMATION" in out


def test_qwen35_local_keeps_default_critique() -> None:
    """qwen3.5 local (LM Studio) is a separate adapter and has different
    behavioral evidence; it must continue to use the generic
    ``critique_injection`` until it is independently validated against
    the imperative wording. This test pins the boundary."""
    session = Session()
    reasons = ["R7_INBOX_CLEANUP: deletion missing"]
    out = QwenA3bAdapter().format_retry_critique(reasons, session)
    assert "Revise and retry" in out
    assert "Your NEXT tool_call MUST be" not in out


# ---------------------------------------------------------------------------
# Parity — 20b and 120b adapters share the same gpt-oss helpers
# ---------------------------------------------------------------------------


def test_gpt_oss_20b_and_120b_share_identical_hook_output() -> None:
    """Both adapters delegate to the same helpers in ``_helpers.py``.
    This pins that contract — if someone introduces a 120b-only
    divergence in future, they MUST update this test and explain why."""
    session = Session()
    session.seen_refs.add("real.md")
    local = GptOssAdapter()
    remote = GptOssRemoteAdapter()

    # Critique
    reasons = ["R7_INBOX_CLEANUP: deletion missing"]
    assert local.format_retry_critique(reasons, session) == \
        remote.format_retry_critique(reasons, session)

    # Terminal post-processing
    fn = _mk_terminal(["real.md", "fake.md"])
    local_out = local.post_process_terminal(fn, session)
    remote_out = remote.post_process_terminal(fn, session)
    assert local_out.grounding_refs == remote_out.grounding_refs

    # Extra reactive skills
    assert local.extra_reactive_skills("the next inbound note") == \
        remote.extra_reactive_skills("the next inbound note")
