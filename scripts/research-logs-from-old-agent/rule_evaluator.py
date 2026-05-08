"""Evaluate candidate enforcer rules against the paired recent-format corpus.

Each rule returns ``True`` when the terminal emission would be **rejected**.

For every rule we print:

- ``fire@pass``  — fraction of actually-passing runs the rule fires on
  (false-positive rate; we want this near zero)
- ``fire@fail``  — fraction of actually-failing runs the rule fires on
  (true-positive rate; higher is better)
- ``delta``      — percentage-point gap between the two (our discrimination
  metric)
- ``precision``  — of runs the rule fires on, fraction that actually failed
- ``catches``    — absolute number of failures the rule catches (useful to
  spot "small but clean" rules)

Add a new candidate by appending to ``CANDIDATES`` below. No other edits
needed.

Usage:
    python3 scripts/research-logs-from-old-agent/rule_evaluator.py --all
    python3 scripts/research-logs-from-old-agent/rule_evaluator.py --rule identity_gate
"""

from __future__ import annotations

import argparse
from typing import Callable

from legacy_loader import (
    IDENTITY_TOOLS,
    READ_LIKE_TOOLS,
    REFUSAL_OUTCOMES,
    Run,
    load_runs,
    partition,
)


RuleFn = Callable[[Run], bool]


def _err_internal(r: Run) -> bool:
    return (r.outcome or "").upper() == "OUTCOME_ERR_INTERNAL"


def identity_gate(r: Run) -> bool:
    """Non-refusal terminal without any identity-context tool call."""

    if r.outcome in REFUSAL_OUTCOMES:
        return False
    return not (r.tools_called & IDENTITY_TOOLS)


def nontrivial_work_gate(r: Run) -> bool:
    """Non-refusal terminal without any read-like tool call."""

    if r.outcome in REFUSAL_OUTCOMES:
        return False
    return not (r.tools_called & READ_LIKE_TOOLS)


def finalization_not_ready(r: Run) -> bool:
    """Non-refusal terminal emitted while the planner's own state says it is
    not ready to finalize."""

    if r.outcome in REFUSAL_OUTCOMES:
        return False
    return r.finalization_ready is False


def respond_instructions_missing(r: Run) -> bool:
    """Non-refusal terminal without respond-instructions loaded."""

    if r.outcome in REFUSAL_OUTCOMES:
        return False
    return r.respond_instructions_loaded is False


def err_internal(r: Run) -> bool:
    """Any OUTCOME_ERR_INTERNAL terminal — the planner gave up."""

    return _err_internal(r)


def grounding_refs_empty_on_ok(r: Run) -> bool:
    """OUTCOME_OK emitted with an empty ``grounding_refs`` list."""

    return (r.outcome or "").upper() == "OUTCOME_OK" and not r.grounding_refs


def post_mutation_unverified(r: Run) -> bool:
    """OUTCOME_OK with ``has_mutation`` True but ``post_mutation_verification``
    not True. The planner mutated state but never re-read it."""

    if (r.outcome or "").upper() != "OUTCOME_OK":
        return False
    return r.has_mutation is True and r.post_mutation_verification is not True


def loop_forced_fallback(r: Run) -> bool:
    """Any terminal where the planner's loop-risk signaled a forced fallback."""

    return r.forced_fallback is True


def read_count_zero(r: Run) -> bool:
    """Non-refusal terminal with ``evidence_inventory.read_count == 0``."""

    if r.outcome in REFUSAL_OUTCOMES:
        return False
    return r.read_count == 0


CANDIDATES: dict[str, RuleFn] = {
    "identity_gate": identity_gate,
    "nontrivial_work_gate": nontrivial_work_gate,
    "read_count_zero": read_count_zero,
    "finalization_not_ready": finalization_not_ready,
    "respond_instructions_missing": respond_instructions_missing,
    "err_internal": err_internal,
    "grounding_refs_empty_on_ok": grounding_refs_empty_on_ok,
    "post_mutation_unverified": post_mutation_unverified,
    "loop_forced_fallback": loop_forced_fallback,
}


def evaluate(name: str, rule: RuleFn, passed: list[Run], failed: list[Run]) -> dict:
    fire_pass = sum(1 for r in passed if rule(r))
    fire_fail = sum(1 for r in failed if rule(r))
    fire_total = fire_pass + fire_fail
    precision = (fire_fail / fire_total) if fire_total else 0.0
    rate_pass = (fire_pass / len(passed)) if passed else 0.0
    rate_fail = (fire_fail / len(failed)) if failed else 0.0
    return {
        "name": name,
        "fire_pass": fire_pass,
        "fire_fail": fire_fail,
        "rate_pass": rate_pass,
        "rate_fail": rate_fail,
        "delta_pp": (rate_fail - rate_pass) * 100.0,
        "precision": precision,
        "catches": fire_fail,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", help="evaluate a single named rule")
    ap.add_argument("--all", action="store_true", help="evaluate all rules")
    args = ap.parse_args()

    if not args.rule and not args.all:
        ap.error("pass --rule NAME or --all")

    runs = load_runs()
    passed, failed = partition(runs)
    print(f"corpus: {len(runs)} runs (pass={len(passed)}, fail={len(failed)})")
    print()

    if args.rule:
        if args.rule not in CANDIDATES:
            ap.error(f"unknown rule {args.rule!r}; choose from {list(CANDIDATES)}")
        targets = [args.rule]
    else:
        targets = list(CANDIDATES)

    header = f"{'rule':<32} {'fire@pass':>10} {'fire@fail':>10} {'delta_pp':>10} {'precision':>10} {'catches':>8}"
    print(header)
    print("-" * len(header))
    results = []
    for name in targets:
        res = evaluate(name, CANDIDATES[name], passed, failed)
        results.append(res)
    # Sort by delta_pp descending so the strongest signals float to the top.
    results.sort(key=lambda r: r["delta_pp"], reverse=True)
    for r in results:
        print(
            f"{r['name']:<32} "
            f"{r['fire_pass']:>4}/{len(passed):<5} "
            f"{r['fire_fail']:>4}/{len(failed):<5} "
            f"{r['delta_pp']:>9.1f} "
            f"{r['precision']*100:>9.1f}% "
            f"{r['catches']:>8}"
        )


if __name__ == "__main__":
    main()
