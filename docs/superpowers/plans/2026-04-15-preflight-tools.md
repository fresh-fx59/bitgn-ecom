# Preflight Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add six preflight tools (`discover_workspace_schema` + 5 specialized) and a harness-level hard gate that forces the agent to run preflight before issuing non-trivial reads/searches/mutations.

**Architecture:** Preflight tools are new `Req_Preflight*` schema variants added to `FunctionUnion`. The PCM adapter dispatches them to a new `preflight/` module that composes existing PCM RPCs (read/list/find/search) to produce `{summary, data}` responses. A harness-level gate inspects the first non-whitelisted tool call in each task; if no preflight has fired yet, it rejects the call with a retry message (no step cost).

**Tech Stack:** Python 3, Pydantic v2, pytest. Existing: `bitgn.vm.pcm_pb2` / `PcmRuntimeClientSync`. No new runtime deps.

---

## File Structure

**New files:**
- `src/bitgn_contest_agent/preflight/__init__.py` — package marker, re-exports
- `src/bitgn_contest_agent/preflight/schema.py` — `WorkspaceSchema` dataclass + `discover_workspace_schema` impl
- `src/bitgn_contest_agent/preflight/canonicalize.py` — entity/vendor alias matching helpers
- `src/bitgn_contest_agent/preflight/response.py` — `{summary, data}` response builder
- `src/bitgn_contest_agent/preflight/inbox.py` — `preflight_inbox`
- `src/bitgn_contest_agent/preflight/finance.py` — `preflight_finance`
- `src/bitgn_contest_agent/preflight/entity.py` — `preflight_entity`
- `src/bitgn_contest_agent/preflight/project.py` — `preflight_project`
- `src/bitgn_contest_agent/preflight/doc_migration.py` — `preflight_doc_migration`
- `tests/preflight/__init__.py`
- `tests/preflight/test_schema.py`
- `tests/preflight/test_canonicalize.py`
- `tests/preflight/test_inbox.py`
- `tests/preflight/test_finance.py`
- `tests/preflight/test_entity.py`
- `tests/preflight/test_project.py`
- `tests/preflight/test_doc_migration.py`
- `tests/test_harness_gate.py`

**Modified files:**
- `src/bitgn_contest_agent/schemas.py` — add 6 `Req_Preflight*` classes, extend `FunctionUnion`, add to `REQ_MODELS`
- `src/bitgn_contest_agent/adapter/pcm.py` — dispatch branches for each preflight tool
- `src/bitgn_contest_agent/agent.py` — harness gate check before `self._adapter.dispatch(fn)` at line ~444
- `src/bitgn_contest_agent/prompts.py` — append preflight protocol instruction to the agent system prompt

---

## Task 1: Skeleton — schemas, canonicalize, response builder, dispatch shell

**Goal:** Wire `Req_Preflight*` through schema union and adapter so new tools are callable (even if they return stubs). Land shared helpers.

**Files:**
- Create: `src/bitgn_contest_agent/preflight/__init__.py`
- Create: `src/bitgn_contest_agent/preflight/canonicalize.py`
- Create: `src/bitgn_contest_agent/preflight/response.py`
- Create: `tests/preflight/__init__.py`
- Create: `tests/preflight/test_canonicalize.py`
- Modify: `src/bitgn_contest_agent/schemas.py`
- Modify: `src/bitgn_contest_agent/adapter/pcm.py`

- [ ] **Step 1: Write failing test for canonicalize helpers**

Create `tests/preflight/test_canonicalize.py`:

```python
from bitgn_contest_agent.preflight.canonicalize import (
    normalize_name,
    score_match,
)


def test_normalize_name_strips_punctuation_and_lowercases():
    assert normalize_name("  Harbor Body!  ") == "harbor body"
    assert normalize_name("深圳市海云电子") == "深圳市海云电子"


def test_score_match_exact():
    assert score_match("Harbor Body", ["Harbor Body"]) == 1.0


def test_score_match_alias():
    assert score_match("walking buddy", ["Harbor Body", "walking buddy"]) == 1.0


def test_score_match_case_insensitive():
    assert score_match("HARBOR BODY", ["Harbor Body"]) == 1.0


def test_score_match_no_match():
    assert score_match("nonexistent", ["Harbor Body"]) == 0.0
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/preflight/test_canonicalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitgn_contest_agent.preflight'`

- [ ] **Step 3: Create preflight package and canonicalize module**

Create `src/bitgn_contest_agent/preflight/__init__.py`:

```python
"""Client-side preflight tools. Each tool composes existing PCM RPCs
(read/list/find/search) into a {summary, data} response that helps the
agent plan before acting.
"""
```

Create `src/bitgn_contest_agent/preflight/canonicalize.py`:

```python
"""Shared canonicalization helpers for preflight tools.

Entity/vendor/project names in the workspace vary by case, punctuation,
and aliasing. These helpers produce a normalized key plus a simple
match score across candidate names/aliases.
"""
from __future__ import annotations

import re
from typing import Iterable


_PUNCT_RE = re.compile(r"[^\w\s\u4e00-\u9fff]+", re.UNICODE)


def normalize_name(name: str) -> str:
    """Lowercase, strip, collapse whitespace, drop non-word non-CJK chars."""
    if not name:
        return ""
    cleaned = _PUNCT_RE.sub(" ", name)
    return " ".join(cleaned.lower().split())


def score_match(query: str, candidates: Iterable[str]) -> float:
    """Return best match score in [0.0, 1.0] of query against candidates.

    Current rule: exact normalized match = 1.0, else 0.0. Intentionally
    narrow; we'll broaden with fuzzy matching only if bench shows need.
    """
    q = normalize_name(query)
    if not q:
        return 0.0
    for cand in candidates:
        if normalize_name(cand) == q:
            return 1.0
    return 0.0
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/preflight/test_canonicalize.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Write failing test for response builder**

Append to `tests/preflight/test_canonicalize.py` — no, new file. Create `tests/preflight/test_response.py`:

```python
from bitgn_contest_agent.preflight.response import build_response


def test_build_response_shape():
    out = build_response(summary="hello", data={"a": 1})
    assert out == '{"summary": "hello", "data": {"a": 1}}'


def test_build_response_unicode():
    out = build_response(summary="深圳市", data={"k": "深圳市海云电子"})
    # Must be JSON-decodable unicode (no ascii-escape).
    assert "深圳市" in out
```

- [ ] **Step 6: Run test to verify failure**

Run: `uv run pytest tests/preflight/test_response.py -v`
Expected: FAIL — module missing

- [ ] **Step 7: Implement response builder**

Create `src/bitgn_contest_agent/preflight/response.py`:

```python
"""Uniform {summary, data} JSON response shape for all preflight tools."""
from __future__ import annotations

import json
from typing import Any


def build_response(summary: str, data: dict[str, Any]) -> str:
    """Serialize a preflight response as compact JSON with unicode preserved."""
    return json.dumps(
        {"summary": summary, "data": data},
        ensure_ascii=False,
        separators=(", ", ": "),
    )
```

- [ ] **Step 8: Run test to verify pass**

Run: `uv run pytest tests/preflight/test_response.py -v`
Expected: PASS (2 tests)

- [ ] **Step 9: Add preflight Req_* schemas to schemas.py**

Modify `src/bitgn_contest_agent/schemas.py`. After the existing `Req_Context` class (around line 69), add:

```python
class Req_PreflightSchema(BaseModel):
    """Discover the workspace layout (roots and roles). Always safe to call."""
    tool: Literal["preflight_schema"]


class Req_PreflightInbox(BaseModel):
    """Enumerate open inbox items with referenced entities and related finance files."""
    tool: Literal["preflight_inbox"]
    inbox_root: NonEmptyStr
    entities_root: NonEmptyStr
    finance_roots: Annotated[List[NonEmptyStr], Field(min_length=1)]


