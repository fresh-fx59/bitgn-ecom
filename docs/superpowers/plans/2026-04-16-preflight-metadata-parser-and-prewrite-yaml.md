# Preflight Metadata Parser + Pre-Write YAML Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `preflight_project` work on PAC1 PROD workspaces (bullet-list / ASCII-table metadata) and stop post-write YAML validation from producing duplicate-write grader rejections.

**Architecture:** One unified `parse_record_metadata` handles YAML frontmatter, bullet lists, and `|pipe|tables|`. Classifier switches from ad-hoc substring fallbacks to a single `record_type` check. `preflight_project` recurses into `<slug>/README.MD` subdirs, returns the match path in `refs`, and stops leaking `start_date` in its summary. A new pre-write YAML guard in `PcmAdapter.dispatch` fails bad writes before persistence so the post-write `FORMAT_VALIDATOR` never forces a duplicate write. Three new `TracePrepass` fields (`schema_roots`, `match_found`, `match_file`) and one new arch category (`FORMAT_PRE_WRITE_REJECT`) make the effect directly greppable in bench logs.

**Tech Stack:** Python 3.12, Pydantic v2 (TracePrepass), PyYAML (pre-write check — already bundled), pytest, bitgn.vm protobufs.

**Spec:** `docs/superpowers/specs/2026-04-16-preflight-metadata-parser-and-prewrite-yaml-design.md` (commit 84dfe9b)

**Branch:** `feat/r4-validator-correctness` (direct commits — do not stack a child branch per branch-stacking memory)

---

## File Structure

**Modified:**
- `src/bitgn_contest_agent/preflight/schema.py` — add `parse_record_metadata`; simplify `_classify_dir` to use `record_type`; delete `_classify_dir_by_content`
- `src/bitgn_contest_agent/preflight/project.py` — subdir recursion, use unified parser, refs attribution, non-leaky summary
- `src/bitgn_contest_agent/trace_schema.py` — extend `TracePrepass` with 3 optional fields
- `src/bitgn_contest_agent/trace_writer.py` — extend `append_prepass` signature
- `src/bitgn_contest_agent/arch_constants.py` — add `FORMAT_PRE_WRITE_REJECT` enum member
- `src/bitgn_contest_agent/adapter/pcm.py` — pre-write YAML validation + arch emission on `Req_Write`
- `src/bitgn_contest_agent/agent.py` — wire `schema_roots` into `preflight_schema` prepass emission; wire `match_found`/`match_file` into `routed_preflight_*` prepass emission

**New tests:**
- `tests/preflight/test_metadata_parser.py` — YAML / bullet / table encodings
- `tests/adapter/__init__.py` — package init
- `tests/adapter/test_pcm_write_validation.py` — pre-write YAML reject

**Updated tests:**
- `tests/preflight/test_schema.py` — PROD-shape classifier cases, delete `_classify_dir_by_content` tests if any
- `tests/preflight/test_project.py` — subdir README.MD layout, refs, non-leaky summary
- `tests/test_trace_writer.py` — optional `schema_roots`, `match_found`, `match_file`

---

## Task 1: Unified metadata parser

**Files:**
- Modify: `src/bitgn_contest_agent/preflight/schema.py`
- Test: `tests/preflight/test_metadata_parser.py` (create)

**Purpose:** One `parse_record_metadata(text) -> dict[str, str]` that handles YAML frontmatter, markdown bullet lists, and ASCII pipe tables. Keys lowercased, multi-line values kept as raw strings.

- [ ] **Step 1: Write the failing tests**

Create `tests/preflight/test_metadata_parser.py`:

```python
from bitgn_contest_agent.preflight.schema import parse_record_metadata


def test_parses_yaml_frontmatter():
    text = (
        "---\n"
        "record_type: project\n"
        "project: Foo\n"
        "start_date: 2026-01-01\n"
        "---\n"
        "Body text.\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "project"
    assert md["project"] == "Foo"
    assert md["start_date"] == "2026-01-01"


def test_parses_bullet_list():
    text = (
        "# Studio Parts Library\n"
        "\n"
        "- record_type: project\n"
        "- project: Studio Parts Library\n"
        "- start_date: 2026-04-21\n"
        "- members: alice, bob\n"
        "\n"
        "Detail body follows.\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "project"
    assert md["project"] == "Studio Parts Library"
    assert md["start_date"] == "2026-04-21"
    assert md["members"] == "alice, bob"


def test_parses_ascii_table():
    text = (
        "# Invoice INV-001\n"
        "\n"
        "| field | value |\n"
        "| --- | --- |\n"
        "| record_type | invoice |\n"
        "| vendor | ACME Corp |\n"
        "| eur_total | 150.00 |\n"
        "\n"
        "Line items follow.\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "invoice"
    assert md["vendor"] == "ACME Corp"
    assert md["eur_total"] == "150.00"


def test_yaml_wins_when_all_three_present():
    text = (
        "---\n"
        "record_type: project\n"
        "project: FromYaml\n"
        "---\n"
        "\n"
        "- record_type: project\n"
        "- project: FromBullet\n"
    )
    md = parse_record_metadata(text)
    assert md["project"] == "FromYaml"


def test_empty_on_no_metadata():
    text = "Just prose, no metadata here."
    assert parse_record_metadata(text) == {}


def test_bullet_fallback_when_yaml_malformed():
    # YAML frontmatter missing closing delimiter → skipped; bullet wins.
    text = (
        "---\n"
        "not: really: yaml\n"
        "\n"
        "- record_type: person\n"
        "- name: Alice\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "person"
    assert md["name"] == "Alice"


def test_keys_lowercased():
    text = (
        "- Record_Type: project\n"
        "- PROJECT: Foo\n"
    )
    md = parse_record_metadata(text)
    assert "record_type" in md
    assert "project" in md
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/preflight/test_metadata_parser.py -v`
Expected: ImportError or "parse_record_metadata not defined".

