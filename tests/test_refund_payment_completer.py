"""Unit tests for refund_payment_completer."""
from __future__ import annotations

import json

from bitgn_contest_agent.refund_payment_completer import (
    RefundPaymentResult,
    complete_refund_payment_refs,
)


RET_007_BODY = json.dumps({
    "id": "ret_007",
    "basket_id": "basket_213",
    "customer_id": "cust_086",
    "payment_id": "pay_013",
    "status": "requested",
})


def _call(**overrides) -> RefundPaymentResult:
    base = dict(
        task_text="Approve the customer refund tied to return ret_007.",
        kind="none",
        refs=[
            "/AGENTS.MD",
            "/docs/security.md",
            "/docs/returns.md",
            "/proc/returns/ret_007.json",
        ],
        read_cache={"/proc/returns/ret_007.json": RET_007_BODY},
        read=None,
    )
    base.update(overrides)
    return complete_refund_payment_refs(**base)


class TestRefundDetection:
    def test_refund_keyword(self):
        r = _call()
        assert r.added == ["/proc/payments/pay_013.json"]

    def test_return_keyword_also_matches(self):
        r = _call(task_text="Process the return for cust_086.")
        assert r.added == ["/proc/payments/pay_013.json"]

    def test_unrelated_task_aborts(self):
        r = _call(task_text="How many baskets does cust_086 have?")
        assert r.aborted
        assert r.abort_reason == "not_refund_family"
        assert r.added == []

    def test_kind_signal_overrides_task_text(self):
        r = _call(kind="refund_approve", task_text="approve this")
        assert r.added == ["/proc/payments/pay_013.json"]


class TestExtractAndAdd:
    def test_no_return_ref_aborts(self):
        r = _call(refs=["/AGENTS.MD", "/docs/returns.md"])
        assert r.aborted
        assert r.abort_reason == "no_return_ref"

    def test_skips_when_no_cache_and_no_read(self):
        r = _call(
            read_cache={},
            read=None,
        )
        assert not r.aborted
        assert r.added == []
        assert any(reason == "no_payment_id"
                   for _, reason in r.skipped)

    def test_uses_read_callback_when_cache_misses(self):
        def fake_read(path: str) -> str:
            assert path == "/proc/returns/ret_007.json"
            return RET_007_BODY
        r = _call(read_cache={}, read=fake_read)
        assert r.added == ["/proc/payments/pay_013.json"]

    def test_skips_already_cited_payment(self):
        r = _call(refs=[
            "/AGENTS.MD",
            "/proc/returns/ret_007.json",
            "/proc/payments/pay_013.json",  # already there
        ])
        assert r.added == []
        assert any(reason == "already_cited"
                   for _, reason in r.skipped)

    def test_invalid_json_skipped(self):
        r = _call(read_cache={"/proc/returns/ret_007.json": "not-json"})
        assert r.added == []
        assert any(reason == "no_payment_id"
                   for _, reason in r.skipped)

    def test_missing_payment_id_field_skipped(self):
        body = json.dumps({"id": "ret_007", "status": "requested"})
        r = _call(read_cache={"/proc/returns/ret_007.json": body})
        assert r.added == []

    def test_malformed_payment_id_skipped(self):
        body = json.dumps({"id": "ret_007", "payment_id": "not-a-pay-id"})
        r = _call(read_cache={"/proc/returns/ret_007.json": body})
        assert r.added == []


class TestMultiReturn:
    def test_chains_each_distinct_return_to_its_payment(self):
        body2 = json.dumps({"id": "ret_022", "payment_id": "pay_055"})
        r = _call(
            refs=[
                "/AGENTS.MD",
                "/proc/returns/ret_007.json",
                "/proc/returns/ret_022.json",
            ],
            read_cache={
                "/proc/returns/ret_007.json": RET_007_BODY,
                "/proc/returns/ret_022.json": body2,
            },
        )
        assert set(r.added) == {
            "/proc/payments/pay_013.json",
            "/proc/payments/pay_055.json",
        }

    def test_idempotent_dedup_within_added(self):
        # Two returns pointing at same payment → add once
        same_body = json.dumps({"id": "ret_x", "payment_id": "pay_013"})
        r = _call(
            refs=[
                "/proc/returns/ret_007.json",
                "/proc/returns/ret_022.json",
            ],
            read_cache={
                "/proc/returns/ret_007.json": RET_007_BODY,
                "/proc/returns/ret_022.json": same_body,
            },
        )
        assert r.added == ["/proc/payments/pay_013.json"]


class TestSafetyGuards:
    def test_never_drops_existing_refs(self):
        r = _call()
        for path in ("/AGENTS.MD", "/docs/security.md",
                     "/docs/returns.md", "/proc/returns/ret_007.json"):
            assert path in r.refs

    def test_only_appends_pay_NNN_format(self):
        # Body has payment_id but with wrong shape
        body = json.dumps({"id": "ret_007", "payment_id": "PAY13"})
        r = _call(read_cache={"/proc/returns/ret_007.json": body})
        assert r.added == []
