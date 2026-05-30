"""Plumbing tests for the LLM-as-judge ref corrector (no real LLM — the
judge call is injected). Validates reference-anchoring (judge can't introduce
unknown paths), abstain-on-bad-output, and the catalogue-ref swap."""
from __future__ import annotations

from bitgn_contest_agent.ref_judge import apply_correction, applies, judge_catalog_refs


def test_applies_to_yes_no_not_count_list():
    # yes/no existence/availability → judge applies
    assert applies("Does the Bosch GSR55 with voltage 18 exist?", ["/proc/catalog/B/PT-X.json"])
    assert applies("Do you have 5 of the Makita drill?", ["/proc/catalog/B/PT-X.json"])
    # count-list is owned by count_ref_completer → judge must NOT fire (no
    # interaction that could prune the completer's additions)
    assert not applies("how many of these SKUs are available: PT-A, PT-B?", ["/proc/catalog/B/PT-A.json"])
    # nothing to reason about → no fire
    assert not applies("Check out basket basket-1.", [])

_CANDS = [
    {"path": "/proc/catalog/DeWalt/PT-A.json", "sku": "PT-A", "brand": "DeWalt", "name": "A"},
    {"path": "/proc/catalog/Makita/PT-B.json", "sku": "PT-B", "brand": "Makita", "name": "B"},
    {"path": "/proc/catalog/Bosch/PT-C.json", "sku": "PT-C", "brand": "Bosch", "name": "C"},
]


def _judge_returning(refs, reasoning="r"):
    def _fn(*, system, user):
        return {"reasoning": reasoning, "catalog_refs": refs}
    return _fn


def test_count_list_judge_returns_all_candidates():
    out = judge_catalog_refs(
        "how many of these SKUs ...: PT-A, PT-B, PT-C?", "2",
        current_catalog_refs=["/proc/catalog/DeWalt/PT-A.json"],
        candidate_records=_CANDS,
        classify_fn=_judge_returning([c["path"] for c in _CANDS]),
    )
    assert set(out) == {c["path"] for c in _CANDS}


def test_yes_no_judge_keeps_only_match():
    out = judge_catalog_refs(
        "does the Bosch PT-C exist with spec X?", "TRUE(1)",
        current_catalog_refs=["/proc/catalog/DeWalt/PT-A.json", "/proc/catalog/Makita/PT-B.json"],
        candidate_records=_CANDS,
        classify_fn=_judge_returning(["/proc/catalog/Bosch/PT-C.json"]),
    )
    assert out == ["/proc/catalog/Bosch/PT-C.json"]


def test_reference_anchored_drops_hallucinated_path():
    out = judge_catalog_refs(
        "how many ...: PT-A?", "1",
        current_catalog_refs=[],
        candidate_records=_CANDS,
        classify_fn=_judge_returning(["/proc/catalog/DeWalt/PT-A.json", "/proc/catalog/FAKE/PT-Z.json"]),
    )
    assert out == ["/proc/catalog/DeWalt/PT-A.json"]  # FAKE not in candidate set → dropped


def test_no_answer_allows_empty_correction_strips_near_miss():
    # does-not-exist answer + judge returns [] → strip the near-miss extra ref
    out = judge_catalog_refs(
        "Customer wants 'air compressor capacity helper'. Does such product exist?",
        "FALSE(2)", ["/proc/catalog/X/PT-NEAR-MISS.json"], _CANDS,
        classify_fn=_judge_returning([]))
    assert out == []


def test_empty_correction_abstains_on_positive_answer():
    # safety: a YES answer with an empty judge result must NOT strip refs
    out = judge_catalog_refs(
        "Does the Bosch PT-C exist?", "TRUE(1)",
        ["/proc/catalog/Bosch/PT-C.json"], _CANDS,
        classify_fn=_judge_returning([]))
    assert out is None


def test_abstain_on_no_candidates():
    assert judge_catalog_refs("t", "a", [], [], classify_fn=_judge_returning(["x"])) is None


def test_abstain_on_bad_output():
    assert judge_catalog_refs("t", "a", [], _CANDS, classify_fn=lambda **k: "not a dict") is None
    assert judge_catalog_refs("t", "a", [], _CANDS, classify_fn=lambda **k: {"x": 1}) is None
    assert judge_catalog_refs("t", "a", [], _CANDS, classify_fn=lambda **k: (_ for _ in ()).throw(RuntimeError())) is None


def test_apply_correction_swaps_catalog_keeps_others():
    all_refs = [
        "/docs/availability-checks.md",
        "/proc/locations/Graz/store-graz-x.json",
        "/proc/catalog/DeWalt/PT-A.json",  # to be replaced
    ]
    corrected = ["/proc/catalog/Makita/PT-B.json", "/proc/catalog/Bosch/PT-C.json"]
    out = apply_correction(all_refs, corrected)
    assert out == [
        "/docs/availability-checks.md",
        "/proc/locations/Graz/store-graz-x.json",
        "/proc/catalog/Makita/PT-B.json",
        "/proc/catalog/Bosch/PT-C.json",
    ]
