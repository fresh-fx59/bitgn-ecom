# Preflight Generalization + UNKNOWN Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (A) Stop cherry-picking frontmatter fields in routed preflights — emit the full parsed dict so new fields propagate without code churn. Fixes the t008 service_line failure. (B) Add a `preflight_unknown` that fires when the router returns UNKNOWN, so tasks without a bound skill get a structured investigation scaffold instead of cold-starting exploration.

**Architecture:** Phase A touches two existing preflight modules (`finance.py`, `inbox.py`). Phase B adds one new preflight (`preflight/unknown.py`), wires it into `routed_preflight.py` (replacing the `"no_skill"` early-return with a dispatch), and extends agent plumbing to pass the LLM backend through so the new tool can classify task intent.

**Tech Stack:** Python, Pydantic (request/response schemas), pytest, gpt-5.3-codex via cliproxyapi (LLM backend), existing `pcm_origin` contextvar (for trace attribution).

---

## File Structure

**Phase A — Preflight generalization (2 tasks):**
- Modify: `src/bitgn_contest_agent/preflight/finance.py:56-67` and `:108-114` — emit raw frontmatter dict per invoice, + all-invoices fallback on no-entity-match
- Modify: `src/bitgn_contest_agent/preflight/inbox.py:113-120` and `:185-191` — add `frontmatter` key per inbox item
- Update: `tests/preflight/test_finance.py`, `tests/preflight/test_inbox.py` — adjust assertions to match new record shape
- No new files

**Phase B — UNKNOWN preflight (4 tasks):**
- Create: `src/bitgn_contest_agent/preflight/unknown.py` — preflight_unknown implementation
- Modify: `src/bitgn_contest_agent/schemas.py` — add `Req_PreflightUnknown` + `Rsp_PreflightUnknown` Pydantic schemas
- Modify: `src/bitgn_contest_agent/routed_preflight.py` — add `_build_unknown` builder, register in `_BUILDERS`, change the `no_skill` branch to dispatch it
- Modify: `src/bitgn_contest_agent/agent.py:715-725` — pass `backend` to `dispatch_routed_preflight` (preflight_unknown needs LLM)
- Create: `tests/preflight/test_unknown.py` — unit tests with mocked backend

---

## Task A1: Finance preflight — emit full frontmatter + all-invoices fallback

**Files:**
- Modify: `src/bitgn_contest_agent/preflight/finance.py` (two call sites: `run_finance_from_fs` around L56-67, `run_preflight_finance` around L108-119)
- Test: `tests/preflight/test_finance.py`

**Rationale:** t008 failure — agent asked about `service_line` but preflight surfaced only `vendor/date/total/line_items`. Generalization: never cherry-pick; emit full `frontmatter` dict. On no-entity-match, emit every invoice so service-line-style queries have data to work with.

- [ ] **Step 1: Write the failing tests**

Append to `tests/preflight/test_finance.py`:

```python
def test_finance_file_record_includes_full_frontmatter():
    """Every invoice record surfaces the full parsed frontmatter dict,
    not a cherry-picked subset. Regression guard for t008 service_line."""
    out = run_finance_from_fs(
        root=FIXTURE,
        finance_roots=["50_finance/purchases"],
        entities_root="20_entities",
        query="Juniper Systems",
    )
    f = out["finance_files"][0]
    assert "frontmatter" in f
    assert isinstance(f["frontmatter"], dict)
    # Should contain ALL frontmatter keys, not just 4 hardcoded ones.
    assert "vendor" in f["frontmatter"]


def test_finance_no_entity_match_returns_all_invoices():
    """When the query doesn't match any entity alias, the preflight
    returns all invoices so the agent can filter (e.g., by service_line)
    in-prompt rather than doing cold-start tree/search."""
    out = run_finance_from_fs(
        root=FIXTURE,
        finance_roots=["50_finance/purchases"],
        entities_root="20_entities",
        query="staff follow-up support",  # service line, not entity
    )
    assert out["canonical_entity"] is None
    # Previously this returned []; now it returns every invoice.
    assert len(out["finance_files"]) > 0
    # And every record carries full frontmatter.
    for f in out["finance_files"]:
        assert "frontmatter" in f and isinstance(f["frontmatter"], dict)
```

Also update the pre-existing `test_finance_returns_file_metadata` — the top-level record no longer has `"vendor"` directly:

```python
def test_finance_returns_file_metadata():
    out = run_finance_from_fs(
        root=FIXTURE,
        finance_roots=["50_finance/purchases"],
        entities_root="20_entities",
        query="Juniper Systems",
    )
    f = out["finance_files"][0]
    assert "path" in f
    assert "frontmatter" in f
    assert f["frontmatter"].get("vendor")  # vendor moves inside frontmatter
```