class Req_PreflightFinance(BaseModel):
    """Canonicalize a finance query and enumerate matching purchase/invoice files."""
    tool: Literal["preflight_finance"]
    finance_roots: Annotated[List[NonEmptyStr], Field(min_length=1)]
    entities_root: NonEmptyStr
    query: NonEmptyStr


class Req_PreflightEntity(BaseModel):
    """Disambiguate an entity query against entity records and aliases."""
    tool: Literal["preflight_entity"]
    entities_root: NonEmptyStr
    query: NonEmptyStr


class Req_PreflightProject(BaseModel):
    """Look up a project record and the entities involved."""
    tool: Literal["preflight_project"]
    projects_root: NonEmptyStr
    entities_root: NonEmptyStr
    query: NonEmptyStr


class Req_PreflightDocMigration(BaseModel):
    """Resolve the migration destination for a set of documents."""
    tool: Literal["preflight_doc_migration"]
    source_paths: Annotated[List[NonEmptyStr], Field(min_length=1)]
    entities_root: NonEmptyStr
    query: NonEmptyStr
```

Then extend `FunctionUnion` (around line 88) — add the six new classes to the list:

```python
FunctionUnion = Annotated[
    Union[
        Req_Read,
        Req_Write,
        Req_Delete,
        Req_MkDir,
        Req_Move,
        Req_List,
        Req_Tree,
        Req_Find,
        Req_Search,
        Req_Context,
        Req_PreflightSchema,
        Req_PreflightInbox,
        Req_PreflightFinance,
        Req_PreflightEntity,
        Req_PreflightProject,
        Req_PreflightDocMigration,
        ReportTaskCompletion,
    ],
    Field(discriminator="tool"),
]
```

Extend `REQ_MODELS` (around line 120):

```python
REQ_MODELS: tuple[type[BaseModel], ...] = (
    Req_Read,
    Req_Write,
    Req_Delete,
    Req_MkDir,
    Req_Move,
    Req_List,
    Req_Tree,
    Req_Find,
    Req_Search,
    Req_Context,
    Req_PreflightSchema,
    Req_PreflightInbox,
    Req_PreflightFinance,
    Req_PreflightEntity,
    Req_PreflightProject,
    Req_PreflightDocMigration,
)
```

- [ ] **Step 10: Add dispatch stubs to adapter/pcm.py**

Modify `src/bitgn_contest_agent/adapter/pcm.py`. Update the imports block to include the new Req_ types:

```python
from bitgn_contest_agent.schemas import (
    NextStep,
    ReportTaskCompletion,
    Req_Context,
    Req_Delete,
    Req_Find,
    Req_List,
    Req_MkDir,
    Req_Move,
    Req_Read,
    Req_Search,
    Req_Tree,
    Req_Write,
    Req_PreflightSchema,
    Req_PreflightInbox,
    Req_PreflightFinance,
    Req_PreflightEntity,
    Req_PreflightProject,
    Req_PreflightDocMigration,
)
```

In the `dispatch` method, after the existing `Req_Context` branch (around line 170), add before the final `else` (or the raise):

```python
            if isinstance(req, Req_PreflightSchema):
                from bitgn_contest_agent.preflight.schema import run_preflight_schema
                return run_preflight_schema(self._client, self._workspace_context)
            if isinstance(req, Req_PreflightInbox):
                from bitgn_contest_agent.preflight.inbox import run_preflight_inbox
                return run_preflight_inbox(self._client, req)
            if isinstance(req, Req_PreflightFinance):
                from bitgn_contest_agent.preflight.finance import run_preflight_finance
                return run_preflight_finance(self._client, req)
            if isinstance(req, Req_PreflightEntity):
                from bitgn_contest_agent.preflight.entity import run_preflight_entity
                return run_preflight_entity(self._client, req)
            if isinstance(req, Req_PreflightProject):
                from bitgn_contest_agent.preflight.project import run_preflight_project
                return run_preflight_project(self._client, req)
            if isinstance(req, Req_PreflightDocMigration):
                from bitgn_contest_agent.preflight.doc_migration import run_preflight_doc_migration
                return run_preflight_doc_migration(self._client, req)
```

Note: check the current PcmAdapter `__init__` for the exact attribute name of the PCM client (`self._client` is typical). If it's `self._runtime` or something else, use that. The implementer should read lines 100-130 of `adapter/pcm.py` to confirm.

- [ ] **Step 11: Run existing adapter tests to verify nothing broken**

Run: `uv run pytest tests/ -x -q 2>&1 | tail -40`
Expected: existing tests pass; preflight tests not yet added may show import failures from step 10 imports — that's OK because dispatch branches lazy-import. If collection fails, fix imports.

- [ ] **Step 12: Commit**

```bash
git add src/bitgn_contest_agent/preflight/__init__.py \
        src/bitgn_contest_agent/preflight/canonicalize.py \
        src/bitgn_contest_agent/preflight/response.py \
        src/bitgn_contest_agent/schemas.py \
        src/bitgn_contest_agent/adapter/pcm.py \
        tests/preflight/__init__.py \
        tests/preflight/test_canonicalize.py \
        tests/preflight/test_response.py
git commit -m "feat(preflight): schema union + dispatch shell + canonicalize/response helpers"
```

---

## Task 2: `discover_workspace_schema` — role-tagged root discovery

**Goal:** Implement the discovery tool that crawls the workspace and tags directory roles by frontmatter signatures.

**Files:**
- Create: `src/bitgn_contest_agent/preflight/schema.py`
- Create: `tests/preflight/test_schema.py`
- Create: `tests/preflight/fixtures/` (test workspace)

- [ ] **Step 1: Build a tiny test workspace fixture**

Create `tests/preflight/fixtures/tiny_ws/00_inbox/task_a.md`:

```markdown
---
inbox_type: ocr_verification
---
Please OCR the Juniper bill.
```

Create `tests/preflight/fixtures/tiny_ws/20_entities/juniper.md`:

```markdown
---
aliases: ["Juniper Systems"]
role: vendor
---
```

Create `tests/preflight/fixtures/tiny_ws/50_finance/purchases/bill_001.md`:

```markdown
---
vendor: Juniper Systems
eur_total: 50
line_items: ["sensor bundle"]
---
```

Create `tests/preflight/fixtures/tiny_ws/30_projects/health.md`:

```markdown
---
project: Health Baseline
start_date: 2025-11-14
members: []
---
```

Create `tests/preflight/fixtures/tiny_ws/60_outbox/outbox/eml_001.md`:

```markdown
---
to: miles@example.com
subject: "Re: invoices"
body: "See attached."
---
```

- [ ] **Step 2: Write failing test for discover_workspace_schema**

Create `tests/preflight/test_schema.py`:

```python
import json
from pathlib import Path

from bitgn_contest_agent.preflight.schema import (
    WorkspaceSchema,
    discover_schema_from_fs,
)


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_ws"


def test_discover_schema_identifies_all_roots():
    schema = discover_schema_from_fs(FIXTURE)
    assert schema.inbox_root == "00_inbox"
    assert schema.entities_root == "20_entities"
    assert "50_finance/purchases" in schema.finance_roots
    assert schema.projects_root == "30_projects"
    assert schema.outbox_root == "60_outbox/outbox"


def test_schema_summary_mentions_each_role():
    schema = discover_schema_from_fs(FIXTURE)
    s = schema.summary()
    assert "inbox" in s.lower()
    assert "finance" in s.lower()
    assert "entit" in s.lower()
    assert "project" in s.lower()
    assert "outbox" in s.lower()


def test_schema_as_data_dict_roundtrips_json():
    schema = discover_schema_from_fs(FIXTURE)
    data = schema.as_data()
    # Must be JSON serializable
    json.dumps(data)
    assert data["inbox_root"] == "00_inbox"
