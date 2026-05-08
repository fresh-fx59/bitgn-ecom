# Descriptor Resolution Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a `preflight_semantic_index` prepass command that emits a compact per-record digest (cast + projects) into the bootstrap block, so the agent can map informal descriptors ("the founder I talk product with", "the do-not-degrade lane") to canonical IDs from first reply.

**Architecture:** New request class `Req_PreflightSemanticIndex`, new module `preflight/semantic_index.py` (parallel to `preflight/schema.py`), dispatched in `PcmAdapter.dispatch` and appended to `run_prepass` after `preflight_schema` so it can reuse discovered `entities_root` / `projects_root`. The bootstrap string is emitted by the implementation module (testable in isolation) and appended verbatim to `PrepassResult.bootstrap_content`.

**Tech Stack:** Python 3.11, pytest, pydantic v2, existing PCM runtime stubs via `unittest.mock.MagicMock`. Reuses `parse_record_metadata` from `preflight/schema.py`.

---

## File Structure

**Create:**
- `src/bitgn_contest_agent/preflight/semantic_index.py` — entry points (fs + PCM), record extraction, digest formatting.
- `tests/preflight/test_semantic_index.py` — parser and formatter unit tests.
- `tests/preflight/fixtures/semantic_index_ws/` — tiny workspace with 2 cast + 2 project records (one YAML-frontmatter, one bullet-list, plus one malformed cast + one project missing `goal:`).

**Modify:**
- `src/bitgn_contest_agent/schemas.py` — add `Req_PreflightSemanticIndex`; extend `FunctionUnion` and `REQ_MODELS`.
- `src/bitgn_contest_agent/adapter/pcm.py` — add dispatch branch; extend `run_prepass` to call semantic index after schema and append bootstrap string.
- `tests/adapter/test_pcm_prepass_schema_roots.py` (or new sibling) — verify the new bootstrap string is appended and traced.

---

## Task 1: Scaffold schemas (request class)

**Files:**
- Modify: `src/bitgn_contest_agent/schemas.py`

- [ ] **Step 1: Add request class after `Req_PreflightSchema` (line 73-75)**

```python
class Req_PreflightSemanticIndex(BaseModel):
    """Emit a compact per-record digest of cast and projects so the agent
    can match informal descriptors (role phrases, lane labels) against
    canonical IDs. Runs once per task in the prepass, after schema
    discovery. Always safe to call.
    """
    tool: Literal["preflight_semantic_index"]
```

- [ ] **Step 2: Extend `FunctionUnion` (line 94-110) — insert after `Req_PreflightSchema`**

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
        Req_PreflightSemanticIndex,
        ReportTaskCompletion,
    ],
    Field(discriminator="tool"),
]
```

- [ ] **Step 3: Extend `REQ_MODELS` (line 129-141) — insert after `Req_PreflightSchema`**

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
    Req_PreflightSemanticIndex,
)
```

- [ ] **Step 4: Run existing schema tests to confirm no regression**

Run: `pytest tests/test_tool_coverage.py -v`
Expected: PASS (or if it enumerates tools, update the expectation list in that test — see the test file to confirm the shape before editing).

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/schemas.py
git commit -m "feat(descriptor): Req_PreflightSemanticIndex schema"
```

---

## Task 2: Record-extraction unit (cast)

**Files:**
- Create: `src/bitgn_contest_agent/preflight/semantic_index.py`
- Create: `tests/preflight/test_semantic_index.py`
- Create: `tests/preflight/fixtures/semantic_index_ws/10_entities/cast/nina.md`
- Create: `tests/preflight/fixtures/semantic_index_ws/10_entities/cast/elena.md`
- Create: `tests/preflight/fixtures/semantic_index_ws/10_entities/cast/malformed.md`

- [ ] **Step 1: Create fixture files**

`tests/preflight/fixtures/semantic_index_ws/10_entities/cast/nina.md`:

```markdown
# Nina Schreiber

- alias: `nina`
- kind: `person`
- relationship: `startup_partner`
- created_on: `2026-03-31`
- birthday: `1989-09-05`

Pushes Miles to narrow the product and find a real buyer.
```

`tests/preflight/fixtures/semantic_index_ws/10_entities/cast/elena.md`:

```markdown
---
record_type: person
alias: elena
kind: person
relationship: day_job_ceo
birthday: 1984-07-02
---

Founder and CEO who cares whether operational pain can become commercial leverage.
```

`tests/preflight/fixtures/semantic_index_ws/10_entities/cast/malformed.md`:

```markdown
This file has no frontmatter, no bullets, just prose. The parser returns
an empty metadata dict here, and the extractor must skip this record silently.
```

- [ ] **Step 2: Write the failing test**

`tests/preflight/test_semantic_index.py`:

```python
from pathlib import Path

