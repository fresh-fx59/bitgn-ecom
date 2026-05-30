# In-trial Answer Re-derivation Verifier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the variance-prone ECOM task families reliably correct by giving the agent a deterministic, in-trial re-derivation of its own answer that BOUNCES the terminal back into the loop on disagreement — reaching a stable ~52/53 (t48 is a separate empirical probe).

**Architecture:** A pure-`/bin/sql`+Python re-derivation runs inside `AgentLoop._post_process_terminal`/`run()` after the model proposes a terminal. It recomputes the answer through an independent, convention-locked path; on disagreement it surfaces the diff as a `Verdict(ok=False)` reason so the existing retry path re-injects it and the adaptive model reconciles. It NEVER rewrites the answer and ABSTAINS on ambiguity (no-op = today). No LLM call (the aux Haiku route 400-blacks-out — see findings). Built family-by-family, each env-gated default-off and A/B'd independently.

**Tech Stack:** Python 3.12, pytest, sqlite3 (local oracle harness), the existing `bitgn_contest_agent` adapter (`/bin/sql` via `Req_Exec`), faithful `artifacts/ws_snapshots/*_real2` snapshots with ground-truth `expected_answer`.

**READ FIRST:** `docs/FINDINGS_RELIABILITY_2026-05-30.md`, `docs/SPEC_RELIABILITY_53.md`, and memories `project_variance_is_reliability_not_seed`, `project_ecom_53_path_per_family`, `project_ecom_provider_400_blackout`, `project_ecom_t48_genuine_wall`.

**Global rules:** TDD every change. Validate against the `*_real2` oracle locally (~$0) before any PROD run. Each phase is one commit-group and one PROD A/B. Run `pytest` (full suite) green before every commit. Do NOT enable a flag by default until its local + PROD A/B is clean.

---

## Phase 0 — Stop the verification layer from randomly vanishing

Provider 400s silently no-op the aux verifiers (3 of 5 runs = total judge blackout). This phase makes aux calls retry and makes blackouts visible, so benches are honest and the existing judge/validator stop being a variance source. Low risk, high diagnostic value.

### Task 0.1: Retry HTTP 400 / transient on the classifier route

**Files:**
- Modify: `src/bitgn_contest_agent/classifier.py` (the `classify` / `raw_completion` retry loop; `_get_openai_client` builds with `max_retries=0`)
- Reference: `src/bitgn_contest_agent/backend/openai_compat.py:109-117` (`_TRANSIENT_MESSAGE_SUBSTRINGS` already documents linkapi transient substrings)
- Test: `tests/test_classifier_retry.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classifier_retry.py
import openai
import pytest
from bitgn_contest_agent import classifier


class _Boom:
    """Raises a 400 N times then returns a valid stream chunk object."""
    def __init__(self, fails):
        self.fails = fails
        self.calls = 0
    def __call__(self, *a, **k):
        self.calls += 1
        if self.calls <= self.fails:
            raise openai.BadRequestError(
                "bad response status code 400", response=None, body=None
            )
        # minimal object the stream-concat path accepts; adjust to the
        # real shape used by _stream_call_content if needed
        raise AssertionError("unreached in this unit; see integration note")


def test_classify_retries_400(monkeypatch):
    # Assert the retry set now includes a 400/bad_response_status_code path:
    assert classifier._is_retryable_aux_error(
        openai.BadRequestError("bad response status code 400", response=None, body=None)
    ) is True
    assert classifier._is_retryable_aux_error(ValueError("nope")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_classifier_retry.py -v`
Expected: FAIL — `_is_retryable_aux_error` does not exist.

- [ ] **Step 3: Add the retry predicate + bounded retry**

Add to `classifier.py` (near the top, after imports):

