"""TDD spec for the deterministic dispatch-wave planner.

The planner solves the PROD "dispatch wave" task family: route a set of
inter-store transfer packages over a directed hub-and-spoke lane network,
maximising expected net profit (on-time margin − lane costs − penalties).

Fixture: a real PROD instance captured under
``artifacts/prod_explore/dispatch_wave_iWNjqLmp/``. We TDD against it.
"""
from __future__ import annotations

import os

import pytest

from bitgn_contest_agent import dispatch_planner as dp

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "artifacts",
    "prod_explore",
    "dispatch_wave_iWNjqLmp",
)


def _read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def packages_tsv() -> str:
    return _read_fixture("packages.tsv")


@pytest.fixture
def lanes_tsv() -> str:
    return _read_fixture("lanes.tsv")


@pytest.fixture
def wave_md() -> str:
    return _read_fixture("wave_dispatch.md")


# ── route validator helper (used by several assertions) ───────────────
def _lane_by_id(lanes):
    return {ln.lane_id: ln for ln in lanes}


def assert_route_connected(route, package, lanes):
    """Walk the route; assert every lane is real, consecutive lanes chain,
    and the path starts at from_store_id and ends at to_store_id."""
    by_id = _lane_by_id(lanes)
    assert route, f"empty route for {package.package_id}"
    # every id is a real lane id
    for lid in route:
        assert lid in by_id, f"unknown lane {lid!r} in route for {package.package_id}"
    first = by_id[route[0]]
    last = by_id[route[-1]]
    assert first.from_ == package.from_store_id, (
        f"{package.package_id}: route starts at {first.from_!r}, "
        f"expected {package.from_store_id!r}"
    )
    assert last.to == package.to_store_id, (
        f"{package.package_id}: route ends at {last.to!r}, "
        f"expected {package.to_store_id!r}"
    )
    # consecutive lanes connect
    for a, b in zip(route, route[1:]):
        assert by_id[a].to == by_id[b].from_, (
            f"{package.package_id}: lane {a} ends at {by_id[a].to!r} "
            f"but next lane {b} starts at {by_id[b].from_!r}"
        )


def _route_eta(route, lanes):
    by_id = _lane_by_id(lanes)
    return sum(by_id[lid].eta for lid in route)


# ── parsing ───────────────────────────────────────────────────────────
def test_parse_packages_count(packages_tsv):
    pkgs = dp.parse_packages(packages_tsv)
    assert len(pkgs) == 10
    p1 = next(p for p in pkgs if p.package_id == "XFER-001")
    assert p1.from_store_id == "store-linz-kleinmuenchen"
    assert p1.to_store_id == "store-vie-meidling"
    assert p1.due_time == 13
    assert p1.margin_cents == 4497
    assert isinstance(p1.due_time, int)
    assert isinstance(p1.margin_cents, int)


def test_parse_lanes_count(lanes_tsv):
    lanes = dp.parse_lanes(lanes_tsv)
    assert len(lanes) == 30
    ln = next(l for l in lanes if l.lane_id == "lane-hub-east-hub-central")
    assert ln.from_ == "hub-east"
    assert ln.to == "hub-central"
    assert ln.capacity == 4
    assert ln.eta == 4
    assert ln.cost_cents == 260
    assert all(isinstance(l.eta, int) for l in lanes)
    assert all(isinstance(l.capacity, int) for l in lanes)
    assert all(isinstance(l.cost_cents, int) for l in lanes)


# ── plan ──────────────────────────────────────────────────────────────
def test_plan_one_assignment_per_package(packages_tsv, lanes_tsv):
    pkgs = dp.parse_packages(packages_tsv)
    lanes = dp.parse_lanes(lanes_tsv)
    plan = dp.plan(pkgs, lanes)
    assert set(plan.keys()) == {"assignments"}
    assigns = plan["assignments"]
    assert len(assigns) == 10
    assigned_ids = {a["package_id"] for a in assigns}
    assert assigned_ids == {p.package_id for p in pkgs}


