#!/usr/bin/env python3
"""Faithful-as-possible local Monte-Carlo simulator for the PROD dispatch-wave
grader, used for OFFLINE A/B of planner variants (never shipped into the agent).

The PROD grader feeds the agent's plan into a stochastic discrete-event shipping
simulator and reports (per `score_detail`):

    dispatch packages delivered avg D of N
    dispatch money made avg EUR M delivered margin
    dispatch packages missed avg X; late avg L
    dispatch costs/penalties avg EUR T transport; EUR LP late; EUR MP missed; EUR IV invalid
    dispatch resulting gain avg EUR G; max EUR Gmax; efficiency E%

This module reproduces that economic model so we can score a plan locally and
compare variants. The exact delay distribution + reference-optimal are unknown,
so DELAY/penalty parameters are explicit and we validate improvements with a
PARAMETER SWEEP (a change must win across the whole plausible range, not one
tuning) — see scripts/dispatch_ab.py.

Model
-----
* Network: directed lanes (eta, capacity-per-trip, cost, delay_hint).
* A plan assigns each package a route (lane list) + a global priority (lower
  loads first).
* Discrete-event traversal: a package becomes available at its from_store at
  t=0, then walks its route lane by lane. At each lane it joins a queue ordered
  by package priority; the lane dispatches `capacity` packages per trip and a
  trip takes `eta`; the q-th waiter (0-indexed, by priority, among packages
  ready at-or-before it) departs after floor(q/capacity) extra trips of wait.
  Traversal adds eta + a stochastic delay drawn from the lane's delay_hint.
* Scoring: a package delivered on time earns full margin; delivered late earns
  margin minus a per-time late penalty; never-arriving (no route) is missed
  (margin forfeited + miss penalty). gain = delivered_margin - transport -
  late - missed. efficiency = gain / reference_gain, where reference_gain is
  the deterministic no-delay optimum (all on time at min feasible transport).
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field

from bitgn_contest_agent import dispatch_planner as dp


# ── delay model ───────────────────────────────────────────────────────
@dataclass
class SimParams:
    p_unlikely: float = 0.15
    p_likely: float = 0.60
    mag_short: float = 1.0
    mag_medium: float = 2.0
    mag_long: float = 4.0
    # EUR per time-unit late. Calibrated to the grader: t004 reported
    # late_pen 6.23 EUR for late avg 0.5 -> ~12.5 EUR per late package;
    # with avg lateness ~1.5 time units that is ~8 EUR/time.
    late_penalty_per_time: float = 8.0   # EUR per time-unit late
    # fraction of a package's margin forfeited when it arrives late (the
    # grader's efficiency gap >> the per-time penalty implies a margin hit).
    late_margin_forfeit: float = 0.0     # 0..1
    miss_penalty: float = 50.0           # EUR per missed package
    trip_wait_scale: float = 1.0         # how strongly capacity contention bites
    n_trials: int = 4000
    seed: int = 12345


def _delay_spec(hint: str, p: SimParams) -> tuple[float, float]:
    """Return (prob_delay, magnitude) for a lane's delay_hint."""
    h = (hint or "").lower()
    prob = p.p_likely if "likely" in h else p.p_unlikely
    if "long" in h:
        mag = p.mag_long
    elif "medium" in h:
        mag = p.mag_medium
    else:
        mag = p.mag_short
    return prob, mag


# ── simulation ────────────────────────────────────────────────────────
@dataclass
class Metrics:
    delivered: float = 0.0
    on_time: float = 0.0
    late: float = 0.0
    missed: float = 0.0
    money_made: float = 0.0
    transport: float = 0.0
    late_pen: float = 0.0
    miss_pen: float = 0.0
    gain: float = 0.0
    gains: list = field(default_factory=list)

    def efficiency(self, reference_gain: float) -> float:
        if reference_gain <= 0:
            return 0.0
        return self.gain / reference_gain


def _route_lanes(route_ids, by_id):
    try:
        return [by_id[lid] for lid in route_ids]
    except KeyError:
        return None


def reference_gain(packages, lanes) -> float:
    """Deterministic no-delay optimum: every package delivered on time via its
    cheapest feasible route; gain = Σ margin − Σ min transport. This is the
    best a perfect static plan could do absent stochastic delay (the score
    denominator)."""
    adj = {}
    for ln in lanes:
        adj.setdefault(ln.from_, []).append(ln)
    total_margin = 0.0
    total_cost = 0.0
    for pkg in packages:
        total_margin += pkg.margin_cents
        route = dp._pick_route(pkg, adj)  # cheapest on-time route
        if route:
            total_cost += sum(l.cost_cents for l in route)
    return (total_margin - total_cost) / 100.0


