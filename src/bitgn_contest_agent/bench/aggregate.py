"""Pure multi-run aggregator for bench_summary v1.1.

Consumes a list of per-run summary dicts (same shape as bench_summary v1.0
output) and returns a dict of v1.1 aggregate fields. Statistics use stdlib
only: statistics.median for the point estimate and a percentile-bootstrap
with a fixed seed for the confidence interval.
"""
from __future__ import annotations

import random
import statistics
from typing import Any, Dict, List

_BOOTSTRAP_RESAMPLES = 1000
_CI_LOWER_PCT = 0.025
_CI_UPPER_PCT = 0.975


def _bootstrap_ci(values: List[float], *, seed: int) -> tuple[float, float]:
    """Percentile bootstrap CI. Stdlib-only, deterministic under seed."""
    if len(values) <= 1:
        v = values[0] if values else 0.0
        return v, v
    rng = random.Random(seed)
    n = len(values)
    medians: List[float] = []
    for _ in range(_BOOTSTRAP_RESAMPLES):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        medians.append(statistics.median(sample))
    medians.sort()
    lo_idx = int(_CI_LOWER_PCT * _BOOTSTRAP_RESAMPLES)
    hi_idx = int(_CI_UPPER_PCT * _BOOTSTRAP_RESAMPLES) - 1
    return medians[lo_idx], medians[hi_idx]


def aggregate_runs(runs: List[Dict[str, Any]], *, seed: int) -> Dict[str, Any]:
    """Return v1.1 aggregate fields across a list of per-run summaries.

    runs: each entry is a bench_summary v1.0-shaped dict with "overall"
        containing "total_runs", "total_passes", "pass_rate".
    seed: RNG seed for the bootstrap CI (must be fixed across invocations
        to keep ratchet comparisons stable).
    """
    if not runs:
        return {
            "runs_per_task": 0,
            "pass_rate_median": 0.0,
            "pass_rate_min": 0.0,
            "pass_rate_ci_lower": 0.0,
            "pass_rate_ci_upper": 0.0,
        }
    pass_rates = [float(r["overall"]["pass_rate"]) for r in runs]
    median = statistics.median(pass_rates)
    lo, hi = _bootstrap_ci(pass_rates, seed=seed)
    return {
        "runs_per_task": len(runs),
        "pass_rate_median": median,
        "pass_rate_min": min(pass_rates),
        "pass_rate_ci_lower": lo,
        "pass_rate_ci_upper": hi,
    }
