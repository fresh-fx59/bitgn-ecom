"""Shape-B (raw-SKU-list compound) count re-derivation — the REAL prod
count_per_store shape (t005/t025/t045/t065). Pure logic: mock read/list
over a path->content dict, drive with the EXACT prod task phrasings, assert
against hand-computed oracles. No LLM, no agent."""
from __future__ import annotations

import json

from bitgn_contest_agent.fs_count_rederive import rederive_count_b


def _mk(stores: dict):
    """stores: {city: {store_id: inventory_list}} -> (read_fn, list_fn)."""
    files: dict[str, str] = {}
    tree: dict[str, list[str]] = {"/proc/locations": []}
    for city, by_id in stores.items():
        cpath = f"/proc/locations/{city}"
        tree["/proc/locations"].append(cpath)
        tree.setdefault(cpath, [])
        for sid, inv in by_id.items():
            p = f"{cpath}/{sid}.json"
            tree[cpath].append(p)
            files[p] = json.dumps({"id": sid, "city": city, "inventory": inv})

    def read_fn(p):
        return files.get(p)

    def list_fn(p):
        return tree.get(p, [])
    return read_fn, list_fn


def test_b1_on_hand_ge_and_available_lt(  # t005/t045/t065 template
):
    inv = [
        {"sku": "PT-A", "on_hand": 5, "reserved": 4},   # avail 1: oh>=2 & avail<2 -> YES
        {"sku": "PT-B", "on_hand": 5, "reserved": 0},   # avail 5: avail<2 fails -> no
        {"sku": "PT-C", "on_hand": 1, "reserved": 0},   # oh>=2 fails -> no
    ]
    read_fn, list_fn = _mk({"Innsbruck": {"store-innsbruck-west": inv}})
    text = ("At ibk west tools place, how many of these SKUs have at least 2 units "
            "physically on hand, but fewer than 2 same-day units available after "
            "reservations: PT-A, PT-B, PT-C, PT-ABSENT? Answer exactly in format \"%d\".")
    rr = rederive_count_b(text, read_fn, list_fn)
    assert rr.count == 1, (rr.reason, rr.per_product)
    verdict = dict((s, q) for s, q in rr.per_product)
    assert verdict["PT-A"] is True and verdict["PT-B"] is False
    assert verdict["PT-ABSENT"] is False  # absent -> 0/0 -> oh>=2 fails


def test_b2_short_of_but_incoming_within_window(  # t025 template
):
    inv = [
        {"sku": "PT-E", "on_hand": 1, "reserved": 0, "incoming": [{"quantity": 1, "arrival_in_days": 2}]},  # avail1<2 & 1+1>=2 -> YES
        {"sku": "PT-F", "on_hand": 0, "reserved": 0, "incoming": [{"quantity": 1, "arrival_in_days": 5}]},  # incoming out of window -> no
    ]
    read_fn, list_fn = _mk({"Linz": {"store-linz-kleinmuenchen": inv}})
    text = ("At kleinmuenchen tools place, how many of these SKUs are short of 2 same-day "
            "units, but would reach 2 units if incoming stock due within 3 days is included: "
            "PT-E, PT-F? Answer exactly in format \"<COUNT:%d>\".")
    rr = rederive_count_b(text, read_fn, list_fn)
    assert rr.count == 1, (rr.reason, rr.per_product)


def test_store_alias_resolution_west_innsbruck(  # t045 "near West Innsbruck"
):
    inv = [{"sku": "PT-A", "on_hand": 3, "reserved": 2}]  # avail1: YES
    read_fn, list_fn = _mk({"Innsbruck": {"store-innsbruck-west": inv,
                                          "store-innsbruck-ost": []}})
    text = ("At PowerTools near West Innsbruck, how many of these SKUs have at least 2 units "
            "physically on hand, but fewer than 2 same-day units available after reservations: "
            "PT-A? Answer exactly in format \"<COUNT:%d>\".")
    rr = rederive_count_b(text, read_fn, list_fn)
    assert rr.count == 1, (rr.reason, rr.per_product)


def test_abstains_when_store_ambiguous(client_free=None):
    inv = [{"sku": "PT-A", "on_hand": 3, "reserved": 2}]
    # two stores both containing 'innsbruck' and no disambiguating token
    read_fn, list_fn = _mk({"Innsbruck": {"store-innsbruck-west": inv,
                                          "store-innsbruck-ost": inv}})
    text = ("At Innsbruck tools place, how many of these SKUs have at least 2 units physically "
            "on hand, but fewer than 2 same-day units available after reservations: PT-A?")
    rr = rederive_count_b(text, read_fn, list_fn)
    assert rr.count is None  # ambiguous store -> ABSTAIN (no bounce)


def test_abstains_on_non_shapeB_and_unknown_predicate():
    read_fn, list_fn = _mk({"Linz": {"store-linz-x": []}})
    # not shape-B
    assert rederive_count_b("How many catalogue products are Work Jacket?", read_fn, list_fn).count is None
    # shape-B signal but unknown predicate template
    txt = "At linz x, how many of these SKUs are blessed by the moon: PT-A?"
    assert rederive_count_b(txt, read_fn, list_fn).count is None
