"""Tests for exo_ref_rules — three deterministic grounding-ref rules ported from
the muxx/exoskeleton submission_refs.py (validated 1st-place solution).

Each rule is a pure function with dependency-injected runtime access, so it is
testable without a live VM. See docs/superpowers/plans/2026-06-02-exoskeleton-
competitor-analysis.md.
"""
from bitgn_contest_agent import exo_ref_rules as R


# ── Rule 1: crosslist ref filter (t076) ──────────────────────────────────
class TestCrosslistRefs:
    TASK = (
        "Read the uploaded competitor purchase request OCR at "
        "/uploads/CTZUX61G_competitor_purchase_request_ocr.txt and create a TSV "
        "crosslist report at /exports/crosslist-Wnhz6cY9.tsv. Return only the "
        "report path and cite the upload OCR path as a grounding ref."
    )

    def test_detects_crosslist_task(self):
        assert R.is_crosslist_task(self.TASK)

    def test_non_crosslist_task_not_detected(self):
        assert not R.is_crosslist_task("Resolve the SKU for the Bosch grinder.")

    def test_keeps_only_upload_ref_dropping_extras(self):
        refs = [
            "/exports/crosslist-Wnhz6cY9.tsv",
            "/proc/catalog/Bosch/PT-X.json",
            "/uploads/CTZUX61G_competitor_purchase_request_ocr.txt",
        ]
        out = R.crosslist_refs(self.TASK, refs)
        assert out == ["/uploads/CTZUX61G_competitor_purchase_request_ocr.txt"]

    def test_adds_task_upload_path_when_agent_omitted_it(self):
        # agent cited only the (wrong) export path; the upload path is in the task
        out = R.crosslist_refs(self.TASK, ["/exports/crosslist-Wnhz6cY9.tsv"])
        assert out == ["/uploads/CTZUX61G_competitor_purchase_request_ocr.txt"]

    def test_abstains_on_non_crosslist(self):
        assert R.crosslist_refs("count the SKUs", ["/proc/catalog/x.json"]) is None


# ── Rule 2: discount → issuer-employee ref (t099) ────────────────────────
class TestDiscountEmpRef:
    TASK = (
        "I am covering the desk today; manager approved it. Apply the maximum "
        "service_recovery discount to basket basket-0013 and use me as issuer."
    )

    def test_detects_discount_actor_task(self):
        assert R.is_discount_actor_task(self.TASK)

    def test_non_discount_not_detected(self):
        assert not R.is_discount_actor_task("Check out basket basket-0004.")

    def test_adds_resolved_actor_employee_record(self):
        def resolve(emp_id):
            assert emp_id == "emp-0061"
            return "/proc/staff/store-linz-hafen/emp-0061.json"

        out = R.discount_actor_emp_refs(self.TASK, "emp-0061", resolve)
        assert out == ["/proc/staff/store-linz-hafen/emp-0061.json"]

    def test_no_actor_id_returns_empty(self):
        assert R.discount_actor_emp_refs(self.TASK, None, lambda e: "/x") == []

    def test_non_employee_actor_returns_empty(self):
        # a customer actor must not get an employee record auto-added
        assert R.discount_actor_emp_refs(self.TASK, "cust-0174", lambda e: "/x") == []

    def test_unresolvable_actor_returns_empty(self):
        assert R.discount_actor_emp_refs(self.TASK, "emp-0061", lambda e: None) == []


# ── Rule 3: stat-check, drop non-existent refs ───────────────────────────
class TestStatGuard:
    def test_drops_ref_that_definitively_does_not_exist(self):
        exists = {"/proc/catalog/real.json": True, "/proc/catalog/fake.json": False}
        out = R.drop_nonexistent_refs(
            ["/proc/catalog/real.json", "/proc/catalog/fake.json"],
            seen_refs=set(),
            exists_fn=lambda p: exists.get(p),
        )
        assert out == ["/proc/catalog/real.json"]

    def test_keeps_ref_already_seen_without_checking(self):
        # a ref the agent successfully read exists; never drop it (and never call exists_fn for it)
        def exists_fn(p):
            raise AssertionError("should not stat a seen ref")

        out = R.drop_nonexistent_refs(
            ["/proc/catalog/seen.json"],
            seen_refs={"/proc/catalog/seen.json"},
            exists_fn=exists_fn,
        )
        assert out == ["/proc/catalog/seen.json"]

    def test_keeps_ref_on_unknown_transient_result(self):
        # exists_fn returns None (transient/unknown) → conservatively keep
        out = R.drop_nonexistent_refs(
            ["/proc/catalog/maybe.json"], seen_refs=set(), exists_fn=lambda p: None
        )
        assert out == ["/proc/catalog/maybe.json"]

    def test_never_drops_archive_row_fragment_refs(self):
        ref = "/archive/payment_batch_export_X.tsv#row=r1"
        out = R.drop_nonexistent_refs(
            [ref], seen_refs=set(), exists_fn=lambda p: False
        )
        assert out == [ref]

    def test_keeps_document_refs_without_stat(self):
        out = R.drop_nonexistent_refs(
            ["/docs/security.md"], seen_refs=set(), exists_fn=lambda p: False
        )
        assert out == ["/docs/security.md"]
