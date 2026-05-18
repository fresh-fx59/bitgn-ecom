"""Fraud-detection preflight — auto-runs the SQL pattern probes and
surfaces the union as a pre-pass bootstrap message.

Why this exists: the v0.1.49 prompt rule documents seven fraud-pattern
families and tells the agent to UNION across them. v149/v151 bench
evidence shows the agent CAN execute the rule but variance is high:
the same prompt produced t40 recall 12/22 (v149) and 22/22 (v151)
on different content draws. The variance source is the agent's
decision of when to stop probing — sometimes it cherry-picks the
single most-evident cluster.

This preflight removes that decision: when the task is fraud-shaped,
run all five strong probes deterministically, UNION the matched
payment ids, READ each payment file (so refs are grounded per the
prompt's rule A), and surface the union to the agent as a pre-pass
observation. The agent's job collapses from "investigate + decide"
to "verify + cite".

Design constraints (per AGENTS.md):
  * Generic — no PROD entity IDs (customer_*, pay_*), no brand names,
    no task-id matchers, no wording copied verbatim from a single
    PROD task instance.
  * Lexical trigger requires BOTH a fraud-word AND a payment-class
    word in the task text. Catches Rinat's three observed phrasings
    ("known fraud hit in the older archived payment history",
    "fraud review says one hit is present in the archived payments",
    "confirmed fraud incident in archived payment history") and the
    natural family of paraphrases without latching onto any single
    one verbatim.
  * SQL probes reference ONLY schema column names that exist by
    design in the ECOM `payments` / `stores` / `customers` tables.
    If the workspace lacks the columns (a non-fraud world), each
    probe degrades gracefully — exceptions are caught, the empty
    union is returned, and the agent falls back to the prompt rule.
  * The prompt-side rule in `prompts.py` is KEPT as a fallback for
    rephrasings the lexical trigger misses; the preflight is an
    additive precision/recall boost, not a replacement.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from bitgn_contest_agent.schemas import Req_Exec, Req_Read

_LOG = logging.getLogger(__name__)


# Lexical trigger: fraud-word + payment-class word in either order.
# Generic across rephrasings; requires both axes to fire so a task
# that only mentions "payment" (e.g. a 3DS recovery question) doesn't
# trip the preflight.
_FRAUD_WORD = r"\b(?:fraud(?:s|ulent)?|fraudulence)\b"
_PAYMENT_WORD = (
    r"\b(?:payment|payments|archived|archives|transaction|transactions|"
    r"incident|incidents|hit|hits|history|histories)\b"
)
_FRAUD_TASK_RE = re.compile(
    rf"(?:{_FRAUD_WORD}.*?{_PAYMENT_WORD})|(?:{_PAYMENT_WORD}.*?{_FRAUD_WORD})",
    re.IGNORECASE | re.DOTALL,
)


def is_fraud_task(task_text: str) -> bool:
    """True iff the task text combines a fraud-word with a payment-class
    word. Both must appear; either order; case-insensitive."""
    if not task_text:
        return False
    return bool(_FRAUD_TASK_RE.search(task_text))


# ── SQL probes ────────────────────────────────────────────────────────
#
# Each probe is a self-contained SELECT that returns the columns
# (id, pattern) — pattern is a literal string label so the union
# preserves which probe fired the row. All probes filter by
# `basket_archived = 1` so they target only archived payment history.
#
# Probes operate on schema column names only. If a column is missing
# (non-fraud world, alternate schema), the probe raises and is dropped
# by the caller — the union just shrinks; the agent still proceeds.

# Pattern (a): same payment_method_fingerprint across multiple
# customer_ids. "Stolen card" archetype. HAVING > 1 distinct customers
# is the discriminator vs legitimate single-customer card reuse.
_PROBE_CARD_SHARING = """
SELECT DISTINCT p.id, 'card_shared_across_customers' AS pattern
FROM payments p
WHERE p.basket_archived = 1
  AND p.payment_method_fingerprint IN (
    SELECT payment_method_fingerprint FROM payments
    WHERE basket_archived = 1
    GROUP BY payment_method_fingerprint
    HAVING COUNT(DISTINCT customer_id) > 1
  )
"""

# Pattern (b): same device_fingerprint across multiple customer_ids.
# "Stolen/shared device" archetype.
_PROBE_DEVICE_SHARING = """
SELECT DISTINCT p.id, 'device_shared_across_customers' AS pattern
FROM payments p
WHERE p.basket_archived = 1
  AND p.device_fingerprint IN (
    SELECT device_fingerprint FROM payments
    WHERE basket_archived = 1
    GROUP BY device_fingerprint
    HAVING COUNT(DISTINCT customer_id) > 1
  )
