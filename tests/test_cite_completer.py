"""Tests for cite_completer."""
from __future__ import annotations

from bitgn_contest_agent.cite_completer import (
    complete,
    complete_refs,
    detect_action_family,
)


def test_detect_checkout_from_check_it_out():
    assert (
        detect_action_family("I am ready to buy everything in basket_132; please check it out.")
        == "checkout"
    )


def test_detect_checkout_from_submit_checkout():
    assert detect_action_family("Submit checkout for basket basket_159.") == "checkout"


def test_detect_discount_from_service_recovery():
    assert (
        detect_action_family(
            "I am covering the desk. Apply the maximum service_recovery discount (10%) to basket basket_021."
        )
        == "discount"
    )


def test_detect_discount_supersedes_checkout():
    """Discount triple includes checkout.md; pick discount when both fingerprints match."""
    task = "Apply 10% discount to basket_021 and check it out."
    assert detect_action_family(task) == "discount"


def test_detect_3ds():
    assert (
        detect_action_family("3DS failed during checkout for my basket basket_216. Recover the 3DS flow.")
        == "ds3_recover"
    )


def test_detect_none_on_non_action_task():
    assert detect_action_family("How many products are Cordless Saw and Sander?") is None
    assert detect_action_family("Do you have <product> in stock?") is None


def test_complete_adds_missing_security_md_when_read():
    refs = ["/AGENTS.MD", "/docs/checkout.md", "/proc/baskets/basket_126.json"]
    seen = {
        "/AGENTS.MD",
        "/docs/checkout.md",
        "/docs/security.md",
        "/proc/baskets/basket_126.json",
    }
    res = complete_refs(refs=refs, family="checkout", seen_refs=seen)
    assert "/docs/security.md" in res.refs
    assert "/docs/security.md" in res.added


def test_complete_skips_unread_doc():
    """If the agent never read /docs/security.md, don't inject it."""
    refs = ["/AGENTS.MD", "/docs/checkout.md", "/proc/baskets/basket_126.json"]
    seen = {"/AGENTS.MD", "/docs/checkout.md", "/proc/baskets/basket_126.json"}
    res = complete_refs(refs=refs, family="checkout", seen_refs=seen)
    assert "/docs/security.md" not in res.refs
    assert "/docs/security.md" not in res.added
    assert any("not in seen_refs" in r for r in res.reasons)


def test_complete_no_op_when_all_triple_present():
    refs = [
        "/AGENTS.MD",
        "/docs/security.md",
        "/docs/checkout.md",
        "/proc/baskets/basket_126.json",
    ]
    seen = set(refs)
    res = complete_refs(refs=refs, family="checkout", seen_refs=seen)
    assert res.added == []
    assert set(res.refs) == set(refs)


def test_complete_discount_triple():
    refs = ["/AGENTS.MD", "/docs/discounts.md", "/proc/baskets/basket_021.json"]
    seen = {
        "/AGENTS.MD",
        "/docs/discounts.md",
        "/docs/security.md",
        "/docs/checkout.md",
        "/proc/baskets/basket_021.json",
    }
    res = complete_refs(refs=refs, family="discount", seen_refs=seen)
    assert "/docs/security.md" in res.refs
    assert "/docs/checkout.md" in res.refs


def test_complete_3ds_triple():
    refs = ["/AGENTS.MD", "/docs/payments/3ds.md", "/proc/payments/pay_009.json"]
    seen = {
        "/AGENTS.MD",
        "/docs/payments/3ds.md",
        "/docs/security.md",
        "/docs/checkout.md",
        "/proc/payments/pay_009.json",
    }
    res = complete_refs(refs=refs, family="ds3_recover", seen_refs=seen)
    assert "/docs/security.md" in res.refs
    assert "/docs/checkout.md" in res.refs


def test_complete_top_level_no_family_passthrough():
    refs = ["/AGENTS.MD", "/proc/catalog/X.json"]
    res = complete(
        task_text="How many products are wood screws?",
        message="<COUNT:42>",
        refs=refs,
        seen_refs=set(refs),
    )
    assert res.family is None
    assert res.refs == refs
    assert res.added == []


def test_v167_t21_repro():
    """v0.1.67 t21 PROD failure: checkout refusal cited
    AGENTS.MD + checkout.md + basket but missing security.md.
    Completer should add security.md if it was read."""
    refs = ["/AGENTS.MD", "/docs/checkout.md", "/proc/baskets/basket_126.json"]
    seen = {
        "/AGENTS.MD",
        "/docs/checkout.md",
        "/docs/security.md",
        "/proc/baskets/basket_126.json",
    }
    res = complete(
        task_text="I am ready to buy everything in basket basket_126; please check it out.",
        message="I can't check out basket_126; one line is short on inventory.",
        refs=refs,
        seen_refs=seen,
    )
    assert res.family == "checkout"
    assert "/docs/security.md" in res.refs