```

- [ ] **Step 3: Run test to verify failure**

Run: `uv run pytest tests/preflight/test_schema.py -v`
Expected: FAIL — `ImportError: cannot import name 'WorkspaceSchema'`

- [ ] **Step 4: Implement schema discovery**

Create `src/bitgn_contest_agent/preflight/schema.py`:

```python
"""Workspace role discovery — identifies which directories hold inbox,
entities, finance, projects, outbox, rulebook, workflows, schemas by
inspecting frontmatter signatures of the files inside.

Path-agnostic: no directory name is hardcoded. Discovery is by content
signature only.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.response import build_response


_LOG = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Minimum fraction of files in a directory that must match the signature
# for the directory to be tagged with that role.
_MATCH_THRESHOLD = 0.3


@dataclass
class WorkspaceSchema:
    inbox_root: Optional[str] = None
    entities_root: Optional[str] = None
    finance_roots: List[str] = field(default_factory=list)
    projects_root: Optional[str] = None
    outbox_root: Optional[str] = None
    rulebook_root: Optional[str] = None
    workflows_root: Optional[str] = None
    schemas_root: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.inbox_root:
            parts.append(f"inbox at {self.inbox_root}")
        if self.entities_root:
            parts.append(f"entities at {self.entities_root}")
        if self.finance_roots:
            parts.append(f"{len(self.finance_roots)} finance root(s)")
        if self.projects_root:
            parts.append(f"projects at {self.projects_root}")
        if self.outbox_root:
            parts.append(f"outbox at {self.outbox_root}")
        extras = [r for r in (self.rulebook_root, self.workflows_root, self.schemas_root) if r]
        if extras:
            parts.append(f"{len(extras)} doc root(s)")
        return "Workspace schema: " + ", ".join(parts) + "."

    def as_data(self) -> dict[str, Any]:
        return {
            "inbox_root": self.inbox_root,
            "entities_root": self.entities_root,
            "finance_roots": self.finance_roots,
            "projects_root": self.projects_root,
            "outbox_root": self.outbox_root,
            "rulebook_root": self.rulebook_root,
            "workflows_root": self.workflows_root,
            "schemas_root": self.schemas_root,
            "errors": self.errors,
        }


def _parse_frontmatter(text: str) -> dict[str, str]:
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


def _classify_dir(frontmatters: list[dict[str, str]]) -> list[str]:
    """Return a list of role labels this directory's contents match.

    A directory can have multiple roles only if more than one signature
    trips the threshold — in practice each dir gets one dominant role.
    """
    if not frontmatters:
        return []
    n = len(frontmatters)

    def frac(pred) -> float:
        return sum(1 for fm in frontmatters if pred(fm)) / n

    roles = []
    if frac(lambda fm: "inbox_type" in fm or "inbox_kind" in fm) >= _MATCH_THRESHOLD:
        roles.append("inbox")
    if frac(lambda fm: "aliases" in fm or ("role" in fm and "relationship" not in fm) or "relationship" in fm) >= _MATCH_THRESHOLD:
        roles.append("entities")
    if frac(lambda fm: "vendor" in fm or "eur_total" in fm or "line_items" in fm) >= _MATCH_THRESHOLD:
        roles.append("finance")
    if frac(lambda fm: "project" in fm and ("start_date" in fm or "members" in fm)) >= _MATCH_THRESHOLD:
        roles.append("projects")
    if frac(lambda fm: "to" in fm and "subject" in fm) >= _MATCH_THRESHOLD:
        roles.append("outbox")
    return roles


def discover_schema_from_fs(root: Path) -> WorkspaceSchema:
    """Filesystem-based discovery — used for local tests and as the
    core implementation that the PCM-backed version wraps.
    """
    schema = WorkspaceSchema()
    root = Path(root)
    if not root.exists():
        schema.errors.append(f"root does not exist: {root}")
        return schema

    for dirpath in sorted(p for p in root.rglob("*") if p.is_dir()):
        md_files = [f for f in dirpath.iterdir() if f.is_file() and f.suffix == ".md"]
        if not md_files:
            continue
        frontmatters = []
        for f in md_files[:50]:  # cap per dir for speed
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                frontmatters.append(_parse_frontmatter(text))
            except OSError as exc:
                schema.errors.append(f"read failed {f}: {exc}")

        roles = _classify_dir(frontmatters)
        rel = str(dirpath.relative_to(root))
        for role in roles:
            if role == "inbox" and schema.inbox_root is None:
                schema.inbox_root = rel
            elif role == "entities" and schema.entities_root is None:
                schema.entities_root = rel
            elif role == "finance":
                if rel not in schema.finance_roots:
                    schema.finance_roots.append(rel)
            elif role == "projects" and schema.projects_root is None:
                schema.projects_root = rel
            elif role == "outbox" and schema.outbox_root is None:
                schema.outbox_root = rel

    return schema


def run_preflight_schema(client: Any, workspace_ctx: Any) -> ToolResult:
    """PCM-backed entry point. Walks the workspace via the PCM list/tree
    RPC, parses frontmatters via read RPC, returns a ToolResult.

    `workspace_ctx` carries the root path or handle the adapter uses to
    talk to PCM. For the PCM client the adapter will pass `client`'s own
    workspace root.
    """
    from bitgn.vm import pcm_pb2  # local import to keep schema module light

    schema = WorkspaceSchema()
    try:
        # Tree walk from root. Depth cap prevents runaway on big workspaces.
        req = pcm_pb2.TreeRequest(path="", max_depth=4)
        tree_resp = client.Tree(req)
        dirs = sorted({entry.path.rsplit("/", 1)[0] for entry in tree_resp.entries if entry.path.endswith(".md")})
        for d in dirs:
            if not d:
                continue
            list_resp = client.List(pcm_pb2.ListRequest(path=d))
            md_names = [e.name for e in list_resp.entries if e.name.endswith(".md")][:50]
            frontmatters = []
            for name in md_names:
                read_resp = client.Read(pcm_pb2.ReadRequest(path=f"{d}/{name}"))
                frontmatters.append(_parse_frontmatter(read_resp.content))
            roles = _classify_dir(frontmatters)
            for role in roles:
                if role == "inbox" and schema.inbox_root is None:
                    schema.inbox_root = d
                elif role == "entities" and schema.entities_root is None:
                    schema.entities_root = d
                elif role == "finance":
                    if d not in schema.finance_roots:
                        schema.finance_roots.append(d)
                elif role == "projects" and schema.projects_root is None:
                    schema.projects_root = d
                elif role == "outbox" and schema.outbox_root is None:
                    schema.outbox_root = d
    except Exception as exc:
        schema.errors.append(f"pcm walk failed: {exc}")

    content = build_response(summary=schema.summary(), data=schema.as_data())
    return ToolResult(
        ok=True,
        content=content,
        refs=tuple(),
        error=None,
        error_code=None,
        wall_ms=0,
    )
```

Update `dispatch` in `adapter/pcm.py` to pass the PCM client + workspace context:

```python
            if isinstance(req, Req_PreflightSchema):
                from bitgn_contest_agent.preflight.schema import run_preflight_schema
                return run_preflight_schema(self._client, None)
```

(The workspace context argument is a future-proofing hook; pass `None` for now.)

- [ ] **Step 5: Run test to verify pass**

Run: `uv run pytest tests/preflight/test_schema.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/preflight/schema.py \
        src/bitgn_contest_agent/adapter/pcm.py \
        tests/preflight/test_schema.py \
        tests/preflight/fixtures/
git commit -m "feat(preflight): discover_workspace_schema — role-tagged root discovery"
```

---

## Task 3: `preflight_inbox` — entity→bills graph traversal

**Goal:** The highest-value preflight. Fixes t016/t041/t066/t091 by enumerating ALL finance files linked to entities referenced in open inbox items.

**Files:**
- Create: `src/bitgn_contest_agent/preflight/inbox.py`
- Create: `tests/preflight/test_inbox.py`
- Extend fixtures: add second Juniper bill and a Juniper alias

- [ ] **Step 1: Extend test fixture with multi-bill entity**

Add `tests/preflight/fixtures/tiny_ws/20_entities/juniper.md` (overwrite):

```markdown
---
aliases: ["Juniper Systems", "House Mesh"]
role: vendor
---
```

Add `tests/preflight/fixtures/tiny_ws/50_finance/purchases/bill_002.md`:

```markdown
---
vendor: House Mesh
eur_total: 105
line_items: ["juniper ssd"]
---
```

- [ ] **Step 2: Write failing test**

Create `tests/preflight/test_inbox.py`:

```python
from pathlib import Path

from bitgn_contest_agent.preflight.inbox import enumerate_inbox_from_fs


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_ws"


def test_enumerate_finds_open_inbox_item():
    items = enumerate_inbox_from_fs(
        root=FIXTURE,
        inbox_root="00_inbox",
        entities_root="20_entities",
        finance_roots=["50_finance/purchases"],
    )
    assert len(items) == 1
    item = items[0]
    assert item["path"].endswith("task_a.md")


def test_item_resolves_entity_via_alias():
    items = enumerate_inbox_from_fs(
        root=FIXTURE,
        inbox_root="00_inbox",
        entities_root="20_entities",
        finance_roots=["50_finance/purchases"],
    )
    item = items[0]
    # "Juniper" in task body should canonicalize to the juniper.md entity.
    assert item["entity_canonical"] is not None


def test_item_lists_all_bills_for_entity():
    items = enumerate_inbox_from_fs(
        root=FIXTURE,
        inbox_root="00_inbox",
        entities_root="20_entities",
        finance_roots=["50_finance/purchases"],
    )
    item = items[0]
    # Juniper has aliases Juniper Systems + House Mesh → 2 bills expected.
    assert len(item["related_finance_files"]) == 2
```

- [ ] **Step 3: Run test to verify failure**

Run: `uv run pytest tests/preflight/test_inbox.py -v`
Expected: FAIL — `ImportError: cannot import name 'enumerate_inbox_from_fs'`

- [ ] **Step 4: Implement preflight_inbox**

Create `src/bitgn_contest_agent/preflight/inbox.py`:

```python
"""preflight_inbox — enumerates open inbox items and the full set of
finance files linked to each referenced entity.