Delete `test_finance_empty_on_unknown_query` — the semantics it asserted (empty list) are now intentionally inverted.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/preflight/test_finance.py -v`
Expected: FAIL — `"frontmatter"` not in record; empty list returned on service_line query.

- [ ] **Step 3: Implement the generalization**

In `src/bitgn_contest_agent/preflight/finance.py`, replace both call-site dicts:

At `run_finance_from_fs` (L32-67), change the no-match fallback to emit all invoices, and change `files_meta` record shape:

```python
def run_finance_from_fs(
    root: Path,
    finance_roots: list[str],
    entities_root: str,
    query: str,
) -> dict[str, Any]:
    root = Path(root)
    entities = _load_entities(root / entities_root)
    match = _match_entity(query, entities)
    finance_dirs = [root / fr for fr in finance_roots]
    if match:
        bill_paths = _bills_for_entity(match, finance_dirs)
    else:
        # No entity match — emit ALL invoices so service-line-style
        # queries (e.g. "staff follow-up support") have data to filter.
        bill_paths = []
        for d in finance_dirs:
            if not d.exists():
                continue
            for f in sorted(d.rglob("*.md")):
                bill_paths.append(str(f))
    files_meta = []
    for bp in bill_paths:
        try:
            text = Path(bp).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        files_meta.append({"path": bp, "frontmatter": fm})
    return {
        "canonical_entity": match["canonical"] if match else None,
        "aliases_matched": [match["matched_alias"]] if match else [],
        "finance_files": files_meta,
    }
```

At `run_preflight_finance` (L94-119), apply the same change:

```python
        files_meta = []
        if match:
            # Filter by vendor-alias match (existing behaviour).
            for froot in req.finance_roots:
                try:
                    fresp = client.list(pcm_pb2.ListRequest(name=froot))
                except Exception:
                    continue
                for fe in fresp.entries:
                    if not fe.name.endswith(".md"):
                        continue
                    fp = f"{froot}/{fe.name}"
                    fr = client.read(pcm_pb2.ReadRequest(path=fp))
                    ffm = _parse_frontmatter(fr.content)
                    vendor = normalize_name(ffm.get("vendor", ""))
                    if vendor and any(a in vendor or vendor in a for a in alias_norms if a):
                        files_meta.append({"path": fp, "frontmatter": ffm})
        else:
            # No entity match — emit every invoice so service-line-style
            # queries have data.
            for froot in req.finance_roots:
                try:
                    fresp = client.list(pcm_pb2.ListRequest(name=froot))
                except Exception:
                    continue
                for fe in sorted(fresp.entries, key=lambda x: x.name):
                    if not fe.name.endswith(".md"):
                        continue
                    fp = f"{froot}/{fe.name}"
                    fr = client.read(pcm_pb2.ReadRequest(path=fp))
                    ffm = _parse_frontmatter(fr.content)
                    files_meta.append({"path": fp, "frontmatter": ffm})
```

Also update the module docstring (L1-4) — it now says "returns matching purchase/invoice files with extracted metadata (vendor, date, total, line_items)"; change the parenthetical to "with full parsed frontmatter".

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/preflight/test_finance.py -v`
Expected: all tests pass.

