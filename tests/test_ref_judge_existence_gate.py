"""ref_judge: the empty-refs ("cite nothing") correction must fire ONLY for
EXISTENCE questions ("does X exist?"), never for AVAILABILITY ("do you have N
of X?") or OCR ("can I buy this basket?") negatives.

Evidence (PROD v0.1.164 run-2): ref_judge stripped t002 ("do you have 7 of X
but not Y") and t063 (OCR "can I buy this basket today") to [] on their
negative answers, but the grader WANTS the candidate products cited for those
families → ref_judge caused/worsened the failure. Existence NO (t006/t046)
must still strip the near-miss to empty.
"""
from __future__ import annotations

from bitgn_contest_agent import ref_judge as rj


def _strip_classifier(system, user):
    """Simulate the LLM judge returning an empty set (the strip decision)."""
    return {"reasoning": "no exact match", "catalog_refs": []}


CAND = [{"path": "/proc/catalog/Bosch Professional/PT-BIT-BOS-CYL9-7.json",
         "sku": "PT-BIT-BOS-CYL9-7", "brand": "Bosch Professional",
         "name": "CYL-9 7-piece", "attributes": {"pieces": 7}}]


def test_existence_no_strips_to_empty():
    """t006/t046: 'Does such product exist?' + NO → strip near-miss to []."""
    out = rj.judge_catalog_refs(
        "Customer wants '7pc bosch cyl-9 multi bits and has case type metal cassette'. "
        "Does such product exist?",
        "FALSE(0)", [CAND[0]["path"]], CAND, classify_fn=_strip_classifier)
    assert out == [], f"existence NO should strip to empty, got {out}"


def test_availability_no_does_not_strip():
    """t002: 'do you have 7 of X (but not Y)' + NO → must NOT strip to []
    (the grader wants the candidate products cited). Abstain instead."""
    out = rj.judge_catalog_refs(
        "Do you have 7 of 'bosch cyl-9 small special set' (but not PT-BIT-BOS-CYL9-5LONG) "
        "in stock near Donaustadt?",
        "<NO>", [CAND[0]["path"]], CAND, classify_fn=_strip_classifier)
    assert out is None, f"availability NO must abstain (None), not strip, got {out}"


def test_ocr_buy_basket_no_does_not_strip():
    """t063: OCR 'can I buy this exact basket today' + NO → must NOT strip."""
    out = rj.judge_catalog_refs(
        "Look at the uploaded OCR receipt /uploads/x_receipt_ocr.txt. Can I buy this "
        "exact basket today from the same branch? Answer yes/no.",
        "<NO>", [CAND[0]["path"]], CAND, classify_fn=_strip_classifier)
    assert out is None, f"OCR buy-basket NO must abstain (None), not strip, got {out}"