def test_plan_routes_connected_and_on_time(packages_tsv, lanes_tsv):
    pkgs = dp.parse_packages(packages_tsv)
    lanes = dp.parse_lanes(lanes_tsv)
    by_pkg = {p.package_id: p for p in pkgs}
    plan = dp.plan(pkgs, lanes)
    for a in plan["assignments"]:
        pkg = by_pkg[a["package_id"]]
        route = a["route"]
        assert route, f"{pkg.package_id} got empty route"
        assert_route_connected(route, pkg, lanes)
        # these fixture instances are all feasible on-time via hubs
        eta = _route_eta(route, lanes)
        assert eta <= pkg.due_time, (
            f"{pkg.package_id}: route eta {eta} > due {pkg.due_time}"
        )


def test_plan_priorities_are_ranking(packages_tsv, lanes_tsv):
    pkgs = dp.parse_packages(packages_tsv)
    lanes = dp.parse_lanes(lanes_tsv)
    by_pkg = {p.package_id: p for p in pkgs}
    plan = dp.plan(pkgs, lanes)
    prios = [a["priority"] for a in plan["assignments"]]
    assert all(isinstance(p, int) for p in prios)
    # a strict ranking: 1..N permutation
    assert sorted(prios) == list(range(1, len(pkgs) + 1))
    # most-urgent (lowest due_time) gets priority 1
    prio1 = next(a for a in plan["assignments"] if a["priority"] == 1)
    min_due = min(p.due_time for p in pkgs)
    assert by_pkg[prio1["package_id"]].due_time == min_due
    # priority order is non-decreasing in due_time (urgency first)
    ordered = sorted(plan["assignments"], key=lambda a: a["priority"])
    dues = [by_pkg[a["package_id"]].due_time for a in ordered]
    assert dues == sorted(dues)


def test_plan_prefers_lower_cost_over_direct(packages_tsv, lanes_tsv):
    """XFER-001 (linz-kleinmuenchen → vie-meidling, due 13) should use the
    cheap hub route, not the expensive lane-direct (cost 980, eta 12)."""
    pkgs = dp.parse_packages(packages_tsv)
    lanes = dp.parse_lanes(lanes_tsv)
    plan = dp.plan(pkgs, lanes)
    a = next(a for a in plan["assignments"] if a["package_id"] == "XFER-001")
    assert "lane-direct-store-linz-kleinmuenchen-store-vie-meidling" not in a["route"]


# ── reliability-aware routing (expected-net-profit, not min nominal cost) ─
def test_plan_prefers_reliable_route_over_tight_risky_cheaper():
    """A package with a tight, risky, but cheaper direct lane vs a slightly
    pricier but safe hub route should take the SAFE route — the simulator
    forfeits most of a late package's margin, so reliability outweighs a small
    cost saving (the doc's 'maximise expected net profit')."""
    pkgs = [
        dp.Package(
            package_id="P1", sku="", product_ref="",
            from_store_id="S", from_store_ref="",
            to_store_id="D", to_store_ref="",
            due_time=5, margin_cents=10000, reason="",
        )
    ]
    lanes = [
        # cheap, tight (eta == due → zero slack), and high-risk
        dp.Lane("lane-direct-S-D", "S", "D", 1, 5, 100,
                "delays likely; long when delayed"),
        # pricier but safe two-hop hub route with slack
        dp.Lane("lane-S-H", "S", "H", 2, 2, 80, "delays unlikely; short when delayed"),
        dp.Lane("lane-H-D", "H", "D", 2, 2, 80, "delays unlikely; short when delayed"),
    ]
    plan = dp.plan(pkgs, lanes)
    route = plan["assignments"][0]["route"]
    assert route == ["lane-S-H", "lane-H-D"], (
        f"expected the safe hub route, got {route}"
    )


