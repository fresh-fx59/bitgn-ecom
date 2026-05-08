from bitgn_contest_agent.bench.aggregate import aggregate_runs


def test_single_run_is_pass_rate_identity():
    """N=1 compatibility: single-run input must yield pass_rate_median ==
    pass_rate_min == overall.pass_rate, and CI bounds == point estimate."""
    runs = [
        {"overall": {"total_runs": 43, "total_passes": 22, "pass_rate": 22 / 43}},
    ]
    out = aggregate_runs(runs, seed=12345)
    assert out["pass_rate_median"] == 22 / 43
    assert out["pass_rate_min"] == 22 / 43
    assert out["pass_rate_ci_lower"] == 22 / 43
    assert out["pass_rate_ci_upper"] == 22 / 43
    assert out["runs_per_task"] == 1


def test_three_run_aggregate_hand_computed():
    """Given pass_rates 20/43, 22/43, 24/43, median must be 22/43 and
    min must be 20/43. CI bounds must straddle the median."""
    runs = [
        {"overall": {"total_runs": 43, "total_passes": n, "pass_rate": n / 43}}
        for n in (20, 22, 24)
    ]
    out = aggregate_runs(runs, seed=12345)
    assert out["pass_rate_median"] == 22 / 43
    assert out["pass_rate_min"] == 20 / 43
    assert out["pass_rate_ci_lower"] <= out["pass_rate_median"] <= out["pass_rate_ci_upper"]
    assert out["runs_per_task"] == 3


def test_seed_is_deterministic():
    runs = [
        {"overall": {"total_runs": 43, "total_passes": n, "pass_rate": n / 43}}
        for n in (20, 22, 24)
    ]
    a = aggregate_runs(runs, seed=12345)
    b = aggregate_runs(runs, seed=12345)
    assert a == b