This is the highest-leverage preflight tool. Bench #2 failures t016,
t041, t066, t091 all stem from OCRing one bill when multiple bills for
the same entity exist. The tool pre-computes the entity→bills graph so
the agent sees the full picture before acting.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.canonicalize import normalize_name
from bitgn_contest_agent.preflight.response import build_response
from bitgn_contest_agent.preflight.schema import _parse_frontmatter
from bitgn_contest_agent.schemas import Req_PreflightInbox


_ENTITY_MENTION_RE = re.compile(r"\b([A-Z][\w\s\-]{2,40})\b")


def _parse_aliases_list(raw: str) -> List[str]:
    """Very small YAML-list parser: 'aliases: ["a", "b"]' → ['a', 'b']."""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        return [p.strip().strip('"\'') for p in inner.split(",") if p.strip()]
    return [raw.strip('"\'')]


def _load_entities(entities_dir: Path) -> list[dict[str, Any]]:
    """Return a list of {file, canonical, aliases, frontmatter} records."""
    entities = []
    if not entities_dir.exists():
        return entities
    for f in entities_dir.rglob("*.md"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        aliases = _parse_aliases_list(fm.get("aliases", ""))
        canonical = f.stem.replace("_", " ").title()
        entities.append({
            "file": str(f),
            "canonical": canonical,
            "aliases": [canonical] + aliases,
            "frontmatter": fm,
        })
    return entities


def _match_entity(text: str, entities: list[dict[str, Any]]) -> dict[str, Any] | None:
    text_norm = normalize_name(text)
    best = None
    for e in entities:
        for alias in e["aliases"]:
            a_norm = normalize_name(alias)
            if a_norm and a_norm in text_norm:
                # Prefer longer alias matches (more specific).
                if best is None or len(a_norm) > len(normalize_name(best["matched_alias"])):
                    best = {**e, "matched_alias": alias}
    return best


def _bills_for_entity(entity: dict[str, Any], finance_dirs: list[Path]) -> list[str]:
    alias_norms = [normalize_name(a) for a in entity["aliases"] if a]
    hits: list[str] = []
    for d in finance_dirs:
        if not d.exists():
            continue
        for f in d.rglob("*.md"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = _parse_frontmatter(text)
            vendor = normalize_name(fm.get("vendor", ""))
            if vendor and any(a in vendor or vendor in a for a in alias_norms if a):
                hits.append(str(f))
    return sorted(hits)


def enumerate_inbox_from_fs(
    root: Path,
    inbox_root: str,
    entities_root: str,
    finance_roots: list[str],
) -> list[dict[str, Any]]:
    """Local-filesystem implementation — used by tests and shared logic."""
    root = Path(root)
    inbox_dir = root / inbox_root
    entities_dir = root / entities_root
    finance_dirs = [root / fr for fr in finance_roots]

    entities = _load_entities(entities_dir)
    items = []
    if not inbox_dir.exists():
        return items
    for f in sorted(inbox_dir.rglob("*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        body = text.split("---", 2)[-1] if text.count("---") >= 2 else text
        match = _match_entity(body, entities)
        item = {
            "path": str(f),
            "task_type": fm.get("inbox_type") or fm.get("inbox_kind") or "",
            "entity_ref": match["matched_alias"] if match else None,
            "entity_canonical": match["canonical"] if match else None,
            "related_finance_files": _bills_for_entity(match, finance_dirs) if match else [],
        }
        items.append(item)
    return items


def _summarize(items: list[dict[str, Any]]) -> str:
    if not items:
        return "0 open inbox items."
    parts = [f"{len(items)} open inbox item(s)."]
    for i, it in enumerate(items, 1):
        canon = it["entity_canonical"] or "unresolved"
        n = len(it["related_finance_files"])
        parts.append(f"Item #{i} references entity '{canon}' → {n} related finance file(s).")
    return " ".join(parts)


def run_preflight_inbox(client: Any, req: Req_PreflightInbox) -> ToolResult:
    """PCM-backed entry. Uses PCM list+read RPCs to enumerate."""
    from bitgn.vm import pcm_pb2
    items: list[dict[str, Any]] = []
    try:
        # Load entities via PCM
        entities_resp = client.List(pcm_pb2.ListRequest(path=req.entities_root))
        entities = []
        for e in entities_resp.entries:
            if not e.name.endswith(".md"):
                continue
            rp = f"{req.entities_root}/{e.name}"
            rr = client.Read(pcm_pb2.ReadRequest(path=rp))
            fm = _parse_frontmatter(rr.content)
            aliases = _parse_aliases_list(fm.get("aliases", ""))
            canonical = Path(e.name).stem.replace("_", " ").title()
            entities.append({
                "file": rp,
                "canonical": canonical,
                "aliases": [canonical] + aliases,
                "frontmatter": fm,
            })

        # Enumerate inbox
        inbox_resp = client.List(pcm_pb2.ListRequest(path=req.inbox_root))
        for e in inbox_resp.entries:
            if not e.name.endswith(".md"):
                continue
            ip = f"{req.inbox_root}/{e.name}"
            ir = client.Read(pcm_pb2.ReadRequest(path=ip))
            fm = _parse_frontmatter(ir.content)
            body = ir.content.split("---", 2)[-1] if ir.content.count("---") >= 2 else ir.content
            match = _match_entity(body, entities)
            related: list[str] = []
            if match:
                alias_norms = [normalize_name(a) for a in match["aliases"] if a]
                for froot in req.finance_roots:
                    try:
                        fresp = client.List(pcm_pb2.ListRequest(path=froot))
                    except Exception:
                        continue
                    for fe in fresp.entries:
                        if not fe.name.endswith(".md"):
                            continue
                        fp = f"{froot}/{fe.name}"
                        fr_read = client.Read(pcm_pb2.ReadRequest(path=fp))
                        ffm = _parse_frontmatter(fr_read.content)
                        vendor = normalize_name(ffm.get("vendor", ""))
                        if vendor and any(a in vendor or vendor in a for a in alias_norms if a):
                            related.append(fp)
            items.append({
                "path": ip,
                "task_type": fm.get("inbox_type") or fm.get("inbox_kind") or "",
                "entity_ref": match["matched_alias"] if match else None,
                "entity_canonical": match["canonical"] if match else None,
                "related_finance_files": sorted(related),
            })
    except Exception as exc:
        return ToolResult(
            ok=False,
            content="",
            refs=tuple(),
            error=f"preflight_inbox failed: {exc}",
            error_code="INTERNAL",
            wall_ms=0,
        )

    content = build_response(summary=_summarize(items), data={"items": items})
    return ToolResult(
        ok=True,
        content=content,
        refs=tuple(),
        error=None,
        error_code=None,
        wall_ms=0,
    )
```

- [ ] **Step 5: Run test to verify pass**

Run: `uv run pytest tests/preflight/test_inbox.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/preflight/inbox.py \
        tests/preflight/test_inbox.py \
        tests/preflight/fixtures/
git commit -m "feat(preflight): preflight_inbox — entity→bills graph traversal"
```

---

## Task 4: `preflight_finance` — vendor/entity canonicalization

**Files:**
- Create: `src/bitgn_contest_agent/preflight/finance.py`
- Create: `tests/preflight/test_finance.py`

- [ ] **Step 1: Write failing test**

Create `tests/preflight/test_finance.py`:

```python
from pathlib import Path

from bitgn_contest_agent.preflight.finance import run_finance_from_fs


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_ws"


def test_finance_canonicalizes_via_alias():
    out = run_finance_from_fs(
        root=FIXTURE,
        finance_roots=["50_finance/purchases"],
        entities_root="20_entities",
        query="House Mesh",
    )
    assert out["canonical_entity"] == "Juniper"
    assert len(out["finance_files"]) >= 1


def test_finance_returns_file_metadata():
    out = run_finance_from_fs(
        root=FIXTURE,
        finance_roots=["50_finance/purchases"],
        entities_root="20_entities",
        query="Juniper Systems",
    )
    f = out["finance_files"][0]
    assert "vendor" in f
    assert "path" in f


def test_finance_empty_on_unknown_query():
    out = run_finance_from_fs(
        root=FIXTURE,
        finance_roots=["50_finance/purchases"],
        entities_root="20_entities",
        query="NonExistentVendor XYZ",
    )
    assert out["canonical_entity"] is None
    assert out["finance_files"] == []
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/preflight/test_finance.py -v`
Expected: FAIL — module missing

- [ ] **Step 3: Implement preflight_finance**

Create `src/bitgn_contest_agent/preflight/finance.py`:

```python
"""preflight_finance — canonicalizes a finance query against entity
aliases and returns matching purchase/invoice files with extracted
metadata (vendor, date, total, line_items).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.canonicalize import normalize_name
from bitgn_contest_agent.preflight.inbox import (
    _bills_for_entity,
    _load_entities,
    _match_entity,
)
from bitgn_contest_agent.preflight.response import build_response
from bitgn_contest_agent.preflight.schema import _parse_frontmatter
from bitgn_contest_agent.schemas import Req_PreflightFinance


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
        # Fallback: match by query directly against vendor field
        q_norm = normalize_name(query)
        bill_paths = []
        for d in finance_dirs:
            if not d.exists():
                continue
            for f in d.rglob("*.md"):
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                fm = _parse_frontmatter(text)
                if q_norm and q_norm in normalize_name(fm.get("vendor", "")):
                    bill_paths.append(str(f))
    files_meta = []
    for bp in bill_paths:
        try:
            text = Path(bp).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        files_meta.append({
            "path": bp,
            "vendor": fm.get("vendor", ""),
            "date": fm.get("date", ""),
            "total": fm.get("eur_total", ""),
            "line_items": fm.get("line_items", ""),
        })
    return {
        "canonical_entity": match["canonical"] if match else None,
        "aliases_matched": [match["matched_alias"]] if match else [],
        "finance_files": files_meta,
    }


def run_preflight_finance(client: Any, req: Req_PreflightFinance) -> ToolResult:
    from bitgn.vm import pcm_pb2
    try:
        # Load entities
        entities = []
        eresp = client.List(pcm_pb2.ListRequest(path=req.entities_root))
        for e in eresp.entries:
            if not e.name.endswith(".md"):
                continue
            rp = f"{req.entities_root}/{e.name}"
            rr = client.Read(pcm_pb2.ReadRequest(path=rp))
            fm = _parse_frontmatter(rr.content)
            from bitgn_contest_agent.preflight.inbox import _parse_aliases_list
            aliases = _parse_aliases_list(fm.get("aliases", ""))
            canonical = Path(e.name).stem.replace("_", " ").title()
            entities.append({
                "file": rp,
                "canonical": canonical,
                "aliases": [canonical] + aliases,
                "frontmatter": fm,
            })
        match = _match_entity(req.query, entities)
        alias_norms = [normalize_name(a) for a in (match["aliases"] if match else [req.query]) if a]

        files_meta = []
        for froot in req.finance_roots:
            try:
                fresp = client.List(pcm_pb2.ListRequest(path=froot))
            except Exception:
                continue
            for fe in fresp.entries:
                if not fe.name.endswith(".md"):
                    continue
                fp = f"{froot}/{fe.name}"
                fr = client.Read(pcm_pb2.ReadRequest(path=fp))
                ffm = _parse_frontmatter(fr.content)
                vendor = normalize_name(ffm.get("vendor", ""))
                if vendor and any(a in vendor or vendor in a for a in alias_norms if a):
                    files_meta.append({
                        "path": fp,
                        "vendor": ffm.get("vendor", ""),
                        "date": ffm.get("date", ""),
                        "total": ffm.get("eur_total", ""),
                        "line_items": ffm.get("line_items", ""),
                    })
        data = {
            "canonical_entity": match["canonical"] if match else None,
            "aliases_matched": [match["matched_alias"]] if match else [],
            "finance_files": files_meta,
        }
    except Exception as exc:
        return ToolResult(
            ok=False, content="", refs=tuple(),
            error=f"preflight_finance failed: {exc}",
            error_code="INTERNAL", wall_ms=0,
        )

    summary = (
        f"Query '{req.query}' → entity '{data['canonical_entity']}' "
        f"({len(data['finance_files'])} finance file(s))."
        if data["canonical_entity"] else
        f"Query '{req.query}' → no entity match. {len(data['finance_files'])} direct vendor match(es)."
    )
    return ToolResult(
        ok=True,
        content=build_response(summary=summary, data=data),
        refs=tuple(), error=None, error_code=None, wall_ms=0,
    )
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/preflight/test_finance.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/finance.py \
        tests/preflight/test_finance.py
git commit -m "feat(preflight): preflight_finance — vendor/entity canonicalization"
```

---

## Task 5: `preflight_entity` — pure entity disambiguation

**Files:**
- Create: `src/bitgn_contest_agent/preflight/entity.py`
- Create: `tests/preflight/test_entity.py`

- [ ] **Step 1: Write failing test**

Create `tests/preflight/test_entity.py`:

```python
from pathlib import Path

from bitgn_contest_agent.preflight.entity import run_entity_from_fs


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_ws"


def test_entity_direct_match():
    out = run_entity_from_fs(
        root=FIXTURE,
        entities_root="20_entities",
        query="Juniper",
    )
    assert len(out["matches"]) >= 1
    assert out["matches"][0]["canonical"] == "Juniper"


def test_entity_alias_match():
    out = run_entity_from_fs(
        root=FIXTURE,
        entities_root="20_entities",
        query="House Mesh",
    )
    assert len(out["matches"]) >= 1
    assert "House Mesh" in out["matches"][0]["aliases"]


def test_entity_no_match():
    out = run_entity_from_fs(
        root=FIXTURE,
        entities_root="20_entities",
        query="Unknown Name ZZZ",
    )
    assert out["matches"] == []
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/preflight/test_entity.py -v`
Expected: FAIL

- [ ] **Step 3: Implement preflight_entity**

Create `src/bitgn_contest_agent/preflight/entity.py`:

```python
"""preflight_entity — disambiguates an entity query against entity
records and aliases. Pure lookup, no cross-referencing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.canonicalize import normalize_name
from bitgn_contest_agent.preflight.inbox import _load_entities, _parse_aliases_list
from bitgn_contest_agent.preflight.response import build_response
from bitgn_contest_agent.preflight.schema import _parse_frontmatter
from bitgn_contest_agent.schemas import Req_PreflightEntity


def _find_matches(query: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    q_norm = normalize_name(query)
    if not q_norm:
        return []
    matches = []
    for e in entities:
        for alias in e["aliases"]:
            if normalize_name(alias) == q_norm:
                matches.append({
                    "canonical": e["canonical"],
                    "aliases": e["aliases"],
                    "file": e["file"],
                    "frontmatter": e["frontmatter"],
                })
                break
        else:
            # substring fallback
            for alias in e["aliases"]:
                a_norm = normalize_name(alias)
                if a_norm and (q_norm in a_norm or a_norm in q_norm):
                    matches.append({
                        "canonical": e["canonical"],
                        "aliases": e["aliases"],
                        "file": e["file"],
                        "frontmatter": e["frontmatter"],
                    })
                    break
    return matches


def run_entity_from_fs(root: Path, entities_root: str, query: str) -> dict[str, Any]:
    root = Path(root)
    entities = _load_entities(root / entities_root)
    return {"matches": _find_matches(query, entities)}


def run_preflight_entity(client: Any, req: Req_PreflightEntity) -> ToolResult:
    from bitgn.vm import pcm_pb2
    try:
        entities = []
        eresp = client.List(pcm_pb2.ListRequest(path=req.entities_root))
        for e in eresp.entries:
            if not e.name.endswith(".md"):
                continue
            rp = f"{req.entities_root}/{e.name}"
            rr = client.Read(pcm_pb2.ReadRequest(path=rp))
            fm = _parse_frontmatter(rr.content)
            aliases = _parse_aliases_list(fm.get("aliases", ""))
            canonical = Path(e.name).stem.replace("_", " ").title()
            entities.append({
                "file": rp,
                "canonical": canonical,
                "aliases": [canonical] + aliases,
                "frontmatter": fm,
            })
        matches = _find_matches(req.query, entities)
        data = {"matches": matches}
    except Exception as exc:
        return ToolResult(
            ok=False, content="", refs=tuple(),
            error=f"preflight_entity failed: {exc}",
            error_code="INTERNAL", wall_ms=0,
        )
    summary = (
        f"Query '{req.query}' → {len(matches)} entity match(es)."
        if matches else
        f"Query '{req.query}' → no entity match."
    )
    return ToolResult(
        ok=True, content=build_response(summary=summary, data=data),
        refs=tuple(), error=None, error_code=None, wall_ms=0,
    )
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/preflight/test_entity.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/entity.py tests/preflight/test_entity.py
git commit -m "feat(preflight): preflight_entity — pure entity disambiguation"
```

---

## Task 6: `preflight_project` — project record + involved entities

**Files:**
- Create: `src/bitgn_contest_agent/preflight/project.py`
- Create: `tests/preflight/test_project.py`

- [ ] **Step 1: Write failing test**

Create `tests/preflight/test_project.py`:

```python
from pathlib import Path

from bitgn_contest_agent.preflight.project import run_project_from_fs


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_ws"


def test_project_resolves_name_to_record():
    out = run_project_from_fs(
        root=FIXTURE,
        projects_root="30_projects",
        entities_root="20_entities",
        query="Health Baseline",
    )
    assert out["project"] is not None
    assert out["project"]["name"] == "Health Baseline"


def test_project_returns_start_date():
    out = run_project_from_fs(
        root=FIXTURE,
        projects_root="30_projects",
        entities_root="20_entities",
        query="Health Baseline",
    )
    assert out["project"]["start_date"] == "2025-11-14"


def test_project_no_match_returns_none():
    out = run_project_from_fs(
        root=FIXTURE,
        projects_root="30_projects",
        entities_root="20_entities",
        query="Nonexistent Project",
    )
    assert out["project"] is None
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/preflight/test_project.py -v`
Expected: FAIL

- [ ] **Step 3: Implement preflight_project**

Create `src/bitgn_contest_agent/preflight/project.py`:

```python
"""preflight_project — locates a project record and returns its
metadata + entities involved (members).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.canonicalize import normalize_name
from bitgn_contest_agent.preflight.response import build_response
from bitgn_contest_agent.preflight.schema import _parse_frontmatter
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
        fm = _parse_frontmatter(text)
        pname = fm.get("project", "")
        if pname and (normalize_name(pname) == q_norm or q_norm in normalize_name(pname) or normalize_name(pname) in q_norm):
            return {
                "name": pname,
                "start_date": fm.get("start_date", ""),
                "members": fm.get("members", ""),
                "file": str(f),
                "frontmatter": fm,
            }
    return None


def run_project_from_fs(
    root: Path, projects_root: str, entities_root: str, query: str,
) -> dict[str, Any]:
    root = Path(root)
    proj = _find_project(root / projects_root, query)
    return {"project": proj, "involved_entities": []}


def run_preflight_project(client: Any, req: Req_PreflightProject) -> ToolResult:
    from bitgn.vm import pcm_pb2
    try:
        q_norm = normalize_name(req.query)
        found = None
        lresp = client.List(pcm_pb2.ListRequest(path=req.projects_root))
        for e in lresp.entries:
            if not e.name.endswith(".md"):
                continue
            fp = f"{req.projects_root}/{e.name}"
            rr = client.Read(pcm_pb2.ReadRequest(path=fp))
            fm = _parse_frontmatter(rr.content)
            pname = fm.get("project", "")
            if pname and (normalize_name(pname) == q_norm
                          or q_norm in normalize_name(pname)
                          or normalize_name(pname) in q_norm):
                found = {
                    "name": pname,
                    "start_date": fm.get("start_date", ""),
                    "members": fm.get("members", ""),
                    "file": fp,
                    "frontmatter": fm,
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
        summary = f"Project '{found['name']}' found. Start date: {found['start_date']}."
    else:
        summary = f"Query '{req.query}' → no project match."
    return ToolResult(
        ok=True, content=build_response(summary=summary, data=data),
        refs=tuple(), error=None, error_code=None, wall_ms=0,
    )
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/preflight/test_project.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/project.py tests/preflight/test_project.py
git commit -m "feat(preflight): preflight_project — project record + involved entities"
```

---

## Task 7: `preflight_doc_migration` — destination resolution

**Files:**
- Create: `src/bitgn_contest_agent/preflight/doc_migration.py`
- Create: `tests/preflight/test_doc_migration.py`

- [ ] **Step 1: Add a NORA entity to the fixture**

Create `tests/preflight/fixtures/tiny_ws/20_entities/nora_rees.md`:

```markdown
---
aliases: ["NORA", "Nora"]
role: person
---
```

- [ ] **Step 2: Write failing test**

Create `tests/preflight/test_doc_migration.py`:

```python
from pathlib import Path

from bitgn_contest_agent.preflight.doc_migration import run_doc_migration_from_fs


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_ws"


def test_doc_migration_resolves_alias_to_entity_dir():
    out = run_doc_migration_from_fs(
        root=FIXTURE,
        source_paths=["some/source/a.md", "some/source/b.md"],
        entities_root="20_entities",
        query="NORA",
    )
    assert out["target_canonical"] == "Nora Rees"
    assert out["destination_root"].startswith("20_entities")


def test_doc_migration_preserves_source_filenames():
    out = run_doc_migration_from_fs(
        root=FIXTURE,
        source_paths=["some/source/a.md"],
        entities_root="20_entities",
        query="NORA",
    )
    m = out["migrations"][0]
    assert m["destination"].endswith("a.md")
    assert m["source"] == "some/source/a.md"
```

- [ ] **Step 3: Run test to verify failure**

Run: `uv run pytest tests/preflight/test_doc_migration.py -v`
Expected: FAIL

- [ ] **Step 4: Implement preflight_doc_migration**

Create `src/bitgn_contest_agent/preflight/doc_migration.py`:

```python
"""preflight_doc_migration — resolves a migration destination root
from a query (entity alias or area name), computes per-source
destination paths, flags collisions.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.entity import _find_matches
from bitgn_contest_agent.preflight.inbox import _load_entities, _parse_aliases_list
from bitgn_contest_agent.preflight.response import build_response
from bitgn_contest_agent.preflight.schema import _parse_frontmatter
from bitgn_contest_agent.schemas import Req_PreflightDocMigration


def _slugify(name: str) -> str:
    return "_".join(name.lower().split())


def run_doc_migration_from_fs(
    root: Path,
    source_paths: list[str],
    entities_root: str,
    query: str,
) -> dict[str, Any]:
    root = Path(root)
    entities = _load_entities(root / entities_root)
    matches = _find_matches(query, entities)
    if not matches:
        return {
            "target_canonical": None,
            "destination_root": None,
            "migrations": [],
        }
    m = matches[0]
    dest_root = f"{entities_root}/{_slugify(m['canonical'])}"
    dest_path = root / dest_root
    existing = set()
    if dest_path.exists():
        existing = {f.name for f in dest_path.iterdir() if f.is_file()}
    migrations = []
    for sp in source_paths:
        fname = os.path.basename(sp)
        migrations.append({
            "source": sp,
            "destination": f"{dest_root}/{fname}",
            "collision": fname in existing,
        })
    return {
        "target_canonical": m["canonical"],
        "destination_root": dest_root,
        "migrations": migrations,
    }


def run_preflight_doc_migration(client: Any, req: Req_PreflightDocMigration) -> ToolResult:
    from bitgn.vm import pcm_pb2
    try:
        entities = []
        eresp = client.List(pcm_pb2.ListRequest(path=req.entities_root))
        for e in eresp.entries:
            if not e.name.endswith(".md"):
                continue
            rp = f"{req.entities_root}/{e.name}"
            rr = client.Read(pcm_pb2.ReadRequest(path=rp))
            fm = _parse_frontmatter(rr.content)
            aliases = _parse_aliases_list(fm.get("aliases", ""))
            canonical = Path(e.name).stem.replace("_", " ").title()
            entities.append({
                "file": rp, "canonical": canonical,
                "aliases": [canonical] + aliases, "frontmatter": fm,
            })
        matches = _find_matches(req.query, entities)
        if not matches:
            data = {"target_canonical": None, "destination_root": None, "migrations": []}
        else:
            m = matches[0]
            dest_root = f"{req.entities_root}/{_slugify(m['canonical'])}"
            existing = set()
            try:
                lresp = client.List(pcm_pb2.ListRequest(path=dest_root))
                existing = {e.name for e in lresp.entries}
            except Exception:
                pass
            migrations = []
            for sp in req.source_paths:
                fname = sp.rsplit("/", 1)[-1]
                migrations.append({
                    "source": sp,
                    "destination": f"{dest_root}/{fname}",
                    "collision": fname in existing,
                })
            data = {
                "target_canonical": m["canonical"],
                "destination_root": dest_root,
                "migrations": migrations,
            }
    except Exception as exc:
        return ToolResult(
            ok=False, content="", refs=tuple(),
            error=f"preflight_doc_migration failed: {exc}",
            error_code="INTERNAL", wall_ms=0,
        )
    if data["target_canonical"]:
        summary = (
            f"Target '{req.query}' → '{data['target_canonical']}'. "
            f"Destination: {data['destination_root']}. "
            f"{len(data['migrations'])} source file(s)."
        )
    else:
        summary = f"Target '{req.query}' → no entity match."
    return ToolResult(
        ok=True, content=build_response(summary=summary, data=data),
        refs=tuple(), error=None, error_code=None, wall_ms=0,
    )
```

- [ ] **Step 5: Run test to verify pass**

Run: `uv run pytest tests/preflight/test_doc_migration.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/preflight/doc_migration.py \
        tests/preflight/test_doc_migration.py \
        tests/preflight/fixtures/
git commit -m "feat(preflight): preflight_doc_migration — destination resolution"
```

---

## Task 8: Harness gate + system prompt + integration test

**Goal:** Enforce "preflight required before non-whitelisted tool call" at the harness level, update the system prompt, prove end-to-end with an integration test.

**Files:**
- Modify: `src/bitgn_contest_agent/agent.py` (add gate before dispatch at ~line 444)
- Modify: `src/bitgn_contest_agent/prompts.py`
- Create: `tests/test_harness_gate.py`

- [ ] **Step 1: Read existing agent.py dispatch path to understand Session attributes**

Run: `grep -n "class Session\|def loop_nudge_needed\|session\." src/bitgn_contest_agent/session.py | head -30`

Expected: Session has fields like `mutations`, `nudges_emitted`, etc. The implementer should read `session.py` fully before writing the gate to see what methods/fields exist for tracking tool-call history.

Also run: `grep -n "reads\|seen_refs\|prior_tools" src/bitgn_contest_agent/session.py | head -20`

Note: Session already has `seen_refs` and tracks reads via `_record_read_attempt`. We need to add a `preflight_seen: bool` flag.

- [ ] **Step 2: Write failing test for the gate**

Create `tests/test_harness_gate.py`:

```python
"""Harness-level preflight gate — proves that non-whitelisted tool
calls are rejected until a preflight_* tool has been observed.
"""
from bitgn_contest_agent.harness_gate import (
    PREFLIGHT_WHITELIST,
    is_preflight_tool,
    should_reject,
)


def test_whitelist_contains_schema_list_context():
    assert "preflight_schema" in PREFLIGHT_WHITELIST
    assert "list" in PREFLIGHT_WHITELIST
    assert "context" in PREFLIGHT_WHITELIST
    assert "report_completion" in PREFLIGHT_WHITELIST


def test_is_preflight_tool_true_for_all_six():
    for name in [
        "preflight_schema", "preflight_inbox", "preflight_finance",
        "preflight_entity", "preflight_project", "preflight_doc_migration",
    ]:
        assert is_preflight_tool(name)


def test_should_reject_when_read_before_preflight():
    # No preflight seen yet, first non-whitelisted call is 'read'
    assert should_reject(tool_name="read", preflight_seen=False) is True


def test_should_reject_false_when_preflight_seen():
    assert should_reject(tool_name="read", preflight_seen=True) is False


def test_should_reject_false_for_whitelist_even_without_preflight():
    assert should_reject(tool_name="list", preflight_seen=False) is False
    assert should_reject(tool_name="preflight_schema", preflight_seen=False) is False


def test_should_reject_false_for_preflight_itself():
    assert should_reject(tool_name="preflight_inbox", preflight_seen=False) is False
```

- [ ] **Step 3: Run test to verify failure**

Run: `uv run pytest tests/test_harness_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitgn_contest_agent.harness_gate'`

- [ ] **Step 4: Implement the gate module**

Create `src/bitgn_contest_agent/harness_gate.py`:

```python
"""Harness-level preflight gate.

Policy: before any non-whitelisted tool call, at least one preflight_*
tool must have been successfully dispatched in the current task trace.
Rejected calls do not consume a step; the agent is told to preflight
first and retries.

Whitelist covers:
 - preflight tools themselves (so the agent can call them)
 - light discovery (list, context) — safe probes that don't read content
 - report_completion — agents that fail to do any work still get to
   terminate rather than spin forever.
"""
from __future__ import annotations


PREFLIGHT_TOOLS = frozenset({
    "preflight_schema",
    "preflight_inbox",
    "preflight_finance",
    "preflight_entity",
    "preflight_project",
    "preflight_doc_migration",
})

PREFLIGHT_WHITELIST = PREFLIGHT_TOOLS | frozenset({
    "list",
    "context",
    "report_completion",
})


REJECTION_MESSAGE = (
    "Preflight required. Before reading, searching, or writing anything, "
    "call preflight_schema to learn the workspace layout, then call the "
    "preflight_* tool(s) that match your task: preflight_inbox for inbox/OCR, "
    "preflight_finance for finance lookups, preflight_entity for entity "
    "questions, preflight_project for project questions, "
    "preflight_doc_migration for document migration. You may call any of "
    "these again later in the task."
)


def is_preflight_tool(tool_name: str) -> bool:
    return tool_name in PREFLIGHT_TOOLS


def should_reject(tool_name: str, preflight_seen: bool) -> bool:
    if tool_name in PREFLIGHT_WHITELIST:
        return False
    return not preflight_seen
```

- [ ] **Step 5: Run gate unit test to verify pass**

Run: `uv run pytest tests/test_harness_gate.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Wire the gate into agent.py**

Read `src/bitgn_contest_agent/session.py` first to confirm Session layout.

Then in `src/bitgn_contest_agent/agent.py`:

Add `preflight_seen: bool = False` tracking. If Session is a dataclass/frozen, track it in the agent loop itself. Otherwise add a field to Session.

Concretely, modify `agent.py` around line 444 (the `tool_result = self._adapter.dispatch(fn)` line). Wrap it:

```python
            # Preflight gate — reject non-whitelisted calls until preflight observed.
            tool_name_peek = getattr(fn, "tool", "")
            if should_reject(tool_name_peek, preflight_seen):
                # Synthesize a rejection ToolResult; don't dispatch, don't count step.
                gate_result = ToolResult(
                    ok=False,
                    content="",
                    refs=tuple(),
                    error=REJECTION_MESSAGE,
                    error_code="PREFLIGHT_REQUIRED",
                    wall_ms=0,
                )
                emit_arch(
                    category=ArchCategory.GATE,
                    at_step=step_idx,
                    details=f"preflight_gate_reject tool={tool_name_peek}",
                )
                # Append assistant message + user tool-result feedback so the
                # LLM sees the rejection, but DO NOT increment totals.steps.
                messages.append(Message(role="assistant", content=step_obj.model_dump_json()))
                messages.append(Message(role="user", content=f"Tool result:\nERROR (PREFLIGHT_REQUIRED): {REJECTION_MESSAGE}"))
                continue  # next loop iteration without incrementing step

            tool_result = self._adapter.dispatch(fn)
            tool_name = getattr(fn, "tool", "")
            if is_preflight_tool(tool_name) and tool_result.ok:
                preflight_seen = True
```

Above the dispatch loop, initialize `preflight_seen = False` at task start.

Imports to add at the top of `agent.py`:

```python
from bitgn_contest_agent.harness_gate import (
    REJECTION_MESSAGE,
    is_preflight_tool,
    should_reject,
)
```

Check whether `ArchCategory.GATE` exists in `arch_constants.py`. If not, add `GATE = "gate"` to the enum.

- [ ] **Step 7: Update the system prompt**

In `src/bitgn_contest_agent/prompts.py`, find the main system prompt string (the one fed to the planner at task start). Append this block to the end:

```python
PREFLIGHT_PROTOCOL = """
## Preflight Protocol

Before reading, searching, or writing anything beyond a directory
listing, you must:

1. Call `preflight_schema` to learn the workspace layout (roots for
   inbox, entities, finance, projects, outbox, etc.).
2. Call whichever `preflight_*` tool(s) match your task shape. Pass
   the roots you learned in step 1 as arguments:
   - Inbox/OCR tasks → `preflight_inbox(inbox_root, entities_root, finance_roots)`
   - Finance lookups → `preflight_finance(finance_roots, entities_root, query)`
   - Entity/person questions → `preflight_entity(entities_root, query)`
   - Project questions → `preflight_project(projects_root, entities_root, query)`
   - Document migration → `preflight_doc_migration(source_paths, entities_root, query)`
3. Only then act on the task with normal tools (read, search, write).

Preflight tools return a short `summary` and structured `data`. Treat
the summary as ground truth about what's in the workspace. Re-invoke
preflight tools later in the task if a search dead-ends or a graph
traversal comes up short — they remain available throughout.

You may call multiple preflight tools if your task spans areas (e.g.
inbox item that references both finance and project records).
"""
```

Then find where the main system prompt is assembled and append `PREFLIGHT_PROTOCOL` to it. The exact concatenation point depends on existing code — implementer should grep for `SYSTEM_PROMPT`, `PLANNER_PROMPT`, or `build_system_prompt` to locate it.

- [ ] **Step 8: Run full test suite**

Run: `uv run pytest tests/ -x -q 2>&1 | tail -30`
Expected: all tests pass including new ones. Any regression in existing tests must be fixed before proceeding.

- [ ] **Step 9: Commit**

```bash
git add src/bitgn_contest_agent/harness_gate.py \
        src/bitgn_contest_agent/agent.py \
        src/bitgn_contest_agent/prompts.py \
        src/bitgn_contest_agent/arch_constants.py \
        tests/test_harness_gate.py
git commit -m "feat(preflight): harness gate + system prompt protocol + tests"
```

---

## Self-Review Checklist

After all tasks complete:

1. **Spec coverage:** every tool in the spec has a task? ✓ (Tasks 2–7). Gate + prompt? ✓ (Task 8). Shared helpers? ✓ (Task 1).
2. **Placeholder scan:** no "TBD" / "similar to" / "handle errors appropriately" — each task has exact code.
3. **Type consistency:** preflight response helpers (`_load_entities`, `_match_entity`, `_bills_for_entity`, `_find_matches`) are defined in `inbox.py` / `entity.py` and re-imported by downstream tools. Names are stable across tasks.
4. **Import cycles:** `finance.py` imports from `inbox.py`; `doc_migration.py` imports from `entity.py` and `inbox.py`; `entity.py` imports from `inbox.py`. All flow one direction; no cycles.

---

## Post-Implementation: Bench Validation

After Task 8 lands, run p10i15 on PROD:

```bash
source .worktrees/plan-b/.env
uv run python -m bitgn_contest_agent.cli run-benchmark \
    --bench bitgn/pac1-prod \
    --parallelism 10 \
    --max-inflight-llm 15 \
    --runs 1 \
    --tag preflight_tools_p10i15_gpt54_prod
```

Expected signal:
- Baseline (fcb9f3e): 96/104 (92.3%)
- Target: ≥99/104 (fixes for t016/t041/t066/t091 = +4, holding others)
- Watch for regressions from gate false positives (non-inbox tasks failing because gate blocked a legitimate call)