- [ ] **Step 3: Implement `parse_record_metadata`**

In `src/bitgn_contest_agent/preflight/schema.py`, add (keep existing `_parse_frontmatter` in place for now; Task 2 will consolidate callers):

```python
def parse_record_metadata(text: str) -> dict[str, str]:
    """Unified metadata reader for YAML frontmatter, markdown bullet
    lists, and ASCII pipe tables. Returns lowercased-key dict. Returns
    {} on unknown shapes — callers treat empty as "no classifiable
    metadata" (fail-safe).

    Scan order: YAML → bullet list → ASCII table. First non-empty wins.
    """
    yaml_md = _parse_frontmatter_yaml(text)
    if yaml_md:
        return yaml_md
    bullet_md = _parse_bullet_list(text)
    if bullet_md:
        return bullet_md
    return _parse_ascii_table(text)


def _parse_frontmatter_yaml(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    body = m.group(1)
    out: dict[str, str] = {}
    for line in body.splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip()
    return out


_BULLET_RE = re.compile(r"^-\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def _parse_bullet_list(text: str) -> dict[str, str]:
    """Scan the top of the file for contiguous `- key: value` lines.

    Skips leading blank lines and a single markdown heading (`# ...`).
    Stops at the first line that doesn't match the bullet pattern.
    """
    out: dict[str, str] = {}
    started = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not started:
            if not line or line.startswith("#"):
                continue
            m = _BULLET_RE.match(line)
            if not m:
                # Not a bullet list file.
                return {}
            started = True
            out[m.group(1).lower()] = m.group(2).strip()
            continue
        m = _BULLET_RE.match(line)
        if m:
            out[m.group(1).lower()] = m.group(2).strip()
            continue
        if line.strip() == "":
            # Blank line ends the bullet block.
            break
        # Non-bullet, non-blank line → stop scanning.
        break
    return out


def _parse_ascii_table(text: str) -> dict[str, str]:
    """Parse a simple two-column markdown pipe table.

    Expected shape:
        | field | value |
        | --- | --- |
        | key1 | val1 |
        | key2 | val2 |

    Header row and separator row are skipped. Returns {} if no such
    table exists at the top of the file.
    """
    out: dict[str, str] = {}
    lines = text.splitlines()
    # Find the first `| ... |` line.
    start = None
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            start = i
            break
        if stripped and not stripped.startswith("#"):
            # Non-heading, non-empty, non-table line → no table here.
            return {}
    if start is None:
        return {}
    # Skip header + separator if present.
    rows = lines[start:]
    if len(rows) < 3:
        return {}
    sep = rows[1].strip()
    if not all(c in "|-: " for c in sep):
        return {}
    for raw in rows[2:]:
        stripped = raw.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            break
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = cells[0].lower()
        if not key:
            continue
        out[key] = cells[1]
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/preflight/test_metadata_parser.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/schema.py tests/preflight/test_metadata_parser.py
git commit -m "feat(preflight): unified parse_record_metadata for YAML/bullet/table"
```

---

## Task 2: Classifier uses record_type (delete substring fallback)

**Files:**
- Modify: `src/bitgn_contest_agent/preflight/schema.py` (`_classify_dir`, delete `_classify_dir_by_content`, update `run_preflight_schema`)
- Test: `tests/preflight/test_schema.py` (update)

**Purpose:** One classification path. `_classify_dir` inspects `record_type` on each parsed record. Invoice directories never get tagged as `projects` again.

- [ ] **Step 1: Write PROD-shape failing test**

In `tests/preflight/test_schema.py`, append (keep existing DEV tests, they must still pass):

```python
from bitgn_contest_agent.preflight.schema import _classify_dir


def test_classify_prod_invoices_as_finance_only():
    # PROD-shape invoice: bullet list with record_type=invoice and a
    # line_items section that mentions "project" — must NOT be classified
    # as projects.
    invoices = [
        {
            "record_type": "invoice",
            "vendor": "ACME",
            "line_items": "project management, consulting",
        }
        for _ in range(3)
    ]
    roles = _classify_dir(invoices)
    assert "finance" in roles
    assert "projects" not in roles


def test_classify_prod_projects_as_projects():
    projects = [
        {"record_type": "project", "project": "Studio Parts Library", "start_date": "2026-04-21"},
        {"record_type": "project", "project": "Toy Forge Saturdays", "start_date": "2026-03-01"},
    ]
    roles = _classify_dir(projects)
    assert "projects" in roles


def test_classify_prod_entities_as_entities():
    people = [
        {"record_type": "person", "name": "Alice"},
        {"record_type": "person", "name": "Bob"},
        {"record_type": "cast", "name": "Crew A"},
    ]
    roles = _classify_dir(people)
    assert "entities" in roles


def test_classify_prod_inbox_as_inbox():
    inbox_items = [
        {"record_type": "inbound_email", "from": "a@example.com"},
        {"record_type": "inbox", "from": "b@example.com"},
    ]
    roles = _classify_dir(inbox_items)
    assert "inbox" in roles


def test_classify_prod_outbox_as_outbox():
    outbox_items = [
        {"record_type": "outbound_email", "to": "a@example.com", "subject": "hi"},
        {"record_type": "outbox", "to": "b@example.com", "subject": "hello"},
    ]
    roles = _classify_dir(outbox_items)
    assert "outbox" in roles
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/preflight/test_schema.py -v`
Expected: Some NEW tests FAIL (current `_classify_dir` uses DEV-style keys like `vendor`, `aliases`).

- [ ] **Step 3: Rewrite `_classify_dir` and route `run_preflight_schema` through the unified parser**

In `src/bitgn_contest_agent/preflight/schema.py`, replace `_classify_dir` body (keep signature):

```python
_RECORD_TYPE_TO_ROLE = {
    "project": "projects",
    "invoice": "finance",
    "bill": "finance",
    "receipt": "finance",
    "purchase": "finance",
    "inbound_email": "inbox",
    "inbox": "inbox",
    "outbound_email": "outbox",
    "outbox": "outbox",
    "person": "entities",
    "entity": "entities",
    "cast": "entities",
}


def _classify_dir(frontmatters: list[dict[str, str]]) -> list[str]:
    """Return role labels the directory's records match.

    Threshold: ≥30% of records share a role. Records without a
    recognized `record_type` contribute no vote. DEV-shape records
    using `record_type` field names map identically to PROD-shape
    records — no separate path needed.
    """
    if not frontmatters:
        return []
    n = len(frontmatters)
    counts: dict[str, int] = {}
    for fm in frontmatters:
        rt = (fm.get("record_type") or "").strip().lower()
        role = _RECORD_TYPE_TO_ROLE.get(rt)
        if role:
            counts[role] = counts.get(role, 0) + 1
    return [role for role, c in counts.items() if c / n >= _MATCH_THRESHOLD]
```

Delete `_classify_dir_by_content` entirely (function + all references).

In `run_preflight_schema`, replace the two-step "try frontmatter then substring" block with a single unified parser call. Find:

```python
            frontmatters = []
            raw_contents: list[str] = []
            for name in md_names:
                read_resp = client.read(
                    pcm_pb2.ReadRequest(path=f"{d}/{name}")
                )
                frontmatters.append(_parse_frontmatter(read_resp.content))
                raw_contents.append(read_resp.content)
            roles = _classify_dir(frontmatters)
            if not roles:
                # Fall back to content-based classification for PAC1 PROD.
                roles = _classify_dir_by_content(raw_contents)
```

Replace with:

```python
            frontmatters = []
            for name in md_names:
                read_resp = client.read(
                    pcm_pb2.ReadRequest(path=f"{d}/{name}")
                )
                frontmatters.append(parse_record_metadata(read_resp.content))
            roles = _classify_dir(frontmatters)
```

In `discover_schema_from_fs`, change `_parse_frontmatter(text)` to `parse_record_metadata(text)` (one call site). Then delete the now-orphaned `_parse_frontmatter` function.

**Note — backward compatibility:** `preflight/project.py` currently imports `_parse_frontmatter`. Task 5 replaces that import. Until then, add a one-line shim at the top of `schema.py` module level to avoid breaking Task 2's commit-boundary tests:

```python
# Back-compat shim — `preflight/project.py` still imports this name in
# the working tree until Task 5 rewires it. Safe to delete after Task 5.
_parse_frontmatter = parse_record_metadata
```

- [ ] **Step 4: Run full preflight test suite**

Run: `uv run pytest tests/preflight/ -v`
Expected: All pass (new PROD tests + existing DEV tests).

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/schema.py tests/preflight/test_schema.py
git commit -m "feat(preflight): classify by record_type, drop substring fallback"
```

---

## Task 3: Extend TracePrepass with observability fields

**Files:**
- Modify: `src/bitgn_contest_agent/trace_schema.py` (TracePrepass)
- Modify: `src/bitgn_contest_agent/trace_writer.py` (append_prepass)
- Test: `tests/test_trace_writer.py` (update)

**Purpose:** Three optional fields on `TracePrepass`: `schema_roots`, `match_found`, `match_file`. All default `None` — additive-only per §6.5 schema evolution rule.

- [ ] **Step 1: Write the failing test**

Find `tests/test_trace_writer.py`. Add a new test:

```python
def test_append_prepass_accepts_schema_roots_and_match_fields(tmp_path):
    from bitgn_contest_agent.trace_writer import TraceWriter
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path=path)
    w.append_prepass(
        cmd="preflight_schema",
        ok=True,
        bytes=100,
        wall_ms=5,
        schema_roots={
            "projects_root": "40_projects",
            "finance_roots": ["50_finance/invoices"],
            "entities_root": "20_entities",
            "inbox_root": "00_inbox",
            "outbox_root": "60_outbox/outbox",
        },
    )
    w.append_prepass(
        cmd="routed_preflight_project",
        ok=True,
        bytes=200,
        match_found=True,
        match_file="40_projects/studio_parts_library/README.MD",
    )
    w.close()
    lines = path.read_text().splitlines()
    import json
    r1 = json.loads(lines[0])
    assert r1["schema_roots"]["projects_root"] == "40_projects"
    r2 = json.loads(lines[1])
    assert r2["match_found"] is True
    assert r2["match_file"] == "40_projects/studio_parts_library/README.MD"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trace_writer.py::test_append_prepass_accepts_schema_roots_and_match_fields -v`
Expected: FAIL with `TypeError: append_prepass() got an unexpected keyword argument`.

- [ ] **Step 3: Extend TracePrepass and append_prepass**

In `src/bitgn_contest_agent/trace_schema.py`, in the `TracePrepass` class after the last existing field, add:

```python
    # Preflight observability fields (2026-04-16).
    # Populated only on relevant cmds so post-run grep can confirm
    # classifier + routed-preflight behavior without reading tool payloads.
    schema_roots: Optional[dict[str, Any]] = None
    match_found: Optional[bool] = None
    match_file: Optional[str] = None
```

In `src/bitgn_contest_agent/trace_writer.py`, extend `append_prepass` signature and body:

```python
    def append_prepass(
        self,
        *,
        cmd: str,
        ok: bool,
        bytes: int = 0,
        wall_ms: int = 0,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        category: Optional[str] = None,
        query: Optional[str] = None,
        skipped_reason: Optional[str] = None,
        schema_roots: Optional[dict[str, Any]] = None,
        match_found: Optional[bool] = None,
        match_file: Optional[str] = None,
    ) -> None:
        rec = TracePrepass(
            cmd=cmd,
            ok=ok,
            bytes=bytes,
            wall_ms=wall_ms,
            error=error,
            error_code=error_code,
            category=category,
            query=query,
            skipped_reason=skipped_reason,
            schema_roots=schema_roots,
            match_found=match_found,
            match_file=match_file,
        )
        self._write(rec.model_dump(mode="json"))
```

Add `Any` to the `typing` import at the top of `trace_writer.py` if not already imported (it already is — verify).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trace_writer.py -v`
Expected: PASS (new test + all existing tests).

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/trace_schema.py src/bitgn_contest_agent/trace_writer.py tests/test_trace_writer.py
git commit -m "feat(trace): TracePrepass schema_roots/match_found/match_file"
```

---

## Task 4: Wire schema_roots into preflight_schema prepass

**Files:**
- Modify: `src/bitgn_contest_agent/adapter/pcm.py` (`run_prepass`)
- Test: inline check via existing integration tests + `tests/test_pcm_adapter.py` if present, else new targeted unit

**Purpose:** When `preflight_schema` runs, attach the parsed `WorkspaceSchema` roots to the prepass trace record so post-run grep can verify classifier correctness.

- [ ] **Step 1: Write failing test**

Append to `tests/test_trace_writer.py` OR create `tests/adapter/__init__.py` + `tests/adapter/test_pcm_prepass_schema_roots.py`. Use the latter — it keeps adapter tests together (and Task 6 adds another one).

Create `tests/adapter/__init__.py` (empty).

Create `tests/adapter/test_pcm_prepass_schema_roots.py`:

```python
"""Verify PcmAdapter.run_prepass attaches schema_roots to the
preflight_schema trace record."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from bitgn_contest_agent.adapter.pcm import PcmAdapter
from bitgn_contest_agent.trace_writer import TraceWriter