"""

# Pattern (d): one customer, consecutive payments at different stores
# inside an interval too short for plausible travel (3600s default).
# Both rows of every triggering pair are included.
_PROBE_TIME_IMPOSSIBLE = """
WITH pairs AS (
  SELECT a.id AS a_id, b.id AS b_id
  FROM payments a JOIN payments b
    ON a.customer_id = b.customer_id
   AND a.basket_archived = 1 AND b.basket_archived = 1
   AND a.store_id <> b.store_id
   AND a.created_at < b.created_at
   AND (strftime('%s', b.created_at) - strftime('%s', a.created_at)) < 3600
)
SELECT DISTINCT id, 'time_impossible_for_one_customer' AS pattern FROM (
  SELECT a_id AS id FROM pairs
  UNION
  SELECT b_id AS id FROM pairs
)
"""

# Pattern (e): observed_lat / observed_lon far from the row's
# store_id lat/lon. ~0.25 degrees squared ≈ ~50km radius; coordinate
# spoofing typically produces deltas well in excess of this.
_PROBE_OBSERVED_VS_STORE = """
SELECT DISTINCT p.id, 'observed_far_from_store' AS pattern
FROM payments p JOIN stores s ON s.id = p.store_id
WHERE p.basket_archived = 1
  AND ((p.observed_lat - s.lat)*(p.observed_lat - s.lat)
     + (p.observed_lon - s.lon)*(p.observed_lon - s.lon)) > 0.25
"""

# Pattern (g): identical observed coordinate (rounded to ~110 m) shared
# across multiple customer_ids AND that coordinate is NOT near any
# store's actual lat/lon. Session-replay / coordinate-spoofing
# archetype. The store-coord exclusion is what discriminates this
# pattern from "many customers transacting from the same store"
# (legitimate clustering at a store location). The exclusion uses
# 2-decimal rounding (~1.1 km tolerance) so a coord cluster that
# happens to coincide with a store within typical store-radius
# noise is treated as legitimate, while a sharply-shared spoofed
# coord far from every store still fires.
_PROBE_COORD_CLUSTER = """
SELECT DISTINCT p.id, 'observed_coord_cluster' AS pattern
FROM payments p
WHERE p.basket_archived = 1
  AND (ROUND(p.observed_lat, 3) || ',' || ROUND(p.observed_lon, 3)) IN (
    SELECT (ROUND(observed_lat, 3) || ',' || ROUND(observed_lon, 3))
    FROM payments
    WHERE basket_archived = 1
    GROUP BY ROUND(observed_lat, 3), ROUND(observed_lon, 3)
    HAVING COUNT(DISTINCT customer_id) > 1
  )
  AND NOT EXISTS (
    SELECT 1 FROM stores s
    WHERE ROUND(s.lat, 2) = ROUND(p.observed_lat, 2)
      AND ROUND(s.lon, 2) = ROUND(p.observed_lon, 2)
  )