from bitgn_contest_agent.preflight.semantic_index import extract_cast_entries


FIXTURE = Path(__file__).parent / "fixtures" / "semantic_index_ws"


def test_extract_cast_entries_parses_bullet_and_yaml_skips_malformed():
    entries = extract_cast_entries(FIXTURE / "10_entities" / "cast")
    # Expect exactly 2 entries (nina + elena), malformed skipped.
    aliases = sorted(e.alias for e in entries)
    assert aliases == ["elena", "nina"]

    nina = next(e for e in entries if e.alias == "nina")
    assert nina.id == "entity.nina"
    assert nina.relationship == "startup_partner"
    assert nina.summary == "Pushes Miles to narrow the product and find a real buyer."

    elena = next(e for e in entries if e.alias == "elena")
    assert elena.id == "entity.elena"
    assert elena.relationship == "day_job_ceo"
    assert elena.summary.startswith("Founder and CEO who cares")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/preflight/test_semantic_index.py -v`
Expected: FAIL with `ImportError: cannot import name 'extract_cast_entries'`

- [ ] **Step 4: Create the module skeleton**

`src/bitgn_contest_agent/preflight/semantic_index.py`:

```python
"""Semantic-index preflight — compact digest of cast + project records.

Emitted once per task in the prepass, right after `preflight_schema`,
so the agent sees descriptor-to-id mappings ("the founder I talk product
with" → `entity.nina`) from the first LLM reply.

Parsing reuses `parse_record_metadata` from `preflight.schema`. The digest
is a one-line-per-record, side-by-side view that makes semantic contrast
visible in a single message.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from bitgn_contest_agent.preflight.schema import parse_record_metadata


_SUMMARY_MAX = 160


@dataclass(frozen=True)
class CastEntry:
    id: str
    alias: str
    relationship: str
    kind: str
    summary: str


def _first_prose_line(text: str) -> str:
    """Return the first non-blank line after any frontmatter / bullet
    block. Trimmed and capped at _SUMMARY_MAX chars.
    """
    in_yaml = False
    seen_bullets = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if seen_bullets:
                seen_bullets = False  # blank line ends bullet block
            continue
        if stripped == "---":
            in_yaml = not in_yaml
            continue
        if in_yaml:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and ":" in stripped:
            seen_bullets = True
            continue
        if seen_bullets:
            continue
        # First real prose line.
        return stripped[:_SUMMARY_MAX]
    return ""


def _file_id_from_path(path: Path, kind: str) -> str:
    """`entity.nina` from `10_entities/cast/nina.md`; `project.harbor_body`
    from `40_projects/2026_04_03_harbor_body/README.MD`.
    """
    if kind == "project":
        # Project id == directory name with date prefix stripped if present.
        name = path.parent.name
        # Strip a leading YYYY_MM_DD_ prefix if it matches.
        parts = name.split("_", 3)
        if len(parts) == 4 and all(p.isdigit() for p in parts[:3]):
            name = parts[3]
        return f"project.{name}"
    return f"entity.{path.stem.lower()}"


def extract_cast_entries(cast_dir: Path) -> List[CastEntry]:
    """Walk `cast_dir` for .md/.MD files; return one CastEntry per
    parseable record. Records whose metadata parser returns {} are
    skipped silently.
    """
    entries: list[CastEntry] = []
    if not cast_dir.exists() or not cast_dir.is_dir():
        return entries
    for f in sorted(cast_dir.iterdir()):
        if not f.is_file():
            continue
        if not f.name.lower().endswith(".md"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        md = parse_record_metadata(text)
        if not md:
            continue
        alias = md.get("alias") or f.stem.lower()
        entries.append(CastEntry(
            id=_file_id_from_path(f, kind="entity"),
            alias=alias,
            relationship=md.get("relationship", ""),
            kind=md.get("kind", ""),
            summary=_first_prose_line(text),
        ))
    return entries
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/preflight/test_semantic_index.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/preflight/semantic_index.py tests/preflight/test_semantic_index.py tests/preflight/fixtures/semantic_index_ws/
git commit -m "feat(descriptor): CastEntry extractor + fixture"
```

---

## Task 3: Record-extraction unit (projects)

**Files:**
- Modify: `src/bitgn_contest_agent/preflight/semantic_index.py`
- Modify: `tests/preflight/test_semantic_index.py`
- Create: `tests/preflight/fixtures/semantic_index_ws/40_projects/2026_04_03_harbor_body/README.MD`
- Create: `tests/preflight/fixtures/semantic_index_ws/40_projects/2026_04_03_black_library_evenings/README.MD`

- [ ] **Step 1: Create fixture files**

`tests/preflight/fixtures/semantic_index_ws/40_projects/2026_04_03_harbor_body/README.MD`:

```markdown
# Harbor Body

- alias: `harbor_body`
- owner_id: `entity.miles`
- kind: `health`
- lane: `health`
- priority: `high`
- status: `active`
- goal: Stay functional enough to carry family, work, and startup life without quietly collapsing.
- next_step: protect walks and one recovery block during the week

Not optimized, but back to functional enough that health is supporting the week.
```

`tests/preflight/fixtures/semantic_index_ws/40_projects/2026_04_03_black_library_evenings/README.MD`:

```markdown
# Black Library Evenings

- alias: `black_library_evenings`
- owner_id: `entity.miles`
- kind: `family`
- lane: `family`
- status: `active`
- next_step: keep one protected reading evening per week

Preserve a protected evening lane for reading that survives sprint weeks.
```

- [ ] **Step 2: Append failing test**

Add to `tests/preflight/test_semantic_index.py`:

```python
from bitgn_contest_agent.preflight.semantic_index import extract_project_entries


def test_extract_project_entries_prefers_goal_field_falls_back_to_prose():
    entries = extract_project_entries(FIXTURE / "40_projects")
    aliases = sorted(e.alias for e in entries)
    assert aliases == ["black_library_evenings", "harbor_body"]

    harbor = next(e for e in entries if e.alias == "harbor_body")
    assert harbor.id == "project.harbor_body"
    assert harbor.lane == "health"
    assert harbor.status == "active"
    # `goal:` field wins over body prose.
    assert harbor.goal.startswith("Stay functional enough")

    library = next(e for e in entries if e.alias == "black_library_evenings")
    assert library.lane == "family"
    # No `goal:` field → first prose line.
    assert library.goal.startswith("Preserve a protected evening lane")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/preflight/test_semantic_index.py::test_extract_project_entries_prefers_goal_field_falls_back_to_prose -v`
Expected: FAIL with `ImportError: cannot import name 'extract_project_entries'`

- [ ] **Step 4: Extend the module**

Append to `src/bitgn_contest_agent/preflight/semantic_index.py`:

```python
@dataclass(frozen=True)
class ProjectEntry:
    id: str
    alias: str
    lane: str
    status: str
    goal: str


def extract_project_entries(projects_dir: Path) -> List[ProjectEntry]:
    """Walk `projects_dir` for subdirectories containing a README.md or
    README.MD; return one ProjectEntry per parseable record.

    `goal` prefers the `goal:` metadata field; falls back to the first
    prose line after the bullet block.
    """
    entries: list[ProjectEntry] = []
    if not projects_dir.exists() or not projects_dir.is_dir():
        return entries
    for sub in sorted(projects_dir.iterdir()):
        if not sub.is_dir():
            continue
        readme: Optional[Path] = None
        for name in ("README.md", "README.MD", "readme.md"):
            candidate = sub / name
            if candidate.is_file():
                readme = candidate
                break
        if readme is None:
            continue
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        md = parse_record_metadata(text)
        if not md:
            continue
        goal_field = md.get("goal", "").strip()
        goal = goal_field[:_SUMMARY_MAX] if goal_field else _first_prose_line(text)
        alias = md.get("alias") or sub.name
        entries.append(ProjectEntry(
            id=_file_id_from_path(readme, kind="project"),
            alias=alias,
            lane=md.get("lane", ""),
            status=md.get("status", ""),
            goal=goal,
        ))
    return entries
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/preflight/test_semantic_index.py -v`
Expected: PASS for both `test_extract_cast_entries_*` and `test_extract_project_entries_*`

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/preflight/semantic_index.py tests/preflight/test_semantic_index.py tests/preflight/fixtures/semantic_index_ws/40_projects/
git commit -m "feat(descriptor): ProjectEntry extractor"
```

---

## Task 4: Digest formatter

**Files:**
- Modify: `src/bitgn_contest_agent/preflight/semantic_index.py`
- Modify: `tests/preflight/test_semantic_index.py`

- [ ] **Step 1: Append failing test**

Add to `tests/preflight/test_semantic_index.py`:

```python
from bitgn_contest_agent.preflight.semantic_index import format_digest


def test_format_digest_includes_both_blocks_and_semantic_contrast():
    from bitgn_contest_agent.preflight.semantic_index import (
        extract_cast_entries, extract_project_entries,
    )
    cast = extract_cast_entries(FIXTURE / "10_entities" / "cast")
    projects = extract_project_entries(FIXTURE / "40_projects")
    digest = format_digest(cast=cast, projects=projects)

    assert "WORKSPACE SEMANTIC INDEX" in digest
    assert "CAST:" in digest
    assert "PROJECTS:" in digest
    # Semantic contrast visible on one line each:
    assert "entity.nina" in digest
    assert "startup_partner" in digest
    assert "narrow the product" in digest
    assert "entity.elena" in digest
    assert "day_job_ceo" in digest
    assert "project.harbor_body" in digest
    assert "lane=health" in digest
    assert "project.black_library_evenings" in digest
    assert "lane=family" in digest


def test_format_digest_omits_empty_blocks():
    digest = format_digest(cast=[], projects=[])
    # Nothing to index → empty string (caller suppresses).
    assert digest == ""


def test_format_digest_cast_only_when_no_projects():
    from bitgn_contest_agent.preflight.semantic_index import extract_cast_entries
    cast = extract_cast_entries(FIXTURE / "10_entities" / "cast")
    digest = format_digest(cast=cast, projects=[])
    assert "CAST:" in digest
    assert "PROJECTS:" not in digest
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/preflight/test_semantic_index.py -v`
Expected: FAIL with `ImportError: cannot import name 'format_digest'`

- [ ] **Step 3: Extend the module**

Append to `src/bitgn_contest_agent/preflight/semantic_index.py`:

```python
_HEADER = (
    "WORKSPACE SEMANTIC INDEX (cast + projects digest, use to map "
    "informal descriptors like \"the founder I talk product with\" or "
    "\"the do-not-degrade lane\" to canonical ids before running any "
    "lookup):"
)


def _fmt_kv(key: str, value: str) -> str:
    """Render `key=value` only when value is non-empty."""
    return f"{key}={value}" if value else ""


def _fmt_cast_line(e: CastEntry) -> str:
    parts = [f"- {e.id}", _fmt_kv("alias", e.alias), _fmt_kv("relationship", e.relationship)]
    if e.kind:
        parts.append(_fmt_kv("kind", e.kind))
    head = "  ".join(p for p in parts if p)
    summary = f'  "{e.summary}"' if e.summary else ""
    return head + summary


def _fmt_project_line(e: ProjectEntry) -> str:
    parts = [
        f"- {e.id}",
        _fmt_kv("alias", e.alias),
        _fmt_kv("lane", e.lane),
        _fmt_kv("status", e.status),
    ]
    head = "  ".join(p for p in parts if p)
    goal = f'  "{e.goal}"' if e.goal else ""
    return head + goal


def format_digest(
    *,
    cast: List[CastEntry],
    projects: List[ProjectEntry],
    cast_cap: int = 100,
    project_cap: int = 100,
) -> str:
    """Return the bootstrap string the adapter appends to prepass output.
    Empty inputs (both blocks empty) → empty string so the caller can
    suppress the message entirely.
    """
    if not cast and not projects:
        return ""
    blocks: list[str] = [_HEADER]
    if cast:
        lines = [_fmt_cast_line(e) for e in cast[:cast_cap]]
        if len(cast) > cast_cap:
            lines.append(f"  …(+{len(cast) - cast_cap} more)")
        blocks.append("CAST:\n" + "\n".join(lines))
    if projects:
        lines = [_fmt_project_line(e) for e in projects[:project_cap]]
        if len(projects) > project_cap:
            lines.append(f"  …(+{len(projects) - project_cap} more)")
        blocks.append("PROJECTS:\n" + "\n".join(lines))
    return "\n\n".join(blocks)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/preflight/test_semantic_index.py -v`
Expected: PASS for all `test_format_digest_*`

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/semantic_index.py tests/preflight/test_semantic_index.py
git commit -m "feat(descriptor): digest formatter with cast/project blocks"
```

---

## Task 5: Filesystem entry point

**Files:**
- Modify: `src/bitgn_contest_agent/preflight/semantic_index.py`
- Modify: `tests/preflight/test_semantic_index.py`

- [ ] **Step 1: Append failing test**

Add to `tests/preflight/test_semantic_index.py`:

```python
from bitgn_contest_agent.preflight.semantic_index import build_digest_from_fs


def test_build_digest_from_fs_composes_both_blocks():
    digest = build_digest_from_fs(
        root=FIXTURE,
        entities_root="10_entities",
        projects_root="40_projects",
    )
    assert "CAST:" in digest
    assert "PROJECTS:" in digest
    assert "entity.nina" in digest
    assert "project.harbor_body" in digest


def test_build_digest_from_fs_no_roots_returns_empty_string():
    digest = build_digest_from_fs(
        root=FIXTURE, entities_root=None, projects_root=None,
    )
    assert digest == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/preflight/test_semantic_index.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_digest_from_fs'`

- [ ] **Step 3: Extend the module**

Append to `src/bitgn_contest_agent/preflight/semantic_index.py`:

```python
def build_digest_from_fs(
    *,
    root: Path,
    entities_root: Optional[str],
    projects_root: Optional[str],
) -> str:
    """Filesystem-backed composer — used by tests and by the PCM
    wrapper's fs fallback. Returns an empty string when neither root
    is present so the adapter can suppress the bootstrap message.

    `entities_root` is the top-level 10_entities path; this function
    looks for a `cast/` subdirectory inside it (PROD convention). If no
    `cast/` subdir exists, it falls back to the entities root itself.
    """
    root = Path(root)
    cast_entries: list[CastEntry] = []
    project_entries: list[ProjectEntry] = []
    if entities_root:
        ent_path = root / entities_root
        cast_dir = ent_path / "cast"
        if cast_dir.is_dir():
            cast_entries = extract_cast_entries(cast_dir)
        else:
            cast_entries = extract_cast_entries(ent_path)
    if projects_root:
        proj_path = root / projects_root
        project_entries = extract_project_entries(proj_path)
    return format_digest(cast=cast_entries, projects=project_entries)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/preflight/test_semantic_index.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/semantic_index.py tests/preflight/test_semantic_index.py
git commit -m "feat(descriptor): fs entry point build_digest_from_fs"
```

---

## Task 6: PCM-backed entry point

**Files:**
- Modify: `src/bitgn_contest_agent/preflight/semantic_index.py`
- Modify: `tests/preflight/test_semantic_index.py`

- [ ] **Step 1: Append failing test**

Add to `tests/preflight/test_semantic_index.py`:

```python
from unittest.mock import MagicMock

from bitgn_contest_agent.preflight.semantic_index import run_preflight_semantic_index
from bitgn_contest_agent.preflight.schema import WorkspaceSchema


def _mk_pcm_stub_for_fixture():
    """Stub a PcmRuntime that walks the on-disk fixture. Uses list/read
    RPCs exactly as run_preflight_semantic_index does."""
    runtime = MagicMock()

    def _list(req):
        entries = []
        p = FIXTURE / req.name
        if p.is_dir():
            for child in sorted(p.iterdir()):
                e = MagicMock()
                e.name = child.name
                e.is_dir = child.is_dir()
                entries.append(e)
        resp = MagicMock()
        resp.entries = entries
        return resp

    def _read(req):
        p = FIXTURE / req.path
        resp = MagicMock()
        resp.content = p.read_text(encoding="utf-8") if p.is_file() else ""
        return resp

    runtime.list.side_effect = _list
    runtime.read.side_effect = _read
    return runtime


def test_run_preflight_semantic_index_returns_bootstrap_via_pcm():
    runtime = _mk_pcm_stub_for_fixture()
    schema = WorkspaceSchema(
        entities_root="10_entities",
        projects_root="40_projects",
    )
    result = run_preflight_semantic_index(runtime, schema)
    assert result.ok is True
    assert "WORKSPACE SEMANTIC INDEX" in (result.content or "")
    assert "entity.nina" in result.content
    assert "project.harbor_body" in result.content


def test_run_preflight_semantic_index_empty_schema_is_ok_empty_content():
    runtime = MagicMock()
    schema = WorkspaceSchema()
    result = run_preflight_semantic_index(runtime, schema)
    assert result.ok is True
    assert result.content == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/preflight/test_semantic_index.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_preflight_semantic_index'`

- [ ] **Step 3: Extend the module**

Append to `src/bitgn_contest_agent/preflight/semantic_index.py`:

```python
from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.schema import WorkspaceSchema


def _list_md_names_via_pcm(client, dir_path: str) -> list[str]:
    """Return the .md/.MD filenames in `dir_path` via the PCM list RPC."""
    from bitgn.vm import pcm_pb2
    try:
        resp = client.list(pcm_pb2.ListRequest(name=dir_path))
    except Exception:
        return []
    return [
        e.name for e in resp.entries
        if not e.is_dir and e.name.lower().endswith(".md")
    ]


def _list_subdirs_via_pcm(client, dir_path: str) -> list[str]:
    from bitgn.vm import pcm_pb2
    try:
        resp = client.list(pcm_pb2.ListRequest(name=dir_path))
    except Exception:
        return []
    return [e.name for e in resp.entries if e.is_dir]


def _read_text_via_pcm(client, path: str) -> str:
    from bitgn.vm import pcm_pb2
    try:
        resp = client.read(pcm_pb2.ReadRequest(path=path))
    except Exception:
        return ""
    return resp.content or ""


def _extract_cast_via_pcm(client, cast_dir: str) -> list[CastEntry]:
    entries: list[CastEntry] = []
    md_names = _list_md_names_via_pcm(client, cast_dir)
    for name in sorted(md_names):
        full = f"{cast_dir}/{name}"
        text = _read_text_via_pcm(client, full)
        if not text:
            continue
        md = parse_record_metadata(text)
        if not md:
            continue
        stem = name.rsplit(".", 1)[0].lower()
        alias = md.get("alias") or stem
        entries.append(CastEntry(
            id=f"entity.{stem}",
            alias=alias,
            relationship=md.get("relationship", ""),
            kind=md.get("kind", ""),
            summary=_first_prose_line(text),
        ))
    return entries


def _strip_date_prefix(name: str) -> str:
    parts = name.split("_", 3)
    if len(parts) == 4 and all(p.isdigit() for p in parts[:3]):
        return parts[3]
    return name


def _extract_projects_via_pcm(client, projects_dir: str) -> list[ProjectEntry]:
    entries: list[ProjectEntry] = []
    subdirs = _list_subdirs_via_pcm(client, projects_dir)
    for sub in sorted(subdirs):
        sub_path = f"{projects_dir}/{sub}"
        md_names = _list_md_names_via_pcm(client, sub_path)
        readme_name = None
        for candidate in ("README.md", "README.MD", "readme.md"):
            if candidate in md_names:
                readme_name = candidate
                break
        if readme_name is None:
            continue
        text = _read_text_via_pcm(client, f"{sub_path}/{readme_name}")
        if not text:
            continue
        md = parse_record_metadata(text)
        if not md:
            continue
        goal_field = md.get("goal", "").strip()
        goal = goal_field[:_SUMMARY_MAX] if goal_field else _first_prose_line(text)
        alias = md.get("alias") or sub
        entries.append(ProjectEntry(
            id=f"project.{_strip_date_prefix(sub)}",
            alias=alias,
            lane=md.get("lane", ""),
            status=md.get("status", ""),
            goal=goal,
        ))
    return entries


def run_preflight_semantic_index(client, schema: WorkspaceSchema) -> ToolResult:
    """PCM-backed entry point. Consumes a schema produced by
    `run_preflight_schema` and returns a ToolResult whose `content` is
    the bootstrap digest (or empty string when no roots are available).
    """
    cast: list[CastEntry] = []
    projects: list[ProjectEntry] = []
    try:
        if schema.entities_root:
            cast_dir = f"{schema.entities_root}/cast"
            cast = _extract_cast_via_pcm(client, cast_dir)
            if not cast:
                # Fallback: walk entities_root directly (non-PROD shape).
                cast = _extract_cast_via_pcm(client, schema.entities_root)
        if schema.projects_root:
            projects = _extract_projects_via_pcm(client, schema.projects_root)
    except Exception as exc:
        return ToolResult(
            ok=False, content="", refs=tuple(), error=str(exc),
            error_code="INTERNAL", wall_ms=0,
        )
    digest = format_digest(cast=cast, projects=projects)
    return ToolResult(
        ok=True, content=digest, refs=tuple(),
        error=None, error_code=None, wall_ms=0,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/preflight/test_semantic_index.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/semantic_index.py tests/preflight/test_semantic_index.py
git commit -m "feat(descriptor): PCM entry point run_preflight_semantic_index"
```

---

## Task 7: Dispatch wiring

**Files:**
- Modify: `src/bitgn_contest_agent/adapter/pcm.py`

- [ ] **Step 1: Locate the `Req_PreflightSchema` dispatch branch**

In `src/bitgn_contest_agent/adapter/pcm.py` near line 212, find:

```python
            if isinstance(req, Req_PreflightSchema):
                from bitgn_contest_agent.preflight.schema import run_preflight_schema
                return run_preflight_schema(self._runtime, None)
```

- [ ] **Step 2: Add the imports at the top of the file**

Check the existing `from bitgn_contest_agent.schemas import ...` block at the top. Add `Req_PreflightSemanticIndex` to that import list.

```python
from bitgn_contest_agent.schemas import (
    Req_Context,
    Req_Find,
    Req_List,
    Req_MkDir,
    Req_Move,
    Req_PreflightSchema,
    Req_PreflightSemanticIndex,
    Req_Read,
    Req_Search,
    Req_Tree,
    Req_Write,
    ReportTaskCompletion,
)
```

Reconcile this list against whatever is actually imported — only add the new name, don't remove or reorder existing ones.

- [ ] **Step 3: Insert a dispatch branch after the schema branch**

After the `Req_PreflightSchema` branch in `dispatch()`, insert:

```python
            if isinstance(req, Req_PreflightSemanticIndex):
                from bitgn_contest_agent.preflight.schema import (
                    parse_schema_content, run_preflight_schema,
                )
                from bitgn_contest_agent.preflight.semantic_index import (
                    run_preflight_semantic_index,
                )
                # The adapter's `dispatch` is stateless — it may be called
                # with no prior schema in hand (e.g. from a unit test). In
                # that case, run schema first so we have the roots.
                schema_result = run_preflight_schema(self._runtime, None)
                schema = parse_schema_content(schema_result.content)
                return run_preflight_semantic_index(self._runtime, schema)
```

- [ ] **Step 4: Run existing dispatch tests to confirm no regression**

Run: `pytest tests/adapter/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/adapter/pcm.py
git commit -m "feat(descriptor): dispatch Req_PreflightSemanticIndex"
```

---

## Task 8: Prepass integration

**Files:**
- Modify: `src/bitgn_contest_agent/adapter/pcm.py`
- Create: `tests/adapter/test_pcm_prepass_semantic_index.py`

- [ ] **Step 1: Write the failing test**

`tests/adapter/test_pcm_prepass_semantic_index.py`:

```python
"""Verify PcmAdapter.run_prepass emits a semantic-index bootstrap
message after the schema bootstrap, when schema roots are present."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult
from bitgn_contest_agent.trace_writer import TraceWriter


