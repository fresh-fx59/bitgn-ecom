"""Unit tests for store_back_completer."""
from __future__ import annotations

import json

from bitgn_contest_agent.store_back_completer import (
    StoreBackResult,
    complete_store_back_refs,
)


EMP_036_BODY = json.dumps({
    "id": "emp_036",
    "display_name": "Maren Maas",
    "roles": ["employee", "store_manager"],
    "store_id": "store_bratislava_stare_mesto",
})


def _call(**overrides) -> StoreBackResult:
    base = dict(
        task_text="Check each row against our exact catalogue and my store's same-day availability.",
        refs=[
            "/AGENTS.MD",
            "/proc/employees/emp_036.json",
            "/proc/catalog/Bondex/PNT-2RHJYR74.json",
        ],
        read_cache={"/proc/employees/emp_036.json": EMP_036_BODY},
        read=None,
    )
    base.update(overrides)
    return complete_store_back_refs(**base)


class TestTrigger:
    def test_my_store_matches(self):
        r = _call()
        assert r.added == ["/proc/stores/store_bratislava_stare_mesto.json"]

    def test_same_day_availability_matches(self):
        r = _call(task_text="Check same-day availability across these SKUs.")
        assert r.added == ["/proc/stores/store_bratislava_stare_mesto.json"]

    def test_in_stock_matches(self):
        r = _call(task_text="Show me items in stock at our store.")
        assert r.added == ["/proc/stores/store_bratislava_stare_mesto.json"]

    def test_unrelated_task_aborts(self):
        r = _call(task_text="How many customers are over 30?")
        assert r.aborted
        assert r.abort_reason == "not_store_availability_task"


class TestChain:
    def test_no_emp_ref_aborts(self):
        r = _call(refs=["/AGENTS.MD", "/proc/catalog/x/y.json"])
        assert r.aborted
        assert r.abort_reason == "no_employee_ref"

    def test_uses_read_callback(self):
        def fake_read(path: str) -> str:
            assert path == "/proc/employees/emp_036.json"
            return EMP_036_BODY
        r = _call(read_cache={}, read=fake_read)
        assert r.added == ["/proc/stores/store_bratislava_stare_mesto.json"]

    def test_already_cited_no_duplicate(self):
        r = _call(refs=[
            "/AGENTS.MD",
            "/proc/employees/emp_036.json",
            "/proc/stores/store_bratislava_stare_mesto.json",
        ])
        assert r.added == []

    def test_home_store_id_field_alias(self):
        body = json.dumps({"id": "emp_036", "home_store_id": "store_xyz_abc"})
        r = _call(read_cache={"/proc/employees/emp_036.json": body})
        assert r.added == ["/proc/stores/store_xyz_abc.json"]

    def test_no_store_id_field_skipped(self):
        body = json.dumps({"id": "emp_036", "display_name": "Maren"})
        r = _call(read_cache={"/proc/employees/emp_036.json": body})
        assert r.added == []

    def test_malformed_store_id_skipped(self):
        body = json.dumps({"id": "emp_036", "store_id": "Not A Valid Id"})
        r = _call(read_cache={"/proc/employees/emp_036.json": body})
        assert r.added == []

    def test_multiple_emps_unique_stores(self):
        body2 = json.dumps({"id": "emp_099", "store_id": "store_other"})
        r = _call(
            refs=[
                "/proc/employees/emp_036.json",
                "/proc/employees/emp_099.json",
            ],
            read_cache={
                "/proc/employees/emp_036.json": EMP_036_BODY,
                "/proc/employees/emp_099.json": body2,
            },
        )
        assert set(r.added) == {
            "/proc/stores/store_bratislava_stare_mesto.json",
            "/proc/stores/store_other.json",
        }


class TestSafety:
    def test_never_drops_existing(self):
        r = _call()
        for p in ("/AGENTS.MD", "/proc/employees/emp_036.json",
                  "/proc/catalog/Bondex/PNT-2RHJYR74.json"):
            assert p in r.refs


class TestActorIdFallback:
    def test_actor_id_used_when_no_emp_ref(self):
        def fake_read(path):
            assert path == "/proc/employees/emp_036.json"
            return EMP_036_BODY
        r = complete_store_back_refs(
            task_text="Check my store's same-day availability.",
            refs=["/AGENTS.MD"],
            read_cache={},
            read=fake_read,
            actor_id="emp_036",
        )
        assert r.added == ["/proc/stores/store_bratislava_stare_mesto.json"]

    def test_actor_id_ignored_when_cust(self):
        r = complete_store_back_refs(
            task_text="Check my store's same-day availability.",
            refs=["/AGENTS.MD"],
            read_cache={},
            read=lambda p: None,
            actor_id="cust_017",
        )
        assert r.aborted
        assert r.abort_reason == "no_employee_ref"

    def test_actor_id_ignored_when_no_read_callback(self):
        # cache empty, no read callback → can't fetch
        r = complete_store_back_refs(
            task_text="Check my store's same-day availability.",
            refs=["/AGENTS.MD"],
            read_cache={},
            read=None,
            actor_id="emp_036",
        )
        # Falls through to no_store_id (body never fetched)
        assert not r.aborted
        assert r.added == []

    def test_emp_ref_in_refs_overrides_actor_id(self):
        def fake_read(path):
            assert False, "should not call read; emp body already in cache"
        r = complete_store_back_refs(
            task_text="Check my store's same-day availability.",
            refs=["/AGENTS.MD", "/proc/employees/emp_036.json"],
            read_cache={"/proc/employees/emp_036.json": EMP_036_BODY},
            read=fake_read,
            actor_id="emp_099",  # ignored — emp_036 already in refs
        )
        assert r.added == ["/proc/stores/store_bratislava_stare_mesto.json"]