"""

_PROBES: list[tuple[str, str]] = [
    ("card_sharing", _PROBE_CARD_SHARING),
    ("device_sharing", _PROBE_DEVICE_SHARING),
    ("time_impossible", _PROBE_TIME_IMPOSSIBLE),
    ("observed_vs_store", _PROBE_OBSERVED_VS_STORE),
    ("coord_cluster", _PROBE_COORD_CLUSTER),
]


# ── public dataclass + entry point ────────────────────────────────────


@dataclass
class FraudPreflightResult:
    """Outcome of a fraud-preflight pass.

    `union_paths` is the de-duplicated list of /proc/payments/<id>.json
    paths the union of all probes matched, each one already read so
    refs are grounded per prompt rule A. `per_pattern_counts` lets
    the agent (and a human triaging the trace) see how each probe
    contributed.
    """
    ran: bool
    triggered: bool
    union_paths: list[str] = field(default_factory=list)
    per_pattern_counts: dict[str, int] = field(default_factory=dict)
    failed_probes: list[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None

    def as_observation(self) -> Optional[str]:
        """Render the result as a pre-pass bootstrap message. Returns
        None when the preflight did not fire (so the agent loop knows
        to inject nothing)."""
        if not self.triggered:
            return None
        if not self.union_paths:
            counts_blob = ", ".join(
                f"{p}={n}" for p, n in self.per_pattern_counts.items()
            ) or "no patterns ran"
            return (
                "PRE-PASS fraud probes — already executed, do NOT re-run.\n"
                f"The five fraud-pattern probes returned ZERO archived "
                f"payment rows ({counts_blob}). The world may use a "
                "non-archived schema or use patterns outside (a)..(g) "
                "in /AGENTS.MD. Fall back to the prompt rule and "
                "investigate manually."
            )
        counts_blob = ", ".join(
            f"{p}={n}" for p, n in self.per_pattern_counts.items() if n > 0
        )
        path_list = "\n".join(f"  {p}" for p in self.union_paths)
        return (
            "PRE-PASS fraud probes — already executed, do NOT re-run.\n"
            "Five SQL pattern probes against payments WHERE "
            "basket_archived=1 produced this UNION of candidate fraud "
            f"rows (hit counts: {counts_blob}). Every path below has "
            "been READ during the preflight so it is grounded for "
            f"`grounding_refs`:\n{path_list}\n"
            "When you report_completion, cite EVERY path above — this "
            "IS the union answer. Do not drop members to reduce "
            "perceived precision; the probes already filter on "
            "cross-customer reuse or large geographic delta, which "
            "is the precision boundary."
        )


def _extract_stdout(content: str) -> str:
    """Pull the `stdout` field out of an exec ToolResult's content.

    The ECOM adapter wraps `/bin/*` results as JSON like
    ``{"stdout": "...", "exit_code": 0}``. Probes only care about
    stdout. Bare-CSV input (already extracted) is returned unchanged
    so this is idempotent for tests and future format changes.
    """
    if not content:
        return ""
    s = content.lstrip()
    if not s.startswith("{"):
        return content
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        return content
    return obj.get("stdout", "") or ""


def _split_sql_csv(stdout_blob: str) -> list[list[str]]:
    """Tolerant CSV-style row parser for /bin/sql output.

    /bin/sql emits CSV with a header row; fields may be quoted with
    embedded commas. We only need the first column (payment id) for
    every probe, so this parses each line conservatively, stripping
    surrounding quotes.

    Accepts either raw CSV or an exec ToolResult content blob
    ({"stdout": "...", ...}); _extract_stdout normalises both."""
    stdout = _extract_stdout(stdout_blob)
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return []
    # Drop header row.
    rows = []
    for line in lines[1:]:
        # Simple CSV split — values don't contain commas in our schema
        # (payment ids and pattern labels are alphanumeric + underscores).
        parts = [p.strip().strip('"').strip("'") for p in line.split(",")]
        rows.append(parts)
    return rows


def run_fraud_probes(
    *,
    adapter: Any,
    task_text: str,
    session: Any,
    trace_writer: Any,
    max_paths: int = 500,
) -> FraudPreflightResult:
    """Run the fraud-pattern preflight when `task_text` is fraud-shaped.

    Each probe is dispatched via the adapter's `/bin/sql` exec. Probe
    exceptions are caught and the probe is dropped from the union;
    the rest still run. Each unique payment id matched by any probe
    is then READ via `/proc/payments/<id>.json` so the path appears
    in `session.seen_refs` and can be cited verbatim by the agent at
    report_completion.

    `max_paths` caps the union size to bound prepass token cost.
    Worlds with thousands of fraud rows are unlikely (Rinat's worlds
    seed ~10-30 fraud rows per task), but the cap prevents a runaway
    if a probe over-fires.

    Returns a FraudPreflightResult with `triggered=False` when the
    task text does not match the fraud trigger.
    """
    if not is_fraud_task(task_text):
        return FraudPreflightResult(ran=False, triggered=False)

    from bitgn_contest_agent.adapter.ecom_tracing import ecom_origin

    counts: dict[str, int] = {}
    failed: list[str] = []
    union: set[str] = set()

    with ecom_origin("prepass"):
        for probe_name, sql in _PROBES:
            try:
                req = Req_Exec(tool="exec", path="/bin/sql", args=[], stdin=sql)
                result = adapter.dispatch(req)
            except Exception as exc:  # noqa: BLE001 — probe failure is best-effort
                _LOG.debug("fraud probe %s raised: %s", probe_name, exc)
                failed.append(probe_name)
                counts[probe_name] = 0
                continue
            if not result.ok:
                _LOG.debug("fraud probe %s returned not-ok: %s", probe_name, result.error)
                failed.append(probe_name)
                counts[probe_name] = 0
                continue
            rows = _split_sql_csv(result.content or "")
            hit = 0
            for row in rows:
                if not row:
                    continue
                pid = row[0]
                if not pid:
                    continue
                if pid not in union:
                    union.add(pid)
                hit += 1
            counts[probe_name] = hit
            trace_writer.append_prepass(
                cmd=f"fraud_probe_{probe_name}",
                ok=True,
                bytes=result.bytes,
                wall_ms=result.wall_ms,
                error=None,
                error_code=None,
                schema_roots=None,
            )

    # Cap and order — sorted ids give deterministic output for tests
    # and stable trace comparisons across runs.
    ordered_ids = sorted(union)[:max_paths]

    # Read each /proc/payments/<id>.json so the ref becomes grounded.
    # The agent's prompt rule A says SQL discovery alone is NOT
    # grounding; we materialise the file read here so the agent can
    # cite without an extra round trip.
    paths: list[str] = []
    with ecom_origin("prepass"):
        for pid in ordered_ids:
            path = f"/proc/payments/{pid}.json"
            try:
                req = Req_Read(tool="read", path=path)
                rresult = adapter.dispatch(req)
            except Exception:  # noqa: BLE001
                continue
            if rresult.ok:
                paths.append(path)
                for ref in (rresult.refs or []):
                    session.seen_refs.add(ref)
                # Note: refs is what the adapter surfaces; we also add the
                # canonical path so the agent's grounding check passes.
                session.seen_refs.add(path)

    return FraudPreflightResult(
        ran=True,
        triggered=True,
        union_paths=paths,
        per_pattern_counts=counts,
        failed_probes=failed,
    )
