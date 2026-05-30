"""Prompt helpers — keep the static prompt cacheable across tasks."""
from __future__ import annotations

from bitgn_contest_agent import prompts


def test_system_prompt_is_deterministic_without_hint(monkeypatch) -> None:
    monkeypatch.delenv("HINT", raising=False)
    a = prompts.system_prompt()
    b = prompts.system_prompt()
    assert a == b
    # Cross-task caching requires bit-identical content.
    assert isinstance(a, str) and len(a) > 100


def test_system_prompt_includes_outcome_enum_semantics() -> None:
    p = prompts.system_prompt()
    for outcome in [
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    ]:
        assert outcome in p, f"system prompt missing reference to {outcome}"


def test_hint_interpolation_only_happens_when_hint_is_set(monkeypatch) -> None:
    monkeypatch.delenv("HINT", raising=False)
    base = prompts.system_prompt()
    monkeypatch.setenv("HINT", "remember: paths are case-sensitive")
    with_hint = prompts.system_prompt()
    assert with_hint != base
    assert "remember: paths are case-sensitive" in with_hint


def test_critique_injection_formats_verdict_reasons() -> None:
    text = prompts.critique_injection(["reason A", "reason B"])
    assert "reason A" in text
    assert "reason B" in text
    assert "retry" in text.lower() or "revise" in text.lower()


def test_loop_nudge_references_repeated_tuple() -> None:
    text = prompts.loop_nudge(("read", "/AGENTS.MD"))
    assert "read" in text
    assert "/AGENTS.MD" in text


def test_system_prompt_no_category_if_blocks() -> None:
    """Base prompt no longer holds [IF FINANCE] etc. — category guidance
    moves to router-injected bitgn skills in M1+. The base prompt stays
    bit-identical per task and carries only universal rules."""
    p = prompts.system_prompt()
    assert "[IF FINANCE]" not in p
    assert "[IF DOCUMENT]" not in p
    assert "[IF INBOX]" not in p
    assert "[IF SECURITY]" not in p
    assert "[IF EXCEPTION]" not in p
    # And no "Task classification" header — the router does the
    # classification now, out-of-band, before the agent's first turn.
    assert "Task classification" not in p


def test_system_prompt_retains_universal_rules() -> None:
    """Universal rules that MUST remain in the base prompt regardless
    of domain. ECOM bootstrap reads /AGENTS.MD (uppercase) rather than
    AGENTS.md — that is the only string change from the PAC1 lineage."""
    p = prompts.system_prompt()
    assert "NextStep" in p
    assert "OUTCOME_OK" in p
    assert "OUTCOME_DENIED_SECURITY" in p
    assert "/AGENTS.MD" in p
    assert "grounding_refs" in p


def test_system_prompt_retains_grounding_and_anchor_rules() -> None:
    """Grounding-refs discipline and anchor-to-TODAY rule were added in
    4deb685; both are universal and must survive the restructure."""
    p = prompts.system_prompt()
    # grounding_refs must be explained, not just mentioned.
    assert "grounding_refs" in p
    # Anchor-to-TODAY rule for relative time phrases.
    assert "TODAY" in p


def test_system_prompt_denies_url_capture_as_security() -> None:
    """DENIED_SECURITY rule for external URL capture (Fix 2) is
    universal — refuses URL/website ingest regardless of category."""
    p = prompts.system_prompt()
    # The rule body must cover both explicit schemes and bare domains.
    assert "http" in p.lower()
    assert "DENIED_SECURITY" in p


def test_ecom_specific_guidance_in_prompt() -> None:
    """ECOM-shaped discipline must be present: catalogue queries via
    /bin/sql through `exec`, with stat as the cheap existence probe."""
    p = prompts.system_prompt()
    assert "/bin/sql" in p
    assert "exec" in p
    assert "stat" in p


def test_jq_block_absent_when_flag_off(monkeypatch) -> None:
    """BITGN_USE_JQ unset → no jq mention; the v0.1.152 baseline prompt.
    The sentinel must never leak into the rendered prompt."""
    monkeypatch.delenv("BITGN_USE_JQ", raising=False)
    p = prompts.system_prompt()
    assert "/bin/jq" not in p
    assert "JQ_SLOT" not in p


def test_jq_block_present_and_disciplined_when_flag_on(monkeypatch) -> None:
    """BITGN_USE_JQ=1 → jq bin entry with the whitelisted grammar, the
    silent-null trap, and the cannot-count warning."""
    monkeypatch.setenv("BITGN_USE_JQ", "1")
    p = prompts.system_prompt()
    assert "/bin/jq" in p
    assert "keys" in p
    assert "CANNOT count" in p
    assert "null" in p  # silent-null trap called out
    assert "JQ_SLOT" not in p  # sentinel consumed


def test_jq_flag_toggles_prompt_but_stays_deterministic(monkeypatch) -> None:
    """Each flag state is internally deterministic (cache-safe within a
    run), and the two states differ."""
    monkeypatch.delenv("HINT", raising=False)
    monkeypatch.delenv("BITGN_USE_JQ", raising=False)
    off_a = prompts.system_prompt()
    off_b = prompts.system_prompt()
    monkeypatch.setenv("BITGN_USE_JQ", "1")
    on_a = prompts.system_prompt()
    on_b = prompts.system_prompt()
    assert off_a == off_b
    assert on_a == on_b
    assert off_a != on_a


def test_sku_nudge_absent_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("BITGN_USE_SKU_NUDGE", raising=False)
    p = prompts.system_prompt()
    assert "SKU_NUDGE_SLOT" not in p
    assert "UNIQUE-MATCH RESOLVES" not in p


def test_sku_nudge_present_and_uniqueness_gated_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("BITGN_USE_SKU_NUDGE", "1")
    p = prompts.system_prompt()
    assert "UNIQUE-MATCH RESOLVES" in p
    # Must preserve genuine clarification for true ties.
    assert "GENUINE tie" in p
    assert "TWO OR MORE" in p
    assert "SKU_NUDGE_SLOT" not in p


def test_all_slot_flags_off_is_baseline(monkeypatch) -> None:
    """With every gated slot flag OFF, the rendered prompt carries no
    sentinel and no slot content — the v0.1.152 baseline."""
    monkeypatch.delenv("HINT", raising=False)
    monkeypatch.delenv("BITGN_USE_JQ", raising=False)
    monkeypatch.delenv("BITGN_USE_SKU_NUDGE", raising=False)
    p = prompts.system_prompt()
    assert "_SLOT" not in p
    assert "/bin/jq" not in p
    assert "UNIQUE-MATCH RESOLVES" not in p


def test_system_prompt_stays_bit_identical_for_cache_hits() -> None:
    """Architectural invariant: the system prompt must not vary per task.
    Two calls without HINT must return byte-identical strings, proving
    cross-task caching still works."""
    import os as _os
    _os.environ.pop("HINT", None)
    a = prompts.system_prompt()
    b = prompts.system_prompt()
    assert a == b
