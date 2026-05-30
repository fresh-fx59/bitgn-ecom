from bitgn_contest_agent.count_rederive import build_bounce_reason, RederiveResult


def test_bounce_reason_states_rederived_count_and_convention():
    rr = RederiveResult(4, per_product=[("Fiskars", "1CD-A3X", True), ("Mobil", "1ZE-TCR", False)])
    msg = build_bounce_reason(3, rr)
    assert "yields 4" in msg            # the re-derived count is surfaced
    assert "3" in msg                   # the agent's reported number is surfaced
    assert "COALESCE" in msg            # the convention is spelled out
    assert "DIRECTION" in msg.upper()   # direction-awareness is emphasised
    assert "1CD-A3X" in msg             # per-product verdicts included


def test_bounce_reason_is_advisory_not_a_command():
    # It must ask the model to RECOMPUTE/reconcile (bounce), not assert the answer.
    rr = RederiveResult(2, per_product=[])
    msg = build_bounce_reason(5, rr).lower()
    assert "recompute" in msg or "reconcile" in msg