def simulate(plan_obj, packages, lanes, params: SimParams) -> Metrics:
    by_id = {ln.lane_id: ln for ln in lanes}
    by_pkg = {p.package_id: p for p in packages}
    rng = random.Random(params.seed)

    # priority lookup + per-lane ordered membership (for capacity contention)
    prio = {a["package_id"]: a.get("priority", 999) for a in plan_obj["assignments"]}
    lane_members: dict[str, list[str]] = {}
    routes: dict[str, list] = {}
    for a in plan_obj["assignments"]:
        pid = a["package_id"]
        lane_objs = _route_lanes(a.get("route") or [], by_id)
        routes[pid] = lane_objs
        if lane_objs:
            for ln in lane_objs:
                lane_members.setdefault(ln.lane_id, []).append(pid)
    # order each lane's members by priority (lower loads first)
    for lid in lane_members:
        lane_members[lid].sort(key=lambda pid: prio.get(pid, 999))

    m = Metrics()
    delay_cache = {lid: _delay_spec(by_id[lid].delay_hint, params) for lid in by_id}

    for _ in range(params.n_trials):
        trial_money = 0.0
        trial_transport = 0.0
        trial_late = 0.0
        trial_miss = 0.0
        for pkg in packages:
            lane_objs = routes.get(pkg.package_id)
            if not lane_objs:  # no/invalid route → missed
                m.missed += 1
                trial_miss += params.miss_penalty
                continue
            t = 0.0
            for ln in lane_objs:
                # capacity contention: position among same-lane users by priority
                members = lane_members.get(ln.lane_id, [pkg.package_id])
                q = members.index(pkg.package_id)
                wait = (q // max(1, ln.capacity)) * ln.eta * params.trip_wait_scale
                # stochastic delay
                p_d, mag = delay_cache[ln.lane_id]
                delay = mag if rng.random() < p_d else 0.0
                t += wait + ln.eta + delay
                trial_transport += ln.cost_cents
            margin = pkg.margin_cents / 100.0  # EUR
            m.delivered += 1
            if t <= pkg.due_time:
                m.on_time += 1
                trial_money += margin
            else:
                m.late += 1
                lateness = t - pkg.due_time
                # late earns margin minus a forfeit fraction; the per-time
                # penalty is booked separately (as the grader reports it).
                trial_money += margin * (1.0 - params.late_margin_forfeit)
                trial_late += params.late_penalty_per_time * lateness
        trial_transport /= 100.0  # cost_cents summed above -> EUR
        gain = trial_money - trial_transport - trial_late - trial_miss
        m.money_made += trial_money
        m.transport += trial_transport
        m.late_pen += trial_late
        m.miss_pen += trial_miss
        m.gain += gain
        m.gains.append(gain)

    n = params.n_trials
    for fld in ("delivered", "on_time", "late", "missed", "money_made",
                "transport", "late_pen", "miss_pen", "gain"):
        setattr(m, fld, getattr(m, fld) / n)
    return m


def score_plan(plan_obj, packages, lanes, params: SimParams) -> dict:
    ref = reference_gain(packages, lanes)
    m = simulate(plan_obj, packages, lanes, params)
    return {
        "delivered": round(m.delivered, 2),
        "on_time": round(m.on_time, 2),
        "late": round(m.late, 2),
        "missed": round(m.missed, 2),
        "money_made": round(m.money_made, 2),
        "transport": round(m.transport, 2),
        "late_pen": round(m.late_pen, 2),
        "gain": round(m.gain, 2),
        "gain_max": round(max(m.gains), 2) if m.gains else 0.0,
        "reference_gain": round(ref, 2),
        "efficiency": round(m.efficiency(ref), 4),
    }


def _load(wave_dir: str):
    import os
    pk = open(os.path.join(wave_dir, "packages.tsv"), encoding="utf-8").read()
    ln = open(os.path.join(wave_dir, "lanes.tsv"), encoding="utf-8").read()
    return dp.parse_packages(pk), dp.parse_lanes(ln)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wave_dir", help="dir with packages.tsv + lanes.tsv")
    ap.add_argument("--trials", type=int, default=4000)
    args = ap.parse_args()
    pkgs, lanes = _load(args.wave_dir)
    params = SimParams(n_trials=args.trials)
    plan = dp.plan(pkgs, lanes)
    res = score_plan(plan, pkgs, lanes, params)
    print("current planner:", res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