def _mk_adapter_with_stub_schema(tmp_path: Path):
    """Build a PcmAdapter whose runtime returns a canned schema."""
    runtime = MagicMock()
    # Short-circuit tree/read/context so only preflight_schema path is exercised.
    runtime.tree.return_value = MagicMock(root=MagicMock(name="", is_dir=True, children=[]))
    runtime.read.return_value = MagicMock(content="")
    runtime.context.return_value = MagicMock()
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=65536)
    return adapter


def test_run_prepass_attaches_schema_roots(tmp_path, monkeypatch):
    adapter = _mk_adapter_with_stub_schema(tmp_path)

    # Stub run_preflight_schema to return a known ToolResult.
    from bitgn_contest_agent.adapter.pcm import ToolResult
    canned_envelope = json.dumps({
        "summary": "ok",
        "data": {
            "inbox_root": "00_inbox",
            "entities_root": "20_entities",
            "finance_roots": ["50_finance/invoices"],
            "projects_root": "40_projects",
            "outbox_root": "60_outbox/outbox",
            "rulebook_root": None,
            "workflows_root": None,
            "schemas_root": None,
            "errors": [],
        },
    })
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.schema.run_preflight_schema",
        lambda client, ctx: ToolResult(
            ok=True, content=canned_envelope, refs=(), error=None,
            error_code=None, wall_ms=1,
        ),
    )

    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path=path)
    session = MagicMock()
    session.identity_loaded = False
    session.rulebook_loaded = False
    session.seen_refs = set()

    adapter.run_prepass(session=session, trace_writer=writer)
    writer.close()

    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    schema_recs = [r for r in records if r.get("cmd") == "preflight_schema"]
    assert schema_recs, "preflight_schema trace record missing"
    sr = schema_recs[0].get("schema_roots")
    assert sr is not None
    assert sr["projects_root"] == "40_projects"
    assert sr["finance_roots"] == ["50_finance/invoices"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/adapter/test_pcm_prepass_schema_roots.py -v`
Expected: FAIL — `schema_roots` is `None` on the record.

- [ ] **Step 3: Wire schema_roots in PcmAdapter.run_prepass**

In `src/bitgn_contest_agent/adapter/pcm.py`, modify the `run_prepass` method. Find:

```python
                trace_writer.append_prepass(
                    cmd=label,
                    ok=result.ok,
                    bytes=result.bytes,
                    wall_ms=result.wall_ms,
                    error=result.error,
                    error_code=result.error_code,
                )
```

Replace with:

```python
                schema_roots = None
                if label == "preflight_schema" and result.ok and result.content:
                    from bitgn_contest_agent.preflight.schema import parse_schema_content
                    parsed = parse_schema_content(result.content)
                    schema_roots = {
                        "projects_root": parsed.projects_root,
                        "finance_roots": list(parsed.finance_roots),
                        "entities_root": parsed.entities_root,
                        "inbox_root": parsed.inbox_root,
                        "outbox_root": parsed.outbox_root,
                    }
                trace_writer.append_prepass(
                    cmd=label,
                    ok=result.ok,
                    bytes=result.bytes,
                    wall_ms=result.wall_ms,
                    error=result.error,
                    error_code=result.error_code,
                    schema_roots=schema_roots,
                )
```

(The `parse_schema_content` import is already used at method bottom — reusing is cheap since it's module-local in `preflight.schema`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/adapter/test_pcm_prepass_schema_roots.py tests/ -x`
Expected: PASS for new test; full suite still green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/adapter/pcm.py tests/adapter/
git commit -m "feat(trace): wire schema_roots into preflight_schema prepass"
```

---

## Task 5: preflight_project — subdir recursion + refs + non-leaky summary

**Files:**
- Modify: `src/bitgn_contest_agent/preflight/project.py`
- Modify: `src/bitgn_contest_agent/agent.py` (`_dispatch_routed_preflight` — wire `match_found` + `match_file`)
- Test: `tests/preflight/test_project.py` (update)

**Purpose:**
1. Recurse into `<projects_root>/<slug>/README.MD` subdirs (Fix Z).
2. Use `parse_record_metadata` (Fix X consumer).
3. Return `refs=(matched_file,)` so the grader's attribution path fires (Fix B).
4. Non-leaky summary (Fix C) — no `start_date` in summary string.
5. Wire `match_found`/`match_file` into the routed-preflight trace emission (observability).

- [ ] **Step 1: Write failing tests**

Overwrite `tests/preflight/test_project.py` with:

```python
"""preflight_project tests — FS-based and PCM-mocked.

Covers DEV layout (flat `.md`) and PROD layout (`<slug>/README.MD`).
"""
from pathlib import Path
from unittest.mock import MagicMock

from bitgn_contest_agent.preflight.project import (
    run_preflight_project,
    run_project_from_fs,
)
from bitgn_contest_agent.schemas import Req_PreflightProject


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_ws"


def test_fs_project_resolves_name():
    out = run_project_from_fs(
        root=FIXTURE,
        projects_root="30_projects",
        entities_root="20_entities",
        query="Health Baseline",
    )
    assert out["project"] is not None
    assert out["project"]["name"] == "Health Baseline"


def test_fs_project_returns_start_date():
    out = run_project_from_fs(
        root=FIXTURE,
        projects_root="30_projects",
        entities_root="20_entities",
        query="Health Baseline",
    )
    assert out["project"]["start_date"] == "2025-11-14"


def test_fs_no_match_returns_none():
    out = run_project_from_fs(
        root=FIXTURE,
        projects_root="30_projects",
        entities_root="20_entities",
        query="Nonexistent Project",
    )
    assert out["project"] is None


# -- PCM-backed tests (the function used in prod) -----------------------


def _mk_runtime_for_prod_layout():
    """Simulate PROD layout: <projects_root>/<slug>/README.MD with
    bullet-list metadata."""
    runtime = MagicMock()
    slug_entries = [
        MagicMock(name="slug", is_dir=True),
    ]
    slug_entries[0].name = "studio_parts_library"  # attribute, not ctor arg
    runtime.list.return_value = MagicMock(entries=slug_entries)

    def _read(req):
        if req.path == "40_projects/studio_parts_library/README.MD":
            return MagicMock(content=(
                "# Studio Parts Library\n"
                "\n"
                "- record_type: project\n"
                "- project: Studio Parts Library\n"
                "- start_date: 2026-04-21\n"
                "- members: alice, bob\n"
            ))
        if req.path == "40_projects/studio_parts_library/README.md":
            raise FileNotFoundError(req.path)
        return MagicMock(content="")

    runtime.read.side_effect = _read
    return runtime


def test_pcm_prod_layout_returns_match_with_refs():
    runtime = _mk_runtime_for_prod_layout()
    req = Req_PreflightProject(
        tool="preflight_project",
        projects_root="40_projects",
        entities_root="20_entities",
        query="Studio Parts Library",
    )
    result = run_preflight_project(runtime, req)
    assert result.ok is True
    assert result.refs == ("40_projects/studio_parts_library/README.MD",)
    # Summary must cite the file, not leak the start_date.
    assert "Studio Parts Library" in result.content
    assert "40_projects/studio_parts_library/README.MD" in result.content
    assert "2026-04-21" not in _summary_line(result.content)


def _summary_line(envelope: str) -> str:
    """Extract the summary field from a build_response envelope."""
    import json
    return json.loads(envelope)["summary"]


def test_pcm_prod_layout_no_match_returns_empty_refs():
    runtime = _mk_runtime_for_prod_layout()
    req = Req_PreflightProject(
        tool="preflight_project",
        projects_root="40_projects",
        entities_root="20_entities",
        query="Nothing Here",
    )
    result = run_preflight_project(runtime, req)
    assert result.ok is True
    assert result.refs == ()
    assert "no project match" in _summary_line(result.content).lower()


def test_pcm_dev_layout_flat_md_still_works():
    """DEV layout: flat <projects_root>/*.md. Must still match."""
    runtime = MagicMock()
    flat_entry = MagicMock(is_dir=False)
    flat_entry.name = "health.md"
    runtime.list.return_value = MagicMock(entries=[flat_entry])
    runtime.read.return_value = MagicMock(content=(
        "---\n"
        "project: Health Baseline\n"
        "start_date: 2025-11-14\n"
        "---\n"
    ))
    req = Req_PreflightProject(
        tool="preflight_project",
        projects_root="30_projects",
        entities_root="20_entities",
        query="Health Baseline",
    )
    result = run_preflight_project(runtime, req)
    assert result.ok is True
    assert result.refs == ("30_projects/health.md",)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/preflight/test_project.py -v`
Expected: 3 new tests FAIL — current code returns `refs=()`, doesn't recurse into subdirs, and leaks start_date in summary.

- [ ] **Step 3: Rewrite `preflight/project.py`**

Replace full contents of `src/bitgn_contest_agent/preflight/project.py`:

```python
"""preflight_project — locates a project record and returns its
metadata + entities involved (members).

Handles both layouts:
  DEV: flat <projects_root>/*.md files
  PROD: nested <projects_root>/<slug>/README.MD files
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.canonicalize import normalize_name
from bitgn_contest_agent.preflight.response import build_response
from bitgn_contest_agent.preflight.schema import parse_record_metadata
from bitgn_contest_agent.schemas import Req_PreflightProject


def _find_project(projects_dir: Path, query: str) -> dict[str, Any] | None:
    if not projects_dir.exists():
        return None
    q_norm = normalize_name(query)
    for f in projects_dir.rglob("*.md"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        md = parse_record_metadata(text)
        pname = md.get("project", "")
        if pname and (
            normalize_name(pname) == q_norm
            or q_norm in normalize_name(pname)
            or normalize_name(pname) in q_norm
        ):
            return {
                "name": pname,
                "start_date": md.get("start_date", ""),
                "members": md.get("members", ""),
                "file": str(f),
                "frontmatter": md,
            }
    return None


def run_project_from_fs(
    root: Path, projects_root: str, entities_root: str, query: str,
) -> dict[str, Any]:
    root = Path(root)
    proj = _find_project(root / projects_root, query)
    return {"project": proj, "involved_entities": []}


def _candidate_paths(projects_root: str, entries: Iterable[Any]) -> list[str]:
    """Build ordered candidate paths to read.

    PROD: `<projects_root>/<slug>/README.MD` (uppercase) then README.md.
    DEV: flat `<projects_root>/<name>.md`.

    Returns the list; caller reads each, first successful parse with a
    matching `project` field wins.
    """
    out: list[str] = []
    for e in entries:
        if getattr(e, "is_dir", False):
            slug = e.name
            out.append(f"{projects_root}/{slug}/README.MD")
            out.append(f"{projects_root}/{slug}/README.md")
        else:
            if e.name.endswith(".md") or e.name.endswith(".MD"):
                out.append(f"{projects_root}/{e.name}")
    return out


def run_preflight_project(client: Any, req: Req_PreflightProject) -> ToolResult:
    from bitgn.vm import pcm_pb2
    try:
        q_norm = normalize_name(req.query)
        found = None
        lresp = client.list(pcm_pb2.ListRequest(name=req.projects_root))
        for fp in _candidate_paths(req.projects_root, lresp.entries):
            try:
                rr = client.read(pcm_pb2.ReadRequest(path=fp))
            except Exception:
                # Missing README.md after README.MD tried, etc.
                continue
            md = parse_record_metadata(rr.content)
            pname = md.get("project", "")
            if pname and (
                normalize_name(pname) == q_norm
                or q_norm in normalize_name(pname)
                or normalize_name(pname) in q_norm
            ):
                found = {
                    "name": pname,
                    "start_date": md.get("start_date", ""),
                    "members": md.get("members", ""),
                    "file": fp,
                    "frontmatter": md,
                }
                break
        data = {"project": found, "involved_entities": []}
    except Exception as exc:
        return ToolResult(
            ok=False, content="", refs=tuple(),
            error=f"preflight_project failed: {exc}",
            error_code="INTERNAL", wall_ms=0,
        )
    if found:
        # Non-leaky summary — cite the file instead of the value so the
        # agent is pressured to read it (grader enforces attribution).
        summary = f"Project '{found['name']}' found at {found['file']}."
        refs: tuple[str, ...] = (found["file"],)
    else:
        summary = f"Query '{req.query}' → no project match."
        refs = ()
    return ToolResult(
        ok=True, content=build_response(summary=summary, data=data),
        refs=refs, error=None, error_code=None, wall_ms=0,
    )
```

Now delete the back-compat shim `_parse_frontmatter = parse_record_metadata` added in Task 2 (it's no longer needed). Keep the function `_parse_frontmatter_yaml` — it's an internal helper referenced only inside `parse_record_metadata`.

- [ ] **Step 4: Run preflight tests**

Run: `uv run pytest tests/preflight/ -v`
Expected: All pass.

- [ ] **Step 5: Wire match_found/match_file in agent._dispatch_routed_preflight**

In `src/bitgn_contest_agent/agent.py`, find the `self._writer.append_prepass(cmd=f"routed_{tool_label}", ...)` call in `_dispatch_routed_preflight`. Extend it to include `match_found` and `match_file` derived from `result.refs`:

```python
        if outcome.tool is not None or outcome.skipped_reason is not None:
            result = outcome.result
            tool_label = outcome.tool or "unknown"
            match_found = None
            match_file = None
            if result is not None and result.ok:
                # Non-empty refs ≡ preflight found something to cite.
                # Empty refs ≡ query ran but no match.
                match_found = bool(result.refs)
                if result.refs:
                    match_file = result.refs[0]
            self._writer.append_prepass(
                cmd=f"routed_{tool_label}",
                ok=bool(result.ok) if result is not None else False,
                bytes=result.bytes if result is not None else 0,
                wall_ms=result.wall_ms if result is not None else 0,
                error=(
                    outcome.error
                    or (result.error if result is not None else None)
                ),
                error_code=(
                    result.error_code if result is not None else None
                ),
                category=category,
                query=query or None,
                skipped_reason=outcome.skipped_reason,
                match_found=match_found,
                match_file=match_file,
            )
```

- [ ] **Step 6: Run full suite**

Run: `uv run pytest tests/ -x`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add src/bitgn_contest_agent/preflight/project.py src/bitgn_contest_agent/preflight/schema.py src/bitgn_contest_agent/agent.py tests/preflight/test_project.py
git commit -m "feat(preflight): project subdir recursion + refs attribution + non-leaky summary"
```

---

## Task 6: Pre-write YAML validation in PcmAdapter

**Files:**
- Modify: `src/bitgn_contest_agent/arch_constants.py` (add enum member)
- Modify: `src/bitgn_contest_agent/adapter/pcm.py` (Req_Write branch in `dispatch`)
- Test: `tests/adapter/test_pcm_write_validation.py` (create)

**Purpose:** Reject writes with malformed YAML frontmatter BEFORE dispatching to PCM. Emits `FORMAT_PRE_WRITE_REJECT` arch event. No persisted mutation ⇒ agent rewrites exactly once ⇒ grader mutation count stays at 1.

- [ ] **Step 1: Write failing tests**

Create `tests/adapter/test_pcm_write_validation.py`:

```python
"""Pre-write YAML frontmatter validation — rejects bad writes before
dispatching to PCM, preventing duplicate-write grader violations."""
from __future__ import annotations

from unittest.mock import MagicMock

from bitgn_contest_agent.adapter.pcm import PcmAdapter
from bitgn_contest_agent.schemas import Req_Write


def _mk_adapter():
    runtime = MagicMock()
    runtime.write.return_value = MagicMock()
    return runtime, PcmAdapter(runtime=runtime, max_tool_result_bytes=65536)


def test_valid_yaml_frontmatter_dispatches_to_pcm():
    runtime, adapter = _mk_adapter()
    content = (
        "---\n"
        "record_type: outbound_email\n"
        "subject: Hello world\n"
        "---\n"
        "Body.\n"
    )
    req = Req_Write(tool="write", path="60_outbox/test.md", content=content)
    result = adapter.dispatch(req)
    assert result.ok is True
    runtime.write.assert_called_once()


def test_invalid_yaml_frontmatter_rejected_before_dispatch():
    runtime, adapter = _mk_adapter()
    # Unquoted colon in `subject:` value — the t071 regression.
    content = (
        "---\n"
        "record_type: outbound_email\n"
        "subject: Re: Invoice request\n"
        "---\n"
        "Body.\n"
    )
    req = Req_Write(tool="write", path="60_outbox/test.md", content=content)
    result = adapter.dispatch(req)
    assert result.ok is False
    assert result.error_code == "FORMAT_INVALID"
    assert "YAML" in (result.error or "")
    runtime.write.assert_not_called()


def test_content_without_frontmatter_dispatches_without_validation():
    runtime, adapter = _mk_adapter()
    req = Req_Write(tool="write", path="note.md", content="Just prose, no YAML.\n")
    result = adapter.dispatch(req)
    assert result.ok is True
    runtime.write.assert_called_once()


def test_format_pre_write_reject_arch_emitted(monkeypatch):
    """Verify a FORMAT_PRE_WRITE_REJECT arch event is emitted on reject."""
    import bitgn_contest_agent.adapter.pcm as pcm_mod
    captured = []

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(pcm_mod, "emit_arch", _capture)

    runtime, adapter = _mk_adapter()
    content = (
        "---\n"
        "subject: Re: broken\n"
        "---\n"
    )
    req = Req_Write(tool="write", path="60_outbox/bad.md", content=content)
    result = adapter.dispatch(req)
    assert result.ok is False
    assert any(
        str(k.get("category")) == "FORMAT_PRE_WRITE_REJECT"
        for k in captured
    ), f"no FORMAT_PRE_WRITE_REJECT emitted, got: {captured}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/adapter/test_pcm_write_validation.py -v`
Expected: FAIL — no pre-write validation exists, `emit_arch` not imported in pcm.py, enum member missing.

- [ ] **Step 3: Add the arch enum member**

In `src/bitgn_contest_agent/arch_constants.py`, append to `ArchCategory`:

```python
    FORMAT_PRE_WRITE_REJECT = "FORMAT_PRE_WRITE_REJECT"
```

- [ ] **Step 4: Add pre-write validation to PcmAdapter.dispatch**

In `src/bitgn_contest_agent/adapter/pcm.py`, add imports near the top (after existing imports):

```python
from bitgn_contest_agent.arch_constants import ArchCategory
from bitgn_contest_agent.arch_log import emit_arch
from bitgn_contest_agent.format_validator import validate_yaml_frontmatter
```

(Verify `arch_log.emit_arch` signature matches `agent.py`'s call — it does: `emit_arch(category=..., at_step=..., details=...)`.)

In `dispatch`, replace the `Req_Write` branch:

```python
            if isinstance(req, Req_Write):
                # Pre-write YAML guard — catches malformed frontmatter
                # BEFORE persistence so the agent can fix-and-retry
                # without accumulating a duplicate-write mutation that
                # the grader flags. Post-write FORMAT_VALIDATOR remains
                # as belt-and-suspenders for non-YAML format issues.
                val = validate_yaml_frontmatter(req.content)
                if not val.ok:
                    emit_arch(
                        category=ArchCategory.FORMAT_PRE_WRITE_REJECT,
                        at_step=None,
                        details=f"path={req.path} error={val.error}",
                    )
                    wall_ms = int((time.monotonic() - start) * 1000)
                    return ToolResult(
                        ok=False,
                        content="",
                        refs=(),
                        error=f"YAML frontmatter parse error: {val.error}",
                        error_code="FORMAT_INVALID",
                        wall_ms=wall_ms,
                    )
                resp = self._runtime.write(
                    pcm_pb2.WriteRequest(path=req.path, content=req.content)
                )
                return self._finish(start, resp, refs=())
```

- [ ] **Step 5: Run adapter tests**

Run: `uv run pytest tests/adapter/ -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Run full suite**

Run: `uv run pytest tests/ -x`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add src/bitgn_contest_agent/arch_constants.py src/bitgn_contest_agent/adapter/pcm.py tests/adapter/test_pcm_write_validation.py
git commit -m "feat(adapter): pre-write YAML frontmatter guard + FORMAT_PRE_WRITE_REJECT"
```

---

## Final verification + push

- [ ] **Step 1: Full test suite**

Run: `uv run pytest tests/ -x`
Expected: All pass, zero failures.

- [ ] **Step 2: Push branch**

```bash
git push origin feat/r4-validator-correctness
```

- [ ] **Step 3: Smoke test on PROD — t001 pattern**

Use the rerun-failing-tasks CLI (per `reference_cli_syntax.md` memory) to run a single task matching the `project_start_date` template. Verify in the trace:

```bash
jq -c 'select(.kind=="prepass" and .cmd=="preflight_schema") | .schema_roots' logs/<latest>/t*.jsonl
# Expect: {"projects_root": "40_projects", ...}

jq -c 'select(.kind=="prepass" and .cmd=="routed_preflight_project") | {match_found, match_file}' logs/<latest>/t*.jsonl
# Expect: {"match_found": true, "match_file": "40_projects/<slug>/README.MD"}
```

Grader score should be 1.0.

- [ ] **Step 4: Full p3i6 PROD bench**

Launch full 104-task run. Compare by intent template (per `feedback_task_compare_by_intent` memory), not task ID. Expected delta: +2 vs baseline 102/104 (ideally 104/104). Zero regressions — fixes only touch previously-failing paths.

---

## Risks

- **Parser may miss encoding edge cases** — PROD dataset might have encodings we didn't fixture. Mitigation: `parse_record_metadata` returns `{}` on unknown shapes (same failure surface as today — "no match").
- **Pre-write rejects too much** — if the validator disagrees with PCM's parser on edge cases, agent gets stuck retrying. Mitigation: `format_validator.validate_yaml_frontmatter` is the same function already used post-write without issue on >100 bench runs.
- **Observability fields miss old readers** — any analyser using strict Pydantic parsing on pre-schema-v1.0.0 traces would break. Mitigation: `TraceRecord` uses `extra="ignore"` + all new fields are Optional=None per §6.5.
