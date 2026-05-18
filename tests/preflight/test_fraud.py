"""Unit tests for the fraud-detection preflight.

Covers:
  * lexical trigger fires on the three observed PROD phrasings and
    natural paraphrases, and does NOT fire on adjacent tasks that
    mention payment in a non-fraud context (3DS recovery, basket
    checkout) or fraud in a non-payment context.
  * the FraudPreflightResult.as_observation() shape — empty union
    vs non-empty — is what the agent loop will see.
  * run_fraud_probes integrates cleanly with the LocalEcomClient
    when given a hand-crafted catalogue containing a multi-pattern
    fraud world. This is the local A/B regression anchor; if a
    future commit changes the probe SQL and breaks recall on this
    fixture, the test catches it before a PROD bench burn.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from bitgn_contest_agent.preflight.fraud import (
    FraudPreflightResult,
    is_fraud_task,
    run_fraud_probes,
)


# ── trigger tests ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "task",
    [
        # Observed PROD phrasings (v147–v151).
        "Risk Ops confirmed a known fraud hit in the older archived payment history. Identify the fraudulent payment records from history.",
        "Fraud review says one hit is present in the archived payments.",
        "We have a confirmed fraud incident in archived payment history.",
        # Natural paraphrases.
        "Find the fraudulent transactions in the archive.",
        "Identify payment records flagged as fraud in the history.",
        "There is fraud in the transaction history; identify the affected payments.",
    ],
)
def test_trigger_fires_on_fraud_payment_tasks(task: str) -> None:
    assert is_fraud_task(task) is True


@pytest.mark.parametrize(
    "task",
    [
        # Payment without fraud — 3DS recovery, basket checkout, etc.
        "Recover 3DS for basket basket_206; payment id pay_054.",
        "Apply the maximum service_recovery discount to my checkoutable basket.",
        "How many payments are in the catalogue?",
        # Fraud without payment — non-existent in current PROD but
        # captures the trigger discipline ("both axes required").
        "Detect fraud signals in the inventory mutation log.",
        "",
        "Tree the root and report what you see.",
    ],
)
def test_trigger_does_not_fire_on_non_fraud_tasks(task: str) -> None:
    assert is_fraud_task(task) is False


def test_trigger_word_order_either_way() -> None:
    # "payment ... fraud" and "fraud ... payment" both fire.
    assert is_fraud_task("payment is suspected to be fraud") is True
    assert is_fraud_task("fraud detected on this payment") is True


# ── observation rendering ────────────────────────────────────────────


def test_observation_none_when_not_triggered() -> None:
    r = FraudPreflightResult(ran=False, triggered=False)
    assert r.as_observation() is None


def test_observation_when_zero_hits_explains_fallback() -> None:
    r = FraudPreflightResult(
        ran=True,
        triggered=True,
        union_paths=[],
        per_pattern_counts={"card_sharing": 0, "device_sharing": 0,
                             "time_impossible": 0, "observed_vs_store": 0,
                             "coord_cluster": 0},
    )
    obs = r.as_observation() or ""
    assert "ZERO archived payment rows" in obs
    assert "Fall back to the prompt rule" in obs


def test_observation_lists_every_union_path_and_pattern_counts() -> None:
    r = FraudPreflightResult(
        ran=True,
        triggered=True,
        union_paths=[
            "/proc/payments/pay_a.json",
            "/proc/payments/pay_b.json",
            "/proc/payments/pay_c.json",
        ],
        per_pattern_counts={"card_sharing": 2, "device_sharing": 0,
                             "time_impossible": 3, "observed_vs_store": 1,
                             "coord_cluster": 0},
    )
    obs = r.as_observation() or ""
    # Counts surface zero-stripped (zero-hit patterns suppressed).
    assert "card_sharing=2" in obs
    assert "time_impossible=3" in obs
    assert "device_sharing=0" not in obs  # zero hits suppressed
    # Every path appears.
    assert "/proc/payments/pay_a.json" in obs
    assert "/proc/payments/pay_b.json" in obs
    assert "/proc/payments/pay_c.json" in obs
    # Honest framing about why not to drop members.
    assert "Do not drop members" in obs


# ── integration: probes vs a hand-crafted catalogue ──────────────────


def _build_fraud_world(workspace: Path) -> None:
    """A 14-row archived-payments world with three orthogonal fraud
    clusters:

      cluster A (card sharing): pay_001..pay_003 — same
        payment_method_fingerprint, three different customers.
      cluster B (time impossibility): pay_010..pay_011 — one customer
        at two stores 20 seconds apart.
      cluster C (coord cluster): pay_020..pay_022 — three customers
        all sharing observed_lat/lon (rounded).

      plus pay_100..pay_106 — legitimate archived payments that
        should NOT be flagged by any probe.

    Expected union: pay_001, pay_002, pay_003, pay_010, pay_011,
    pay_020, pay_021, pay_022 = 8 rows.
    """
    (workspace / "proc" / "payments").mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.MD").write_text("stub\n", encoding="utf-8")

    db = workspace / "catalogue.db"
    conn = sqlite3.connect(db)
    c = conn.cursor()
    # Minimal schema mirroring the PROD archived-payments shape.
    c.executescript("""
    CREATE TABLE payments (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        basket_archived INTEGER NOT NULL,
        customer_id TEXT NOT NULL,
        store_id TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        payment_method_fingerprint TEXT NOT NULL,
        device_fingerprint TEXT NOT NULL,
        observed_lat REAL NOT NULL,
        observed_lon REAL NOT NULL
    );
    CREATE TABLE stores (
        id TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL
    );
    CREATE TABLE customers (
        id TEXT PRIMARY KEY, home_lat REAL NOT NULL, home_lon REAL NOT NULL
    );
    """)
    c.execute("INSERT INTO stores VALUES ('s_north', 48.20, 16.40)")
    c.execute("INSERT INTO stores VALUES ('s_south', 47.20, 15.40)")
    c.execute("INSERT INTO stores VALUES ('s_west',  46.50, 14.00)")
    for cid in ("c_001", "c_002", "c_003", "c_004", "c_005"):
        c.execute("INSERT INTO customers VALUES (?, 48.0, 16.0)", (cid,))

    rows: list[tuple] = []
    # Cluster A — card shared across c_001..c_003 (same pm_x).
    for i, (pid, cust) in enumerate(
        [("pay_001", "c_001"), ("pay_002", "c_002"), ("pay_003", "c_003")], start=1
    ):
        rows.append((pid, f"/proc/payments/{pid}.json", 1, cust, "s_north",
                     1200 + i*10, f"2021-04-28T08:0{i}:00Z",
                     "pm_x", f"dev_{cust}", 48.20, 16.40))
    # Cluster B — c_004 at s_north then s_south 20s apart.
    rows.append(("pay_010", "/proc/payments/pay_010.json", 1, "c_004", "s_north",
                 3500, "2021-05-15T10:00:00Z", "pm_y1", "dev_y1", 48.20, 16.40))
    rows.append(("pay_011", "/proc/payments/pay_011.json", 1, "c_004", "s_south",
                 2200, "2021-05-15T10:00:20Z", "pm_y2", "dev_y2", 47.20, 15.40))
    # Cluster C — three different customers share same observed coords.
    for i, cust in enumerate(("c_001", "c_002", "c_005"), start=20):
        rows.append((f"pay_0{i}", f"/proc/payments/pay_0{i}.json", 1, cust, "s_west",
                     900 + i, f"2021-06-01T0{i-19}:00:00Z",
                     f"pm_clean_{i}", f"dev_clean_{i}", 49.00, 17.00))
    # Legitimate background — each row at a different store with
    # observed coords ~at the store's actual lat/lon (no cross-customer
    # clustering, no card/device reuse, no impossible travel). These
    # rows must NOT be flagged by any probe.
    _legit_locs = [
        ("c_001", "s_north", 48.20, 16.40),
        ("c_002", "s_south", 47.20, 15.40),
        ("c_003", "s_west",  46.50, 14.00),
        ("c_004", "s_north", 48.21, 16.41),
        ("c_005", "s_south", 47.19, 15.39),
    ]
    for i, (cust, store, olat, olon) in enumerate(_legit_locs, start=100):
        rows.append((f"pay_{i}", f"/proc/payments/pay_{i}.json", 1, cust, store,
                     800 + i, f"2021-09-{i-99:02d}T12:00:00Z",
                     f"pm_legit_{i}", f"dev_legit_{i}", olat, olon))

    c.executemany(
        "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
    )

    # Materialise each payment as a JSON file so the preflight's
    # post-probe reads succeed.
    for r in rows:
        pid = r[0]
        (workspace / "proc" / "payments" / f"{pid}.json").write_text(
            json.dumps({"id": pid, "amount_cents": r[5]}) + "\n",
            encoding="utf-8",
        )

    conn.commit()
    conn.close()


def test_probes_recover_every_fraud_cluster(tmp_path: Path) -> None:
    """End-to-end probe pass against the 3-cluster fixture. Expected
    union = 8 archived rows (3 + 2 + 3) with zero false positives from
    the 7 legitimate background rows."""
    from bitgn_contest_agent.adapter.ecom import EcomAdapter
    from bitgn_contest_agent.adapter.ecom_tracing import TracingEcomClient
    from bitgn_contest_agent.local.ecom_client import LocalEcomClient

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _build_fraud_world(workspace)

    class _FakeSession:
        def __init__(self) -> None:
            self.seen_refs: set[str] = set()
            self.identity_loaded = False
            self.rulebook_loaded = False
            self.mutation_count = 0
            self.step = 0
    class _NullWriter:
        def append_prepass(self, **kw) -> None: pass

    runtime = LocalEcomClient(workspace)
    traced = TracingEcomClient(runtime, writer=None)
    adapter = EcomAdapter(runtime=traced, max_tool_result_bytes=128 * 1024)

    session = _FakeSession()
    result = run_fraud_probes(
        adapter=adapter,
        task_text="Identify fraudulent payment records in archived history.",
        session=session,
        trace_writer=_NullWriter(),
    )

    assert result.triggered, "trigger missed a fraud-shaped task"
    union_ids = {Path(p).stem for p in result.union_paths}
    expected = {
        "pay_001", "pay_002", "pay_003",  # cluster A
        "pay_010", "pay_011",             # cluster B
        "pay_020", "pay_021", "pay_022",  # cluster C
    }
    assert expected.issubset(union_ids), (
        f"missing fraud rows: {expected - union_ids}"
    )
    legit = {f"pay_{i}" for i in range(100, 105)}
    assert not (union_ids & legit), (
        f"flagged legitimate rows: {union_ids & legit}"
    )
    # Per-pattern counts are populated and probes that hit are non-zero.
    assert result.per_pattern_counts["card_sharing"] >= 3
    assert result.per_pattern_counts["time_impossible"] >= 2
    assert result.per_pattern_counts["coord_cluster"] >= 3
    # And the session got every union path so the agent can cite them
    # without an extra read at report_completion.
    assert all(p in session.seen_refs for p in result.union_paths)


def test_probes_skip_when_not_triggered(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _build_fraud_world(workspace)
    from bitgn_contest_agent.adapter.ecom import EcomAdapter
    from bitgn_contest_agent.adapter.ecom_tracing import TracingEcomClient
    from bitgn_contest_agent.local.ecom_client import LocalEcomClient
    class _FakeSession:
        def __init__(self) -> None:
            self.seen_refs: set[str] = set()
    class _NullWriter:
        def append_prepass(self, **kw) -> None: pass

    adapter = EcomAdapter(
        runtime=TracingEcomClient(LocalEcomClient(workspace), writer=None),
        max_tool_result_bytes=64 * 1024,
    )
    result = run_fraud_probes(
        adapter=adapter,
        task_text="How many catalogue products are Work Jacket?",
        session=_FakeSession(),
        trace_writer=_NullWriter(),
    )
    assert result.triggered is False
    assert result.union_paths == []
    assert result.as_observation() is None