def test_run_prepass_appends_semantic_index_bootstrap(tmp_path, monkeypatch):
    runtime = MagicMock()
    runtime.tree.return_value = MagicMock(root=MagicMock(name="", is_dir=True, children=[]))
    runtime.read.return_value = MagicMock(content="")
    runtime.context.return_value = MagicMock()
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=65536)

    canned_schema = json.dumps({
        "summary": "ok",
        "data": {
            "inbox_root": None,
            "entities_root": "10_entities",
            "finance_roots": [],
            "projects_root": "40_projects",
            "outbox_root": None,
            "rulebook_root": None,
            "workflows_root": None,
            "schemas_root": None,
            "errors": [],
        },
    })
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.schema.run_preflight_schema",
        lambda client, ctx: ToolResult(
            ok=True, content=canned_schema, refs=(),
            error=None, error_code=None, wall_ms=1,
        ),
    )
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.semantic_index.run_preflight_semantic_index",
        lambda client, schema: ToolResult(
            ok=True,
            content="WORKSPACE SEMANTIC INDEX …\nCAST:\n- entity.nina  alias=nina",
            refs=(), error=None, error_code=None, wall_ms=1,
        ),
    )

    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path=path)
    session = MagicMock()
    session.identity_loaded = False
    session.rulebook_loaded = False
    session.seen_refs = set()

    prepass = adapter.run_prepass(session=session, trace_writer=writer)
    writer.close()

    # Two bootstrap messages: schema first, semantic index second.
    assert len(prepass.bootstrap_content) == 2
    assert "WORKSPACE SCHEMA" in prepass.bootstrap_content[0]
    assert "WORKSPACE SEMANTIC INDEX" in prepass.bootstrap_content[1]

    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    cmds = [r.get("cmd") for r in records]
    assert "preflight_semantic_index" in cmds