```python
import openai as _openai

_AUX_RETRY_SUBSTRINGS = (
    "bad response status code 400",
    "bad_response_status_code",
    "upstream request failed",
    "upstream_connection_error",
    "concurrency limit exceeded",
)

def _is_retryable_aux_error(exc: Exception) -> bool:
    if isinstance(exc, _openai.APIError):
        msg = str(getattr(exc, "message", "") or exc).lower()
        return any(s in msg for s in _AUX_RETRY_SUBSTRINGS)
    return False
```

Then wrap the existing transport call in `_llm_call` / `_stream_call_content` with a bounded retry (max 3 attempts, no sleep needed in tests):

```python
def _call_with_retry(fn, *, attempts: int = 3):
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable_aux_error(exc):
                raise
            last = exc
    raise last
```

Use `_call_with_retry(lambda: client.chat.completions.create(**kwargs))` at the existing call site (`classifier.py:244`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_classifier_retry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/classifier.py tests/test_classifier_retry.py
git commit -m "fix(classifier): retry aux-route 400/transient errors (was max_retries=0, silently no-opped judge/validator)"
```

### Task 0.2: Log per-task verification coverage

**Files:**
- Modify: `src/bitgn_contest_agent/agent.py` (terminal handling in `run()`; emit one arch line)
- Test: covered by run-log grep, no unit test

- [ ] **Step 1: Add counters on the AgentLoop**

In `AgentLoop.__init__`, add `self._aux_attempted = 0; self._aux_succeeded = 0`. Increment `_aux_attempted` at each judge/validator/canonicalizer call site and `_aux_succeeded` when it returns non-None.

- [ ] **Step 2: Emit coverage at terminal**

In `run()` just before submitting the terminal:

```python
emit_arch(
    category=ArchCategory.VALIDATOR_T2, at_step=None,
    details=f"verification_coverage attempted={self._aux_attempted} succeeded={self._aux_succeeded}",
)
```

- [ ] **Step 3: Commit**

```bash
git add src/bitgn_contest_agent/agent.py
git commit -m "feat(agent): log per-task verification_coverage to flag provider-400 blackout windows"
```

> **Phase 0 acceptance:** run a local bench; grep `verification_coverage` — a run where `succeeded=0` across tasks is a provider-outage window and its bench must be discarded, not read as an agent regression.

---

## Phase 1 — count_per_store re-derivation (FLAGSHIP, highest-confidence win)

Proven: the convention is `available = COALESCE(qty,0)`, direction-dependent; the agent flips on INNER-vs-LEFT join + missing-row direction. Oracle: `t45_real2`→4, `t16_real2`→3.

### Task 1.1: The re-derivation module

**Files:**
- Create: `src/bitgn_contest_agent/count_rederive.py`
- Reuse: `sku_completer._csv_split`, `_unwrap_sql`, `_sql_quote`, `_detect_schema`, `resolve_store_id`; `refless_count_override._parse_threshold`
- Test: `tests/test_count_rederive.py`

- [ ] **Step 1: Write the failing oracle test**

```python
# tests/test_count_rederive.py
import json, sqlite3, types
from pathlib import Path
import pytest
from bitgn_contest_agent.count_rederive import rederive_count

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "artifacts" / "ws_snapshots"

def _build_db(snap_dir: Path) -> sqlite3.Connection:
    schema = (snap_dir / "sql_schema.sql").read_text()
    conn = sqlite3.connect(":memory:"); conn.executescript(schema)
    for jf in (snap_dir / "sql").glob("*.json"):
        rows = json.loads(jf.read_text())
        if not rows: continue
        cols = list(rows[0].keys())
        conn.executemany(
            f'INSERT INTO "{jf.stem}" ({",".join(chr(34)+c+chr(34) for c in cols)}) '
            f'VALUES ({",".join("?" for _ in cols)})',
            [tuple(r.get(c) for c in cols) for r in rows])
    conn.commit(); return conn

def _make_run_sql(conn):
    def run_sql(sql: str):
        try: cur = conn.execute(sql)
        except sqlite3.Error: return None
        rows = cur.fetchall()
        header = "|".join(d[0] for d in cur.description) if cur.description else ""
        return "\n".join([header] + ["|".join("" if c is None else str(c) for c in r) for r in rows])
    return run_sql

def _spec_from_meta(snap_dir):
    """Build a task_spec-like object from the snapshot metadata's parsed products.
    If the snapshot lacks a parsed spec, hand-build it from the instruction —
    see the metadata.json 'instruction' field and the per-product list."""
    meta = json.loads((snap_dir / "run_0" / "metadata.json").read_text())
    # The instruction enumerates products as 'the <name> from <brand> in the
    # <series> <CODE> <name> line that has <attr>, <attr>'. Parse or hardcode
    # the spec for the test (this is a TEST fixture, not production parsing).
    return meta  # replace with a SimpleNamespace spec; see Step 3 note

def test_t45_rederives_to_4():
    snap = SNAP / "t45_real2"
    conn = _build_db(snap); run_sql = _make_run_sql(conn)
    meta = json.loads((snap / "run_0" / "metadata.json").read_text())
    spec = _t45_spec()  # SimpleNamespace built from the known 6 products (see below)
    res = rederive_count(spec, run_sql, meta["instruction"])
    assert res.count == 4, res

def test_t16_rederives_to_3():
    snap = SNAP / "t16_real2"
    conn = _build_db(snap); run_sql = _make_run_sql(conn)
    meta = json.loads((snap / "run_0" / "metadata.json").read_text())
    spec = _t16_spec()
    res = rederive_count(spec, run_sql, meta["instruction"])
    assert res.count == 3, res

def _P(brand, model, series="", **attrs):
    o = types.SimpleNamespace()
    o.brand, o.model, o.series, o.name, o.attributes = brand, model, series, "", attrs
    return o

def _spec(store_descriptor, products):
    o = types.SimpleNamespace()
    o.kind, o.store_descriptor, o.products = "count_per_store", store_descriptor, products
    return o

def _t45_spec():
    # 6 products from the t45_real2 instruction (Wilten/Innsbruck, 'fewer than 4').
    # Fill brand/model-code/attrs from metadata['instruction']; the matcher must
    # resolve each to its variant. Confirm against sql/product_variant_properties.json.
    return _spec("Wilten PowerTool store in Innsbruck", [
        _P("Fiskars", "1CD-A3X", attributes={"power_source": "battery"}),
        _P("Mobil", "1ZE-TCR", attributes={"volume": "5000 ml", "viscosity": "15W-40"}),
        _P("Keter", "...code...", attributes={}),   # fill from instruction
        _P("EngelbertStrauss", "...", attributes={}),
        _P("Sika", "...", attributes={}),
        _P("Sonax", "...", attributes={}),
    ])

def _t16_spec():
    return _spec("Veveri PowerTool shop in Brno", [ ... ])  # 6 products, 'at least 1'
```

> **Note for the implementer:** fill the `_t45_spec()`/`_t16_spec()` product lists
> from `metadata.json["instruction"]` and verify attribute keys against
> `artifacts/ws_snapshots/t45_real2/sql/product_variant_properties.json`. These
> are TEST fixtures emulating what the LLM's `task_spec` would carry — production
> parsing is the model's job, already reliable per the findings.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_count_rederive.py -v`
Expected: FAIL — `count_rederive` module missing.

- [ ] **Step 3: Implement the module**

```python
# src/bitgn_contest_agent/count_rederive.py
"""Deterministic in-trial re-derivation of count_per_store answers.

Independent path: candidate set from product_variants(+properties) BEFORE
inventory; LEFT JOIN + COALESCE(...,0) (missing row = 0 available); threshold
applied direction-aware. Returns a count or ABSTAIN (None) + per-product
verdicts. The CALLER bounces the terminal on disagreement; this module never
rewrites and abstains on ambiguity. See docs/SPEC_RELIABILITY_53.md.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import os, re
from typing import Callable, Optional

from bitgn_contest_agent.sku_completer import _sql_quote, _detect_schema, resolve_store_id
from bitgn_contest_agent.refless_count_override import _parse_threshold


def is_enabled() -> bool:
    return os.environ.get("BITGN_USE_REDERIVE_COUNT", "").strip() == "1"


@dataclass
class RederiveResult:
    count: Optional[int]                  # None = ABSTAIN (no bounce)
    per_product: list = field(default_factory=list)
    reason: str = ""
    @property
    def abstained(self) -> bool:
        return self.count is None


def _split(line: str) -> list[str]:
    s = line.strip()
    return [c.strip() for c in (s.split("|") if "|" in s else s.split(","))]


def _rows(run_sql, sql: str) -> list[list[str]]:
    out = run_sql(sql)
    if out is None:
        return []
    res = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s or s.startswith("["):
            continue
        res.append(_split(s))
    return res


def _norm(v) -> str:
    return re.sub(r"[^a-z0-9.]", "", str(v).lower())


def _attr_matches(named_key: str, named_val: str, props: dict[str, tuple]) -> bool:
    """props: {prop_key_lower: (value_text_lower, value_number_or_'')}."""
    nk_lead = re.split(r"[_\s]", named_key.lower())[0]
    nv = _norm(named_val)
    m = re.search(r"(\d+(?:\.\d+)?)", named_val)
    named_num = float(m.group(1)) if m else None
    for pkey, (ptext, pnum) in props.items():
        if re.split(r"[_\s]", pkey)[0] != nk_lead and nk_lead not in pkey:
            continue
        if nv and (nv in _norm(ptext) or (_norm(ptext) and _norm(ptext) in nv)):
            return True
        if named_num is not None:
            for cand in (pnum, ptext):
                tm = re.search(r"(\d+(?:\.\d+)?)", str(cand))
                if tm and abs(float(tm.group(1)) - named_num) < 1e-9:
                    return True
    return False


def rederive_count(task_spec, run_sql: Callable[[str], Optional[str]], task_text: str) -> RederiveResult:
    if task_spec is None or getattr(task_spec, "kind", "") != "count_per_store":
        return RederiveResult(None, reason="not count_per_store")
    pred = _parse_threshold(task_text)
    if pred is None:
        return RederiveResult(None, reason="threshold/direction unparseable")
    products = list(getattr(task_spec, "products", None) or [])
    if not products:
        return RederiveResult(None, reason="no products in spec")
    store_id = resolve_store_id(getattr(task_spec, "store_descriptor", "") or "", run_sql)
    if store_id is None:
        return RederiveResult(None, reason="store not uniquely resolved")

    verdicts, count = [], 0
    for p in products:
        brand = getattr(p, "brand", "") or ""
        model = getattr(p, "model", "") or ""
        code = model.split()[-1] if model else ""
        if not brand or not code:
            return RederiveResult(None, reason=f"product missing brand/model: {brand} {model}")
        fam = _rows(run_sql,
            f"SELECT product_sku FROM product_variants "
            f"WHERE brand='{_sql_quote(brand)}' COLLATE NOCASE "
            f"AND (model LIKE '%{_sql_quote(code)}%' OR product_name LIKE '%{_sql_quote(code)}%');")
        skus = [r[0] for r in fam if r and r[0]]
        if not skus:
            return RederiveResult(None, reason=f"family empty for {brand} {code} (resolution gap)")
        attrs = dict(getattr(p, "attributes", {}) or {})
        matched = []
        for sku in skus:
            prows = _rows(run_sql,
                f"SELECT property_key, property_value_text, property_value_number "
                f"FROM product_variant_properties WHERE product_sku='{_sql_quote(sku)}';")
            props = {r[0].lower(): (r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "")
                     for r in prows if r and r[0]}
            if all(_attr_matches(k, v, props) for k, v in attrs.items()):
                matched.append(sku)
        if not matched:
            return RederiveResult(None, reason=f"no variant matches attrs for {brand} {code}")
        sides = set()
        for sku in matched:
            av = _rows(run_sql,
                f"SELECT COALESCE(available_today_quantity,0) FROM store_inventory "
                f"WHERE store_id='{_sql_quote(store_id)}' AND product_sku='{_sql_quote(sku)}';")
            avail = int(av[0][0]) if (av and av[0] and av[0][0].lstrip('-').isdigit()) else 0
            sides.add(bool(pred(avail)))
        if len(sides) != 1:
            return RederiveResult(None, reason=f"matched variants disagree on threshold side: {brand} {code}")
        q = sides == {True}
        verdicts.append((brand, code, q))
        count += 1 if q else 0
    return RederiveResult(count, per_product=verdicts)
```

- [ ] **Step 4: Run oracle tests; iterate the matcher until green**

Run: `pytest tests/test_count_rederive.py -v`
Expected: PASS (count==4, count==3). If a product abstains when it should resolve, inspect `artifacts/ws_snapshots/<t>/sql/product_variant_properties.json` and extend `_attr_matches` key-aliasing/unit-normalization — but keep abstain-on-genuine-ambiguity (never force a side).

- [ ] **Step 5: Add abstain tests (safety contract)**

```python
def test_abstains_on_unparseable_threshold():
    res = rederive_count(_spec("x", [_P("B","C")]), lambda s: "x\n", "no comparator here")
    assert res.count is None
```

Run: `pytest tests/test_count_rederive.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/count_rederive.py tests/test_count_rederive.py
git commit -m "feat(count_rederive): deterministic count_per_store re-derivation (t45->4, t16->3 on oracle); abstains on ambiguity"
```

### Task 1.2: Wire the bounce into the agent loop

**Files:**
- Modify: `src/bitgn_contest_agent/agent.py` (terminal handling in `run()`, after `_post_process_terminal` + `check_terminal`, near line 506-520; build `_run_sql` like line 1620-1629)
- Test: `tests/test_count_rederive_integration.py` (a thin test asserting the bounce reason is produced)

- [ ] **Step 1: Build the run_sql + re-derivation call**

In `run()`, after the terminal `verdict = self._validator.check_terminal(...)` and before submit, add (gated):

```python
from bitgn_contest_agent.adapter.ecom import Req_Exec
from bitgn_contest_agent import count_rederive

if (count_rederive.is_enabled()
        and isinstance(fn, ReportTaskCompletion)
        and fn.outcome == "OUTCOME_OK"
        and getattr(getattr(fn, "task_spec", None), "kind", "") == "count_per_store"
        and not getattr(self, "_count_rederive_bounced", False)):
    def _run_sql(sql: str):
        try:
            tr = self._adapter.dispatch(Req_Exec(tool="exec", path="/bin/sql", args=[], stdin=sql))
            return tr.content if tr.ok else None
        except Exception:
            return None
    rr = count_rederive.rederive_count(fn.task_spec, _run_sql,
                                       getattr(session, "task_text_en", "") or self._current_task_text)
    if rr.count is not None:
        m = re.search(r"-?\d+", fn.message or "")
        agent_n = int(m.group()) if m else None
        if agent_n is not None and agent_n != rr.count:
            self._count_rederive_bounced = True
            emit_arch(category=ArchCategory.VALIDATOR_T2, at_step=None,
                      details=f"count_rederive disagree: agent={agent_n} rederived={rr.count} verdicts={rr.per_product}")
            verdict = Verdict(ok=False, reasons=[
                f"COUNT RE-DERIVATION: you reported {agent_n} but an independent SQL "
                f"re-derivation yields {rr.count}. Convention: available_today = "
                f"COALESCE(store_inventory.available_today_quantity, 0); a SKU with NO "
                f"inventory row at the store has 0 available. Apply the threshold "
                f"DIRECTION-aware (for 'fewer than'/'no availability' a 0 QUALIFIES; for "
                f"'at least' a 0 does NOT). Per-product verdicts: {rr.per_product}. "
                f"Recompute each listed product and reconcile."])
```

Initialize `self._count_rederive_bounced = False` in `run()` setup. The existing retry path consumes `verdict.ok == False`.

- [ ] **Step 2: Verify bounce on a synthetic disagreement**

Write `tests/test_count_rederive_integration.py` that constructs a minimal `RederiveResult(count=4)` vs an agent message `<COUNT:3>` and asserts the reason string contains "re-derivation yields 4". (Unit-level; no full loop needed.)

Run: `pytest tests/test_count_rederive_integration.py -v` → PASS.

- [ ] **Step 3: Full suite + commit**

```bash
pytest -q
git add src/bitgn_contest_agent/agent.py tests/test_count_rederive_integration.py
git commit -m "feat(agent): bounce count_per_store terminal on independent SQL re-derivation disagreement (gated BITGN_USE_REDERIVE_COUNT)"
```

### Task 1.3: Local A/B on graded traces, then PROD A/B

- [ ] **Step 1:** Run the local bench with `BITGN_USE_REDERIVE_COUNT=1` vs off against the count snapshots; confirm t45/t16/t13/t14 reach the oracle and no count task regresses.
- [ ] **Step 2:** One DEV PROD run (`export BITGN_USE_REDERIVE_COUNT=1` + the proven 5-flag stack). Read scores via `scripts/fetch_run_scores.py`. Ship default-on only if the count family improves and nothing regresses.

> **Phase 1 acceptance:** t45/t16 reproduce the oracle locally; the bounce reason fires only on disagreement; abstains leave the answer untouched; no count task regresses in the PROD A/B.

---

## Phase 2 — catalogue_count addendum-filter reconciliation

**Module:** `src/bitgn_contest_agent/catalogue_count_rederive.py` (new). **Oracle:** t11/t49 family; `artifacts/ws_snapshots/t11_en/.../catalogue-count-chargers-bulbs-graz-2024-07-17.md`.

**Logic:** (1) locate + read the matched addendum doc BODY; (2) detect availability/store predicate tokens (`available_today`, `available today`, `open ... store`, `in <City>`, `>0`, or any of `available`/`in stock`/`today`); (3) run BOTH `A = SELECT COUNT(DISTINCT product_sku) FROM product_variants WHERE product_kind_id=K` and `B = A JOIN store_inventory si ... JOIN stores s ... WHERE s.city=<City> AND s.is_open=1 AND si.available_today_quantity>0`; (4) if the body carries the predicate, the answer MUST equal `B` — bounce if the agent's integer != B; if no predicate, accept either. Abstain if the addendum can't be located/read. **Tasks mirror Phase 1's TDD rhythm** (write oracle test asserting the joined count, implement, wire the bounce gated `BITGN_USE_REDERIVE_CATCOUNT`, A/B). Acceptance: work-gloves world bounces 26→joined; helmets world bounces 14→joined; a no-filter addendum accepts the raw count.

## Phase 3 — quote/pasted-list (t47) guard

**Module:** `src/bitgn_contest_agent/quote_rederive.py` (new). **Oracle:** `t47_real2` (4 SKUs: PWR-1ALYVIXX, PNT-1CJQ9WWP, FST-1GZ71C60, CLN-3066WM0S; actor emp_016 → store_graz_lend).

**Logic:** (1) **all-rows-empty guard** — if the agent's answer marks every pasted row unmatched, re-resolve each row's code via `model LIKE '%CODE%' OR product_name LIKE '%CODE%'` then exact attribute conjunction (reuse `quote_ref_completer.resolve_matched_refs` / `_attr_matches`); if any row now matches, bounce with the resolved SKUs (a quote yielding zero matches for every row is a near-certain bug). (2) **store triple-check** — assert SQL `employee_accounts.store_id` == `/proc/employees/<actor>.json` store_id == `/bin/id` actor; flag on mismatch (defends the documented wrong-store prime failure). Tasks mirror Phase 1; gated `BITGN_USE_REDERIVE_QUOTE`. Acceptance: the all-empty t47 trace bounces to the 4 real SKUs; a correct multi-match trace is a no-op.

## Phase 4 — yes_no_sku: remove the harmful completer, add SQL self-verify

**Files:** `src/bitgn_contest_agent/agent.py` (the yes_no_sku post-pass ~1771-1865), `src/bitgn_contest_agent/sku_completer.py:917` (`complete_yes_no_sku_refs`). **Oracle:** `t01_real2` (expected `<YES>`, required_refs `['/proc/catalog/STO-2R84BSHQ.json']`).

**Logic:** the agent's verdict + SKU pick is reliably correct; the failure is the completer flooding never-read family refs (validator R1 reject) and `sku_verifier` unable to strip numeric-renamed attrs. (1) **Disable** the family-flood union for yes_no_sku. (2) **SQL self-verify**: `SELECT product_sku FROM product_variants pv JOIN product_variant_properties pp USING(product_sku) WHERE pv.brand=? AND (pv.model LIKE '%code%' OR pv.product_name LIKE '%code%') GROUP BY pv.product_sku HAVING <all named attrs matched, text-or-number>`; set `grounding_refs` to exactly the confirmed matching SKU(s) **that are already in `session.seen_refs`**; if the SQL singleton disagrees with the agent's verdict, ABSTAIN (leave the agent's ref untouched — never flip the verdict). Tasks mirror Phase 1; gated `BITGN_USE_YESNO_SELFVERIFY` (and a kill switch for the old completer). Acceptance: t01 across the documented failing runs ends with exactly the one read+confirmed SKU and no never-read ref → no R1 reject; passing worlds unchanged.

## Phase 5 — fraud closure in-trial (t40/t39)

**Files:** `src/bitgn_contest_agent/agent.py` (terminal block), reuse `src/bitgn_contest_agent/fraud_component_completer.py:103-147` (already correct). **Oracle:** `t40_real2`/`t40_real3` → 26-row component.

**Logic:** gated on the fraud-incident task text (`looks_like_sql_fraud_task`: `fraud`-word + `payment`/`archiv`-word, NOT `.tsv`), build `_run_sql`, call `resolve_fraud_component(run_sql, task_text)`; if it returns a component that is a STRICT SUPERSET of the agent's cited archived-payment refs, ADD the missing record_paths (add-only) OR bounce asking the agent to expand to the closure. Keep the dominance gate (top ≥15 AND ≥2× runner-up) so non-fraud worlds abstain. Tasks: write an oracle test asserting `resolve_fraud_component` returns 26 on `t40_real2`/`real3` (test already exists — `tests/test_fraud_component_completer.py`; extend it to assert the in-trial wiring), then wire gated `BITGN_USE_FRAUD_COMPONENT_COMPLETER=1` in-trial, A/B. Acceptance: the 23 device-only 0.927 PROD runs move toward 1.0; the gate keeps every non-fraud task a no-op (the same anomalous cluster exists in every background DB — gate is the precision boundary).

## Phase 6 — selection (t50 no-fall-through, t26 reconciliation)

**Module:** `src/bitgn_contest_agent/basket_select_rederive.py` (new). **Oracle:** `t26_real3` → basket_053, OUTCOME_OK; t50 ground truth STATUS.md:88 (newest basket, no fall-through).

**t50 logic:** select the UNIQUE newest active basket in one query (`ORDER BY basket_created_at DESC LIMIT 1`, no inventory); gate THAT basket alone; assert the basket id passed to `/bin/checkout` (or named in the OK message) == the unique-newest; if its inventory gate fails, the terminal MUST be `OUTCOME_NONE_UNSUPPORTED` with no `/bin/checkout` in the trace — bounce otherwise. Surface a flag (not auto-resolve) on a `created_at` tie.

**t26 logic:** one reconciliation aggregate (group active baskets for the customer, scoped to actor store when "from my store"; compute `all_lines_ok` via `MIN(requested_quantity <= COALESCE(available_today_quantity,-1))` and `subtotal`); assert the chosen basket is the FIRST `all_lines_ok=1` row by recency; recompute the discount cap (1–10% iff subtotal≥15000 else 1–5%); require `grounding_refs` ⊇ {`/docs/security.md`, `/docs/discounts.md`, `/docs/checkout.md`, basket path, customer record} before an OK; bounce on over-refusal (a checkoutable basket exists) or missing ref. Tasks mirror Phase 1; gated `BITGN_USE_REDERIVE_BASKET`. Acceptance: t50 fall-through traces bounce to NONE_UNSUPPORTED; t26 over-refusal traces bounce to OK with the full ref set.

## Phase 7 — t48 probe (PROD-only; NOT promised solvable)

t48 has **no in-workspace fraud rule** and was never ground-truthed locally (PROD max ≈0.305). Two independent sub-tasks:

- **7a (reliable):** fix TSV read-truncation — enumerate `/archive/...tsv` rows to EOF (line-count first via `exec`/`stat`, then slice exhaustively) and assert reconstructed row count == file lines − 1 before computing. This alone removes the "computed on partial data" failures. Worth shipping regardless.
- **7b (the wall — empirical):** implement a disciplined **cross-customer device/method-ring** fraud set (device or method fingerprint shared by ≥2 DISTINCT customers → ring; drop pure same-customer time-impossible bursts), output the exact `EUR %d.%02d` total + per-row refs. **Validate ONLY on PROD**: run t48 with this strategy, read the grader verdict via `scripts/fetch_trial_detail.py`. If the score jumps materially → t48 is solvable and ship it. If not → t48 is the author ceiling and 53/53 pass@1.0 is genuinely unreachable; record that as the honest answer. Do NOT re-enable the old export hint (regressed 0.43→0.0).

---

## Self-review

- **Spec coverage:** Phases 0–7 map 1:1 to the SPEC component table (Phase N ↔ component N). Every failing family in `docs/FINDINGS_RELIABILITY_2026-05-30.md` has a phase.
- **Placeholder scan:** Phase 1 + Phase 0 contain complete code. Phases 2–6 give the exact module name, oracle snapshot, SQL logic, gate flag, integration point, and acceptance test — they intentionally reuse Phase 1's fully-worked TDD rhythm and the `_run_sql`/`Verdict(ok=False)` wiring rather than repeating it; the implementer authors each family's oracle test against the named snapshot first (TDD). The `_t45_spec()`/`_t16_spec()` fixtures are flagged to be filled from `metadata.json` + `product_variant_properties.json` (test fixtures, not production code).
- **Type consistency:** `rederive_count(task_spec, run_sql, task_text) -> RederiveResult`; `RederiveResult.count: Optional[int]`; `Verdict(ok: bool, reasons: List[str])` (matches `validator.py:45-48`); `_run_sql(sql:str)->str|None` (matches `agent.py:1620-1629`); `Req_Exec(tool,path,args,stdin)` (matches the existing call). The fraud reuse calls the existing `resolve_fraud_component(run_sql, task_text)->list[str]` (matches `fraud_component_completer.py:103`).

---

## Execution handoff

After Phase 1 lands and A/Bs clean, proceed Phase 2→6 in order (each its own A/B), then Phase 7 as the empirical 53/53 probe. Stop after Phase 1's PROD A/B to reassess the band before investing in the rest.
</content>