def test_plan_is_capacity_aware_spreads_load():
    """Two packages S→D sharing a capacity-1 bottleneck: the planner should
    not pile both onto the same scarce lane when an alternative exists — the
    lower-priority package routes around the committed capacity."""
    # tight deadline (5): a second package queued behind the capacity-1 lane
    # would wait a trip and miss the deadline, so EV diverts it to the
    # alternative hub. (With a loose deadline sharing the cheap lane is
    # genuinely optimal — capacity-awareness should only bite when it must.)
    pkgs = [
        dp.Package("P1", "", "", "S", "", "D", "", 5, 5000, ""),
        dp.Package("P2", "", "", "S", "", "D", "", 5, 5000, ""),
    ]
    lanes = [
        # primary cheap path through a capacity-1 hub lane
        dp.Lane("lane-S-H1", "S", "H1", 1, 2, 50, "delays unlikely; short when delayed"),
        dp.Lane("lane-H1-D", "H1", "D", 2, 2, 50, "delays unlikely; short when delayed"),
        # alternative path of comparable cost via a different hub
        dp.Lane("lane-S-H2", "S", "H2", 2, 2, 60, "delays unlikely; short when delayed"),
        dp.Lane("lane-H2-D", "H2", "D", 2, 2, 60, "delays unlikely; short when delayed"),
    ]
    plan = dp.plan(pkgs, lanes)
    routes = {a["package_id"]: a["route"] for a in plan["assignments"]}
    # not both packages should sit first on the capacity-1 lane-S-H1
    on_bottleneck = [pid for pid, r in routes.items() if r and r[0] == "lane-S-H1"]
    assert len(on_bottleneck) <= 1, (
        f"both packages overloaded the capacity-1 lane: {routes}"
    )


# ── validate ──────────────────────────────────────────────────────────
def test_validate_accepts_good_plan(packages_tsv, lanes_tsv):
    pkgs = dp.parse_packages(packages_tsv)
    lanes = dp.parse_lanes(lanes_tsv)
    plan = dp.plan(pkgs, lanes)
    assert dp.validate(plan, pkgs, lanes) is True


def test_validate_rejects_incomplete_plan(packages_tsv, lanes_tsv):
    pkgs = dp.parse_packages(packages_tsv)
    lanes = dp.parse_lanes(lanes_tsv)
    plan = dp.plan(pkgs, lanes)
    # drop one assignment → incomplete
    broken = {"assignments": plan["assignments"][:-1]}
    assert dp.validate(broken, pkgs, lanes) is False
    # bad lane id → disconnected
    bad = {
        "assignments": [
            dict(plan["assignments"][0], route=["lane-does-not-exist"])
        ]
        + plan["assignments"][1:]
    }
    assert dp.validate(bad, pkgs, lanes) is False


# ── extract_wave_path ─────────────────────────────────────────────────
def test_extract_wave_path_matches():
    txt = "Plan the dispatch wave described in /ops/dispatch/wave-iWNjqLmp/dispatch.md."
    assert dp.extract_wave_path(txt) == "/ops/dispatch/wave-iWNjqLmp/dispatch.md"


def test_extract_wave_path_none_for_non_dispatch():
    assert dp.extract_wave_path("How many catalogue products are pliers?") is None
    assert dp.extract_wave_path("") is None
    assert dp.extract_wave_path("Refund the customer for return ret_007.") is None


# ── plan_from_paths ───────────────────────────────────────────────────
def _fixture_reader(path: str):
    # serve the wave md and the two TSVs from the fixture dir
    mapping = {
        "/ops/dispatch/wave-iWNjqLmp/packages.tsv": "packages.tsv",
        "/ops/dispatch/wave-iWNjqLmp/lanes.tsv": "lanes.tsv",
    }
    if path in mapping:
        return _read_fixture(mapping[path])
    return None


def test_plan_from_paths_reproduces_plan(wave_md, packages_tsv, lanes_tsv):
    pkgs = dp.parse_packages(packages_tsv)
    lanes = dp.parse_lanes(lanes_tsv)
    direct = dp.plan(pkgs, lanes)
    via_paths = dp.plan_from_paths(wave_md, _fixture_reader)
    assert via_paths is not None
    assert via_paths == direct
    assert len(via_paths["assignments"]) == 10


def test_plan_from_paths_abstains_on_missing(wave_md):
    # reader returns None for everything → cannot read TSVs → abstain
    assert dp.plan_from_paths(wave_md, lambda p: None) is None


def test_plan_from_paths_abstains_on_unparseable_wave():
    assert dp.plan_from_paths("no paths here", _fixture_reader) is None


# ── gating ────────────────────────────────────────────────────────────
def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BITGN_USE_DISPATCH_PLANNER", raising=False)
    assert dp.is_enabled() is False


def test_enabled_when_flag_set(monkeypatch):
    monkeypatch.setenv("BITGN_USE_DISPATCH_PLANNER", "1")
    assert dp.is_enabled() is True
    monkeypatch.setenv("BITGN_USE_DISPATCH_PLANNER", "0")
    assert dp.is_enabled() is False
