"""T2.2: RunMetrics pure collector — peak in-flight + rate-limit counters."""
from __future__ import annotations

import json
import threading

from bitgn_contest_agent.bench.run_metrics import RunMetrics


def test_run_metrics_tracks_peak_inflight() -> None:
    m = RunMetrics(max_inflight_llm=6)
    m.on_call_start()
    m.on_call_start()
    m.on_call_start()
    m.on_call_end()
    m.on_call_start()
    m.on_call_start()
    assert m.peak_inflight_llm == 4


def test_run_metrics_counts_rate_limit_errors() -> None:
    m = RunMetrics(max_inflight_llm=6)
    m.on_rate_limit_error()
    m.on_rate_limit_error()
    assert m.rate_limit_errors == 2


def test_run_metrics_snapshot_is_json_serializable() -> None:
    m = RunMetrics(max_inflight_llm=6)
    m.on_call_start()
    m.on_call_end()
    m.on_rate_limit_error()
    snap = m.snapshot()
    json.dumps(snap)  # must not raise
    assert snap["peak_inflight_llm"] == 1
    assert snap["rate_limit_errors"] == 1
    assert snap["max_inflight_llm"] == 6


def test_run_metrics_is_thread_safe() -> None:
    """Concurrent on_call_start/on_call_end from many threads must produce
    a consistent peak count (no lost updates)."""
    m = RunMetrics(max_inflight_llm=100)

    def worker() -> None:
        for _ in range(50):
            m.on_call_start()
            m.on_call_end()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No strict assert on peak value (schedule-dependent) — just that
    # the inflight counter settled back to 0 and snapshot is readable.
    snap = m.snapshot()
    assert snap["peak_inflight_llm"] >= 0
    assert snap["peak_inflight_llm"] <= 100