def test_run_prepass_suppresses_semantic_index_when_empty(tmp_path, monkeypatch):
    runtime = MagicMock()
    runtime.tree.return_value = MagicMock(root=MagicMock(name="", is_dir=True, children=[]))
    runtime.read.return_value = MagicMock(content="")
    runtime.context.return_value = MagicMock()
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=65536)

    canned_schema = json.dumps({
        "summary": "ok",
        "data": {
            "inbox_root": None,
            "entities_root": None,
            "finance_roots": [],
            "projects_root": None,
            "outbox_root": None,
            "rulebook_root": None,
            "workflows_root": None,
            "schemas_root": None,
            "errors": [],
        },
    })
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.schema.run_preflight_schema",
        lambda client, ctx: ToolResult(
            ok=True, content=canned_schema, refs=(),
            error=None, error_code=None, wall_ms=1,
        ),
    )
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.semantic_index.run_preflight_semantic_index",
        lambda client, schema: ToolResult(
            ok=True, content="", refs=(), error=None,
            error_code=None, wall_ms=1,
        ),
    )

    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path=path)
    session = MagicMock()
    session.identity_loaded = False
    session.rulebook_loaded = False
    session.seen_refs = set()

    prepass = adapter.run_prepass(session=session, trace_writer=writer)
    writer.close()

    # Empty semantic-index content → no bootstrap entry for it.
    # Schema bootstrap still present.
    assert len(prepass.bootstrap_content) == 1
    assert "WORKSPACE SCHEMA" in prepass.bootstrap_content[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/adapter/test_pcm_prepass_semantic_index.py -v`
Expected: FAIL — `bootstrap_content` has 1 entry (schema only) because the semantic index is not wired yet.

- [ ] **Step 3: Extend `run_prepass` in `src/bitgn_contest_agent/adapter/pcm.py`**

Locate the `pre_cmds` list around line 277-282:

```python
        pre_cmds = [
            ("tree", Req_Tree(tool="tree", root="/")),
            ("read_agents_md", Req_Read(tool="read", path="AGENTS.md")),
            ("context", Req_Context(tool="context")),
            ("preflight_schema", Req_PreflightSchema(tool="preflight_schema")),
        ]
```

This list stays the same — the semantic-index call happens AFTER the loop so it can reuse the parsed schema. After the `for label, req in pre_cmds:` loop body (after line ~319, before the `return PrepassResult(...)`), add:

```python
        # Phase 2: semantic index — depends on schema roots discovered above.
        parsed_schema = parse_schema_content(schema_content)
        if parsed_schema.entities_root or parsed_schema.projects_root:
            with pcm_origin("prepass"):
                from bitgn_contest_agent.preflight.semantic_index import (
                    run_preflight_semantic_index,
                )
                t0 = time.perf_counter()
                try:
                    si_result = run_preflight_semantic_index(
                        self._runtime, parsed_schema,
                    )
                except Exception as exc:
                    si_result = ToolResult(
                        ok=False, content="", refs=tuple(), error=str(exc),
                        error_code="INTERNAL", wall_ms=0,
                    )
                wall_ms = int((time.perf_counter() - t0) * 1000)
                if si_result.ok and si_result.content:
                    bootstrap_content.append(si_result.content)
                trace_writer.append_prepass(
                    cmd="preflight_semantic_index",
                    ok=si_result.ok,
                    bytes=len(si_result.content or ""),
                    wall_ms=wall_ms,
                    error=si_result.error,
                    error_code=si_result.error_code,
                    schema_roots=None,
                )
```

Note: `time` and `parse_schema_content` are already imported at the top of the file in the existing prepass implementation. If not, add:

```python
import time
from bitgn_contest_agent.preflight.schema import parse_schema_content
```

— but check before duplicating. The existing code in `run_prepass` imports `parse_schema_content` locally around line 273; hoist the import if the Phase 2 block also needs it at the same indentation.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/adapter/test_pcm_prepass_semantic_index.py -v`
Expected: PASS for both cases.

- [ ] **Step 5: Re-run the existing schema-roots test to confirm no regression**

Run: `pytest tests/adapter/test_pcm_prepass_schema_roots.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/adapter/pcm.py tests/adapter/test_pcm_prepass_semantic_index.py
git commit -m "feat(descriptor): prepass wiring — semantic index appended after schema"
```

---

## Task 9: Full test suite + push

**Files:**
- None (validation task)

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -x`
Expected: PASS — if any failure surfaces that's unrelated to this work, stop and escalate. Do not skip tests.

- [ ] **Step 2: Push**

```bash
git push
```

Expected: fast-forward push to `fix/descriptor-resolution`.

---

## Self-review notes

- **Spec coverage:** Schema shape (Task 4) covers the bootstrap format in the spec's "Bootstrap message format" section. Extractors (Tasks 2–3) cover field shapes. FS + PCM entry points (Tasks 5–6) cover both test-path and runtime-path invocation. Dispatch + prepass wiring (Tasks 7–8) cover the integration in spec's "Shape" section.
- **Failure modes:** Spec's "Failure / truncation behavior" is covered: empty schema → empty digest (Task 4 + Task 8 test 2), malformed records skipped (Task 2 test), missing goal → prose fallback (Task 3), per-lane cap (Task 4 formatter with `cast_cap`/`project_cap` kwargs).
- **Open questions from spec:** Q1 ("formatter in module vs adapter") resolved in favor of module — `format_digest` and `run_preflight_semantic_index` both live in `preflight/semantic_index.py`, adapter just appends. Q2 (origin label) resolved in favor of `prepass` with `cmd="preflight_semantic_index"` — matches existing schema prepass pattern.
- **Out of scope deliberately:** Local bench replay and PROD validation happen after this plan lands; tracked as task #83.