Also run the full preflight test suite to catch unintended regressions:
Run: `uv run pytest tests/preflight/ -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/finance.py tests/preflight/test_finance.py
git commit -m "$(cat <<'EOF'
feat(preflight): finance emits full frontmatter + all-invoices fallback

- Per-invoice record: {path, frontmatter: <full dict>} instead of
  cherry-picked {vendor, date, total, line_items}. New frontmatter
  fields (e.g. service_line) propagate without code changes.
- No-entity-match branch now emits every invoice so service-line
  queries ("staff follow-up support") have structured data to filter
  rather than cold-starting tree/search.

Regression guard for t008 (PROD 2026-04-16): agent answered 330 vs
expected 490 because it substring-matched filenames and never saw
service_line field.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task A2: Inbox preflight — add full frontmatter per item

**Files:**
- Modify: `src/bitgn_contest_agent/preflight/inbox.py` (two call sites: `enumerate_inbox_from_fs` around L113-120, `run_preflight_inbox` around L185-191)
- Test: `tests/preflight/test_inbox.py`

**Rationale:** Same generalization pattern. Inbox items carry frontmatter beyond just `inbox_type` (priority, sender, due, status). The agent shouldn't have to re-read the file to see them.

- [ ] **Step 1: Write the failing test**

Append to `tests/preflight/test_inbox.py`:

```python
def test_inbox_item_includes_full_frontmatter():
    """Every inbox item surfaces the full parsed frontmatter dict so
    fields beyond inbox_type (priority, sender, due, status, ...) are
    visible without re-reading the file."""
    root = FIXTURE  # existing fixture path
    items = enumerate_inbox_from_fs(
        root=root,
        inbox_root="00_inbox",
        entities_root="20_entities",
        finance_roots=["50_finance/purchases"],
    )
    assert len(items) >= 1
    for it in items:
        assert "frontmatter" in it
        assert isinstance(it["frontmatter"], dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/preflight/test_inbox.py::test_inbox_item_includes_full_frontmatter -v`
Expected: FAIL — `"frontmatter"` not in item dict.

- [ ] **Step 3: Add `frontmatter` key to inbox item records**

In `src/bitgn_contest_agent/preflight/inbox.py`, both in `enumerate_inbox_from_fs` (around L113-120) and `run_preflight_inbox` (around L185-191), add one line to the item dict:

```python
        item = {
            "path": str(f),
            "task_type": fm.get("inbox_type") or fm.get("inbox_kind") or "",
            "entity_ref": match["matched_alias"] if match else None,
            "entity_canonical": match["canonical"] if match else None,
            "related_finance_files": _bills_for_entity(match, finance_dirs) if match else [],
            "frontmatter": fm,  # NEW — full parsed dict
        }
```

Apply the same add to the `run_preflight_inbox` item dict (the PCM-backed path).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/preflight/test_inbox.py -v`
Expected: all pass.

Full preflight suite too:
Run: `uv run pytest tests/preflight/ -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/inbox.py tests/preflight/test_inbox.py
git commit -m "$(cat <<'EOF'
feat(preflight): inbox surfaces full frontmatter per item

Same generalization pattern as finance. Inbox items now carry the full
parsed frontmatter dict in addition to the derived task_type — so
fields like priority, sender, due, status are visible without the
agent re-reading the file.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task A3: Smoke test Phase A

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: all pass (~450+ tests).

- [ ] **Step 2: Run a dashboard-visible 1-task smoke test**

Sources .env and launches a single PROD task via `run-benchmark --max-trials 1`:

```bash
source .worktrees/plan-b/.env
uv run bitgn-agent run-benchmark \
  --max-trials 1 \
  --max-parallel 1 \
  --max-inflight-llm 6 \
  --runs 1 \
  --output artifacts/bench/phaseA-smoke-$(date +%Y%m%d_%H%M%S).json
```

Expected: benchmark completes, output JSON written, pass rate reported. No exceptions in log.

- [ ] **Step 3: If smoke fails, diagnose + fix, commit the fix, re-smoke**

Diagnose via the trace JSONL in the `logs/<stamp>/` dir. Common failure shapes:
- Unicode issue in frontmatter → check `_parse_frontmatter` tolerates
- Payload size blowup → add a per-field byte cap if any field exceeds 4KB

Commit fixes as separate `fix(preflight): ...` commits.

---

## Task B1: Add `Req_PreflightUnknown` + `Rsp_PreflightUnknown` schemas

**Files:**
- Modify: `src/bitgn_contest_agent/schemas.py` — add request/response Pydantic schemas
- Test: `tests/test_schemas.py` (or equivalent) — round-trip serialization test

**Rationale:** preflight_unknown needs a structured output shape the LLM can fill predictably. Pydantic schema lets us validate the shape and reject hallucinations (e.g., LLM suggests paths that don't exist in the workspace).

- [ ] **Step 1: Write the failing test**

Add or append to `tests/test_schemas.py` (create if it doesn't exist):

```python
def test_req_preflight_unknown_roundtrip():
    from bitgn_contest_agent.schemas import Req_PreflightUnknown
    req = Req_PreflightUnknown(
        tool="preflight_unknown",
        task_text="When was my ambient AI buddy born?",
        workspace_schema_summary="entities_root=10_entities/cast/, projects_root=40_projects/, ...",
        allowed_roots=["10_entities/cast/", "40_projects/", "50_finance/invoices/"],
    )
    assert req.tool == "preflight_unknown"
    js = req.model_dump_json()
    rt = Req_PreflightUnknown.model_validate_json(js)
    assert rt.task_text == req.task_text


def test_rsp_preflight_unknown_roundtrip():
    from bitgn_contest_agent.schemas import Rsp_PreflightUnknown, UnknownRecommendedRoot
    rsp = Rsp_PreflightUnknown(
        likely_class="entity_attribute_lookup",
        clarification_risk_flagged=True,
        clarification_risk_why="descriptor may be ambiguous",
        recommended_roots=[
            UnknownRecommendedRoot(path="10_entities/cast/", why="task references a person"),
        ],
        investigation_plan=["enumerate candidates", "verify unique match"],
        known_pitfalls=["descriptor is not a unique-name match"],
    )
    js = rsp.model_dump_json()
    rt = Rsp_PreflightUnknown.model_validate_json(js)
    assert rt.likely_class == "entity_attribute_lookup"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schemas.py -v` (or wherever you placed it)
Expected: FAIL — `Req_PreflightUnknown` not found in schemas module.

- [ ] **Step 3: Add schemas**

In `src/bitgn_contest_agent/schemas.py`, append:

```python
class Req_PreflightUnknown(BaseModel):
    """Fires when the router returns UNKNOWN (no bound skill). The
    preflight classifies the task and emits a structured investigation
    scaffold so the agent doesn't cold-start exploration.
    """
    tool: Literal["preflight_unknown"] = "preflight_unknown"
    task_text: str
    workspace_schema_summary: str
    # Allowed roots constrain the LLM's recommended_roots — it can only
    # point at paths that actually exist in the workspace schema. This
    # is the hallucination guard.
    allowed_roots: list[str] = Field(default_factory=list)


class UnknownRecommendedRoot(BaseModel):
    path: str
    why: str


class Rsp_PreflightUnknown(BaseModel):
    """Structured scaffold the preflight emits for the agent."""
    likely_class: Literal[
        "entity_attribute_lookup",
        "inbox_processing",
        "security_refusal",
        "cleanup_receipts",
        "ambiguous_referent",
        "other",
    ]
    clarification_risk_flagged: bool
    clarification_risk_why: str = ""
    recommended_roots: list[UnknownRecommendedRoot] = Field(default_factory=list)
    investigation_plan: list[str] = Field(default_factory=list)
    known_pitfalls: list[str] = Field(default_factory=list)
```

Note: check existing imports at top of schemas.py — `Literal` and `Field` may already be imported.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/schemas.py tests/test_schemas.py
git commit -m "$(cat <<'EOF'
feat(schemas): Req_PreflightUnknown + Rsp_PreflightUnknown

Pydantic shapes for the UNKNOWN-route preflight. allowed_roots on the
request constrains the LLM's recommended_roots to paths that exist in
the workspace — hallucination guard.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task B2: Implement `preflight_unknown`

**Files:**
- Create: `src/bitgn_contest_agent/preflight/unknown.py`
- Test: `tests/preflight/test_unknown.py`

**Rationale:** Single LLM call via the existing backend. Structured output via `Rsp_PreflightUnknown`. No PCM ops — operates on already-parsed workspace schema.

- [ ] **Step 1: Write the failing test**

Create `tests/preflight/test_unknown.py`:

```python
"""Tests for preflight_unknown — the generic UNKNOWN-route preflight.

Mocks the LLM backend; asserts the tool passes the right prompt and
wraps the response as a ToolResult the agent can consume.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bitgn_contest_agent.preflight.unknown import run_preflight_unknown
from bitgn_contest_agent.schemas import (
    Req_PreflightUnknown,
    Rsp_PreflightUnknown,
    UnknownRecommendedRoot,
)


def test_preflight_unknown_returns_structured_scaffold():
    fake_rsp = Rsp_PreflightUnknown(
        likely_class="ambiguous_referent",
        clarification_risk_flagged=True,
        clarification_risk_why="descriptor 'ambient AI buddy' is not a unique name",
        recommended_roots=[
            UnknownRecommendedRoot(path="10_entities/cast/", why="task mentions a person"),
        ],
        investigation_plan=["enumerate candidates", "if >1 match → OUTCOME_NONE_CLARIFICATION"],
        known_pitfalls=["descriptor matching != unique-name match"],
    )
    fake_backend = MagicMock()
    fake_backend.call_structured.return_value = fake_rsp

    req = Req_PreflightUnknown(
        task_text="When was my ambient AI buddy born?",
        workspace_schema_summary="entities_root=10_entities/cast/, projects_root=40_projects/",
        allowed_roots=["10_entities/cast/", "40_projects/"],
    )
    out = run_preflight_unknown(backend=fake_backend, req=req)
    assert out.ok is True
    assert "ambiguous_referent" in out.content
    assert "clarification" in out.content.lower()
    fake_backend.call_structured.assert_called_once()


def test_preflight_unknown_rejects_hallucinated_root():
    """If the LLM recommends a root not in allowed_roots, it gets
    filtered out of the emitted content."""
    fake_rsp = Rsp_PreflightUnknown(
        likely_class="other",
        clarification_risk_flagged=False,
        recommended_roots=[
            UnknownRecommendedRoot(path="99_made_up_root/", why="hallucination"),
            UnknownRecommendedRoot(path="10_entities/cast/", why="legit"),
        ],
    )
    fake_backend = MagicMock()
    fake_backend.call_structured.return_value = fake_rsp

    req = Req_PreflightUnknown(
        task_text="whatever",
        workspace_schema_summary="...",
        allowed_roots=["10_entities/cast/"],
    )
    out = run_preflight_unknown(backend=fake_backend, req=req)
    assert "99_made_up_root" not in out.content
    assert "10_entities/cast/" in out.content


def test_preflight_unknown_backend_error_returns_skipped_not_crash():
    """Backend exception must not crash the agent — return ok=False
    and let the agent fall through to normal exploration."""
    fake_backend = MagicMock()
    fake_backend.call_structured.side_effect = RuntimeError("llm died")

    req = Req_PreflightUnknown(
        task_text="x",
        workspace_schema_summary="...",
        allowed_roots=[],
    )
    out = run_preflight_unknown(backend=fake_backend, req=req)
    assert out.ok is False
    assert out.error_code == "INTERNAL"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/preflight/test_unknown.py -v`
Expected: FAIL — module doesn't exist yet.

- [ ] **Step 3: Implement the preflight**

Create `src/bitgn_contest_agent/preflight/unknown.py`:

```python
"""preflight_unknown — generic scaffold for tasks the router couldn't
bind to a skill. Emits a structured investigation plan so the agent
starts with food for thought rather than cold-start tree/search.

Unlike other preflights this does NOT call PCM — it operates on the
already-captured workspace schema + a single LLM classification call.
Failures degrade gracefully: the agent falls through to manual
investigation as it would without this preflight.
"""
from __future__ import annotations

import json
from typing import Any

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.response import build_response
from bitgn_contest_agent.schemas import (
    Req_PreflightUnknown,
    Rsp_PreflightUnknown,
)


_PROMPT_TEMPLATE = """You help an agent bootstrap investigation of a task
that could not be routed to a specialised skill. Analyse the task and
emit a structured scaffold. Be honest when the task is ambiguous.

TASK:
{task}

WORKSPACE SCHEMA (these are the roots that actually exist):
{schema}

ALLOWED ROOTS (you MUST only name paths from this list in
recommended_roots; do not invent new paths):
{allowed_roots}

Classify the task and emit the Rsp_PreflightUnknown object.
Guidelines:
- likely_class: pick the best fit from the enum; "other" is fine
- clarification_risk_flagged: true when the task references an entity
  by descriptor (e.g. "the new hire", "my AI buddy") rather than a
  unique name, OR when multiple candidates could reasonably match
- recommended_roots: 1-4 top roots the agent should investigate,
  drawn ONLY from ALLOWED ROOTS; each with a short "why"
- investigation_plan: 2-5 concrete steps (not abstract advice)
- known_pitfalls: 0-3 gotchas specific to this task class
"""


def _render_content(rsp: Rsp_PreflightUnknown, allowed: set[str]) -> str:
    """Render the structured response as a concise block the agent
    sees. Filters out hallucinated roots not in `allowed`."""
    filtered_roots = [r for r in rsp.recommended_roots if r.path in allowed]
    lines = [
        f"likely_class: {rsp.likely_class}",
    ]
    if rsp.clarification_risk_flagged:
        lines.append(
            f"clarification_risk: FLAGGED — {rsp.clarification_risk_why}"
        )
    if filtered_roots:
        lines.append("recommended_roots:")
        for r in filtered_roots:
            lines.append(f"  - {r.path} — {r.why}")
    if rsp.investigation_plan:
        lines.append("investigation_plan:")
        for i, step in enumerate(rsp.investigation_plan, 1):
            lines.append(f"  {i}. {step}")
    if rsp.known_pitfalls:
        lines.append("known_pitfalls:")
        for p in rsp.known_pitfalls:
            lines.append(f"  - {p}")
    return "\n".join(lines)


def run_preflight_unknown(*, backend: Any, req: Req_PreflightUnknown) -> ToolResult:
    """Dispatch the preflight. `backend` must expose a `call_structured`
    method that takes (prompt: str, response_schema: type[BaseModel])
    and returns an instance of response_schema. See Task B4 for the
    backend-side plumbing of call_structured.
    """
    try:
        prompt = _PROMPT_TEMPLATE.format(
            task=req.task_text,
            schema=req.workspace_schema_summary,
            allowed_roots="\n".join(f"  - {r}" for r in req.allowed_roots),
        )
        rsp: Rsp_PreflightUnknown = backend.call_structured(
            prompt, Rsp_PreflightUnknown,
        )
    except Exception as exc:  # noqa: BLE001 — never crash the agent
        return ToolResult(
            ok=False, content="", refs=tuple(),
            error=f"preflight_unknown failed: {exc}",
            error_code="INTERNAL", wall_ms=0,
        )
    content_body = _render_content(rsp, set(req.allowed_roots))
    summary = (
        f"Task classified as '{rsp.likely_class}'. "
        f"Clarification risk: {'FLAGGED' if rsp.clarification_risk_flagged else 'no'}."
    )
    return ToolResult(
        ok=True,
        content=build_response(summary=summary, data={"scaffold": content_body}),
        refs=tuple(), error=None, error_code=None, wall_ms=0,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/preflight/test_unknown.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/unknown.py tests/preflight/test_unknown.py
git commit -m "$(cat <<'EOF'
feat(preflight): add preflight_unknown — scaffold for UNKNOWN route

Single LLM-light call classifies the task, flags clarification risk,
and emits an investigation plan rooted in the workspace schema.
Hallucinated roots (paths not in allowed_roots) are filtered out
before emission. Backend exceptions degrade gracefully to ok=False so
the agent falls through to normal exploration.

Designed to catch t000-class failures: "When was my ambient AI buddy
born?" → clarification_risk FLAGGED, likely_class=ambiguous_referent.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task B3: Plumb `backend.call_structured` for preflight_unknown

**Files:**
- Modify: `src/bitgn_contest_agent/backend/base.py` — add `call_structured` abstract method
- Modify: `src/bitgn_contest_agent/backend/openai_compat.py` — implement `call_structured`
- Test: `tests/test_backend_openai_compat.py` — add round-trip test

**Rationale:** The existing `Backend.next_step` is coupled to NextStep + messages. preflight_unknown needs a simpler "one-shot structured call" primitive. Adding `call_structured(prompt, schema) → BaseModel` is a clean expansion.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_openai_compat.py`:

```python
def test_call_structured_parses_response(mocker: Any) -> None:
    """call_structured takes a prompt + Pydantic schema, returns an
    instance of that schema. Uses the structured-output path when
    enabled; falls back to streaming + manual validate otherwise."""
    from pydantic import BaseModel

    class _Shape(BaseModel):
        verdict: str
        ok: bool

    fake_client = MagicMock()
    fake_parsed = _Shape(verdict="yes", ok=True)
    completion = MagicMock()
    completion.choices = [
        MagicMock(message=MagicMock(parsed=fake_parsed, content='{"verdict":"yes","ok":true}'))
    ]
    completion.usage = MagicMock(prompt_tokens=5, completion_tokens=3)
    fake_client.beta.chat.completions.parse.return_value = completion

    backend = OpenAIChatBackend(
        client=fake_client,
        model="gpt-5.3-codex",
        reasoning_effort="medium",
        use_structured_output=True,
    )
    out = backend.call_structured("test prompt", _Shape)
    assert isinstance(out, _Shape)
    assert out.verdict == "yes"
    assert out.ok is True
```

Also add to `src/bitgn_contest_agent/backend/base.py` contract tests if they exist.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backend_openai_compat.py::test_call_structured_parses_response -v`
Expected: FAIL — `call_structured` method doesn't exist.

- [ ] **Step 3: Add `call_structured` to the base + openai_compat backend**

In `src/bitgn_contest_agent/backend/base.py`, add to the abstract `Backend` class:

```python
from typing import TypeVar
from pydantic import BaseModel

_T = TypeVar("_T", bound=BaseModel)


class Backend(Protocol):
    # ... existing next_step ...

    def call_structured(
        self, prompt: str, response_schema: type[_T], *, timeout_sec: float = 30.0,
    ) -> _T:
        """One-shot structured call — takes a text prompt and a Pydantic
        schema, returns an instance of that schema. Used by preflight
        tools that need LLM classification without the full message-list
        plumbing of next_step."""
        ...
```

In `src/bitgn_contest_agent/backend/openai_compat.py`, implement on `OpenAIChatBackend`:

```python
    def call_structured(
        self,
        prompt: str,
        response_schema: type[_T],
        *,
        timeout_sec: float = 30.0,
    ) -> _T:
        """One-shot structured call — delegates to the same two paths as
        next_step (structured via beta.parse; streaming + manual validate
        otherwise)."""
        payload = [{"role": "user", "content": prompt}]
        try:
            if self._use_structured_output:
                completion = self._client.beta.chat.completions.parse(
                    model=self._model,
                    messages=payload,
                    response_format=response_schema,
                    timeout=timeout_sec,
                    extra_body={"reasoning": {"effort": self._reasoning_effort}},
                )
                parsed = completion.choices[0].message.parsed
                if parsed is None:
                    raw = completion.choices[0].message.content or ""
                    parsed = response_schema.model_validate_json(raw)
                return parsed
            # Fallback: streaming + manual validate (same pattern as next_step).
            stream = self._client.chat.completions.create(
                model=self._model,
                messages=payload,
                stream=True,
                stream_options={"include_usage": True},
                timeout=timeout_sec,
                extra_body={"reasoning": {"effort": self._reasoning_effort}},
            )
            parts: list[str] = []
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None) if delta else None
                if piece:
                    parts.append(piece)
            raw = _extract_json_object("".join(parts))
            return response_schema.model_validate_json(raw)
        except _TRANSIENT_EXCEPTIONS as exc:
            raise TransientBackendError(str(exc)) from exc
        except openai.APIError as exc:
            if _is_transient_by_message(exc):
                raise TransientBackendError(str(exc)) from exc
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_backend_openai_compat.py -v`
Expected: all pass (12+ tests including the new one).

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/backend/base.py src/bitgn_contest_agent/backend/openai_compat.py tests/test_backend_openai_compat.py
git commit -m "$(cat <<'EOF'
feat(backend): Backend.call_structured for one-shot structured LLM calls

Adds a cleaner primitive for preflight tools that need classification
without the message-list plumbing of next_step. Reuses the same two
code paths (beta.parse for structured-output; streaming + manual
validate otherwise) and the same transient-error remapping.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task B4: Wire preflight_unknown into routed_preflight dispatcher

**Files:**
- Modify: `src/bitgn_contest_agent/routed_preflight.py` — add `_build_unknown`, register, replace `no_skill` early-return
- Modify: `src/bitgn_contest_agent/agent.py:715-725` — pass `backend` through the dispatcher call
- Test: `tests/test_routed_preflight.py` — assert UNKNOWN path dispatches preflight_unknown

**Rationale:** Current dispatcher returns early when `skill_name is None`. Replace that with "dispatch preflight_unknown instead". Agent plumbs `backend` so the dispatcher can pass it to `run_preflight_unknown`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_routed_preflight.py`:

```python
def test_dispatch_routes_unknown_to_preflight_unknown():
    """When decision.skill_name is None (router returned UNKNOWN), the
    dispatcher runs preflight_unknown rather than skipping with
    reason='no_skill'."""
    from bitgn_contest_agent.router import RoutingDecision, RoutingSource
    from bitgn_contest_agent.routed_preflight import dispatch_routed_preflight
    from bitgn_contest_agent.preflight.schema import WorkspaceSchema
    from unittest.mock import MagicMock

    fake_adapter = MagicMock()
    fake_adapter.dispatch.return_value = MagicMock(
        ok=True, content="scaffold content", bytes=10, wall_ms=5,
        error=None, error_code=None,
    )
    fake_backend = MagicMock()

    schema = WorkspaceSchema(
        workspace_root="/ws",
        entities_root="10_entities/cast",
        projects_root="40_projects",
        finance_roots=["50_finance/invoices"],
        inbox_root="00_inbox",
    )
    decision = RoutingDecision(
        skill_name=None, category="UNKNOWN", source=RoutingSource.DEFAULT,
        confidence=0.0, extracted={}, task_text="when was my ambient buddy born",
    )
    out = dispatch_routed_preflight(
        decision=decision,
        schema=schema,
        adapter=fake_adapter,
        skills_by_name={},
        backend=fake_backend,
    )
    assert out.tool == "preflight_unknown"
    assert out.result is not None and out.result.ok is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_routed_preflight.py -v`
Expected: FAIL — `backend` kwarg not accepted.

- [ ] **Step 3: Wire dispatcher and agent**

In `src/bitgn_contest_agent/routed_preflight.py`:

```python
def dispatch_routed_preflight(
    *,
    decision: RoutingDecision,
    schema: WorkspaceSchema,
    adapter: Any,
    skills_by_name: Dict[str, BitgnSkill],
    backend: Any = None,  # NEW — needed by preflight_unknown
) -> RoutedPreflightOutcome:
    # Replace the old "no_skill" early-return with a dispatch to
    # preflight_unknown when a backend is available.
    if decision.skill_name is None:
        if backend is None:
            return RoutedPreflightOutcome(skipped_reason="no_skill")
        return _dispatch_unknown(
            decision=decision, schema=schema, backend=backend,
        )
    # ... rest unchanged ...
```

Add `_dispatch_unknown` helper:

```python
def _dispatch_unknown(
    *,
    decision: RoutingDecision,
    schema: WorkspaceSchema,
    backend: Any,
) -> RoutedPreflightOutcome:
    from bitgn_contest_agent.preflight.unknown import run_preflight_unknown
    from bitgn_contest_agent.schemas import Req_PreflightUnknown

    allowed = [r for r in [
        schema.entities_root, schema.projects_root, schema.inbox_root,
        *schema.finance_roots,
    ] if r]

    # Compact summary — one line per root. Sized to fit even with
    # aggressive prompt caching.
    summary_lines = []
    if schema.entities_root:
        summary_lines.append(f"entities_root={schema.entities_root}")
    if schema.projects_root:
        summary_lines.append(f"projects_root={schema.projects_root}")
    if schema.inbox_root:
        summary_lines.append(f"inbox_root={schema.inbox_root}")
    if schema.finance_roots:
        summary_lines.append(f"finance_roots={','.join(schema.finance_roots)}")

    req = Req_PreflightUnknown(
        task_text=decision.task_text,
        workspace_schema_summary="; ".join(summary_lines),
        allowed_roots=allowed,
    )
    try:
        result = run_preflight_unknown(backend=backend, req=req)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("preflight_unknown raised: %s", exc)
        return RoutedPreflightOutcome(
            tool="preflight_unknown", skipped_reason="dispatch_exception",
            error=str(exc),
        )
    return RoutedPreflightOutcome(tool="preflight_unknown", result=result)
```

In `src/bitgn_contest_agent/agent.py`, around line 719-725, add `backend=self._backend`:

```python
        with pcm_origin("routed_preflight"):
            outcome = dispatch_routed_preflight(
                decision=decision,
                schema=schema,
                adapter=self._adapter,
                skills_by_name=self._router.skills_by_name(),
                backend=self._backend,  # NEW
            )
```

Note: check `RoutingDecision` has `task_text`. If not, add it (routing already knows the task text) or pass it via a separate arg. Ground this in the actual RoutingDecision shape before writing code.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_routed_preflight.py -v tests/preflight/test_unknown.py -v`
Expected: all pass.

Full suite:
Run: `uv run pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/routed_preflight.py src/bitgn_contest_agent/agent.py tests/test_routed_preflight.py
git commit -m "$(cat <<'EOF'
feat(routed-preflight): dispatch preflight_unknown on UNKNOWN route

Replaces the skipped_reason='no_skill' early-return with a dispatch to
preflight_unknown. Router-UNKNOWN tasks now receive a structured
investigation scaffold (likely_class, clarification_risk, recommended
roots, plan) instead of cold-starting tree/search.

Agent plumbs self._backend through so the dispatcher can hand it to
preflight_unknown for the LLM classification call.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task B5: Smoke test Phase B

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: all pass.

- [ ] **Step 2: Dashboard-visible 1-task smoke**

```bash
source .worktrees/plan-b/.env
uv run bitgn-agent run-benchmark \
  --max-trials 1 \
  --max-parallel 1 \
  --max-inflight-llm 6 \
  --runs 1 \
  --output artifacts/bench/phaseB-smoke-$(date +%Y%m%d_%H%M%S).json
```

Expected: benchmark completes. Check the trace in `logs/<stamp>/` for an `arch` record with `category=UNKNOWN` — it should show preflight_unknown dispatched.

- [ ] **Step 3: If smoke fails, diagnose + fix, commit the fix, re-smoke**

Most likely failure modes:
- Backend `call_structured` mismatches a field — update schema defaults
- Prompt cache miss blows cost — no action needed for smoke, monitor in full bench
- Hallucinated roots pass through filter — tighten allowed_roots regex

---

## Task C: Push + full bench

- [ ] **Step 1: Push branch**

```bash
git push origin feat/r4-validator-correctness
```

- [ ] **Step 2: Launch full bench at p3i6**

```bash
source .worktrees/plan-b/.env
STAMP=$(date +%Y%m%d_%H%M%S)
HASH=$(git rev-parse --short=7 HEAD)
LOG=/tmp/full-bench-${HASH}-${STAMP}.log
OUT=artifacts/bench/${HASH}_preflight_gen_unknown_p3i6_gpt54_prod_runs1.json

nohup uv run bitgn-agent run-benchmark \
  --max-parallel 3 \
  --max-inflight-llm 6 \
  --runs 1 \
  --output "$OUT" > "$LOG" 2>&1 &
disown
echo "PID=$! LOG=$LOG OUT=$OUT"
```

- [ ] **Step 3: Monitor, report pass-rate vs baseline (100/104) when done**

When bench exits, inspect `$OUT` and run:

```bash
uv run scripts/failure_report.py "$OUT" | head -200
```

Compare to `artifacts/bench/1af9bd7_usewhen_p3i6_gpt54_prod_runs1.json` (last good = 100/104).
