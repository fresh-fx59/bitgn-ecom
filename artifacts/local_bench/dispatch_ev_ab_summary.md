# Dispatch planner EV-optimization — local A/B evidence (2026-05-30, v0.1.161)

**Problem.** PROD dispatch-wave tasks t004/t014/t024/t044/t064 all scored a tight
~0.82 in run-22S2C61… (v0.1.160) despite the deterministic `dispatch_planner`
firing ("planned 10 packages"). The grader `score_detail` shows the loss is
economic efficiency: all 10 delivered, but ~0.4–0.7 packages **late** + transport
cost. `gain = delivered_margin − transport − late − missed`, `efficiency = gain /
reference`. Late deliveries forfeit margin (the efficiency gap, ~17% / ~110 EUR,
far exceeds the reported per-time late penalty of ~6 EUR).

**Root cause.** The old `plan()` chose, per package independently, the **min-cost
route among nominally-on-time routes** — ignoring (a) the stochastic delay
probability in each lane's `delay_hint` and (b) lane-capacity contention from other
packages. The doc says literally *"Maximize expected net profit, not just the number
of delivered packages."* The greedy did not.

**Fix.** `plan()` now does sequential, capacity-aware assignment in priority order
(urgent/valuable first), choosing each route to **maximize expected net profit**:
`margin·P(on-time) + margin·(1−forfeit)·P(late) − transport`, where `P(on-time)`
erodes nominal slack by the route's expected stochastic delay, and a contention
term reflects capacity already committed by higher-priority packages. Priority
ranking (due_time asc, margin desc) is unchanged.

**Local validation.** Built a faithful Monte-Carlo simulator of the grader's
economic model (`scripts/dispatch_sim.py`) + a sweep A/B harness
(`scripts/dispatch_ab.py`), calibrated the late penalty to the grader's reported
~8 EUR/time. Compared old greedy vs new EV planner on 4 captured PROD waves across
a 4-regime grid (delay severity × margin-forfeit fraction 0.0–0.6), 5000 trials each:

| wave | old on-time | new on-time | Δ gain (range over regimes) |
|---|---|---|---|
| wave-iWNjqLmp | 8.3–8.6 | 9.3–9.5 | **+16 … +39 EUR** |
| wave-ApdgNmry | 7.9–8.5 | 8.7–9.0 | **+3 … +21 EUR** |
| wave-BD2bv3HB | 10.0 (already optimal) | 10.0 | +0 (no regression) |
| wave-Yj4oo8jz | 9.0–9.1 | 9.3–9.5 | **+6 … +17 EUR** |

**Robustness.** WIN in every non-trivial cell, **0 regressions**, across baked
planner forfeit ∈ {0.4, 0.5, 0.8} and the full scoring grid — including the most
conservative "per-time-penalty-only, no margin forfeit" regime. The win is driven
by reliability (more on-time) at a small (≤+5 EUR) transport premium that the
margin recovery dwarfs.

**Honesty caveat.** The grader's exact stochastic delay distribution and
reference-optimal are not observable; the simulator's absolute efficiency is not
calibrated to the grader's. The *direction* is model-free and robust: the new plan
delivers strictly more packages on-time at comparable transport on every captured
wave, and on-time/late/transport are the grader's own gain components. The PROD
run is the final confirmation. The change is gated (`BITGN_USE_DISPATCH_PLANNER`)
and validate-guarded (abstains to the LLM plan if a plan is malformed), so downside
is bounded.

Suite: 881 passed / 3 skipped. New deterministic tests lock the reliability
preference and capacity-awareness (`tests/test_dispatch_planner.py`).
