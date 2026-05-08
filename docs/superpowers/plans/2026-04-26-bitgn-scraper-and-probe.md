# BitGN Scraper Phases 1+2+3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the workspace-scrape, grader-probe, and self-validate phases of the BitGN local-harness scraper. After this plan ships and its PROD-integration step runs, the SQLite DB at `artifacts/harness_db/bitgn_local.db` will hold a per-task-instantiation grader rule set sufficient for Plan 2 (`bitgn_local_harness` gRPC server) to score agent submissions offline.

**Architecture:** Adds three modules to `src/bitgn_scraper/` (`workspace_walk`, `probes`, `validate`) plus a `scrape_runner` orchestrator. Wires three new CLI subcommands (`scrape`, `probe`, `validate`) into `scripts/bitgn_scraper.py`. Strategy B (one trial per probe with post-hoc instruction-text matching) is forced by the Phase 0 finding that PCM rejects a second `Answer` call.

**Tech Stack:** Same as Plan 1 — Python 3.12, `bitgn-local-sdk`, Connect-RPC sync clients, stdlib `sqlite3`, pytest.

**Spec reference:** `docs/superpowers/specs/2026-04-26-bitgn-local-harness-clone-design.md` — Phases 1, 2, 3.
**Phase 0 findings (input to this plan):** `artifacts/harness_db/scrape_runs/20260426_191138/SUMMARY.md`.

**Key Phase 0 facts that shape this plan:**
- Trials are **isolated** — a fresh trial returns a fresh workspace; writes don't leak.
- PCM enforces **one Answer per trial** at the wire (`ConnectError("Answer was already provided")`). Forces Strategy B.
- Workspaces are tiny (~130 KiB); no need for a multi-MB safety abort.
- Rotation, URL lifetime, auto-term, rate-limit are user-confirmed (see memory `project_prod_harness_lifecycle.md`).

**Out of scope for this plan:** Component 2 (gRPC server, Phase 4), Component 3 (LLM trace gate, Phase 5), the Phase 6 acceptance run.

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/bitgn_scraper/workspace_walk.py` | Tree walker — given a PCM client, produces `list[FileRecord]` for the live workspace. Used by Phase 1 + Phase 3 integrity check. |
| `src/bitgn_scraper/scrape_runner.py` | Phase 1 orchestrator. Per-task loop with saturation heuristic; writes `task_instantiations` + `workspace_files` rows + flat files. |
| `src/bitgn_scraper/probes.py` | P1–P5 + P2b + P6 probe definitions; per-instantiation runner with adaptive stopping. Strategy B (one trial per probe). |
| `src/bitgn_scraper/probe_extract.py` | Maps `(probe_kind, score, score_detail)` → list of `scoring_rules` rows. Reuses `seed_rules.extract_rules` patterns; adds probe-specific extractors. |
| `src/bitgn_scraper/validate.py` | Phase 3 validators: determinism diff, workspace-file integrity (sha256 disk vs DB), probe coverage stats. Pure functions; CLI wraps them. |
| `scripts/bitgn_scraper.py` | Add subcommands: `scrape`, `probe`, `validate`. Each is a thin shim over the package. |
| `tests/scraper/test_workspace_walk.py` | Unit tests using a mock PCM client. |
| `tests/scraper/test_scrape_runner.py` | Saturation-heuristic + DB-row-shape unit tests with a fake harness/PCM. |
| `tests/scraper/test_probes.py` | Probe-runner unit tests with a fake harness/PCM that returns canned `score_detail`. |
| `tests/scraper/test_probe_extract.py` | Pattern-extraction unit tests on probe-style `score_detail` strings. |
| `tests/scraper/test_validate.py` | Determinism + integrity + coverage validator tests against an in-memory SQLite DB and tmp_path tree. |

**Library inventory (already installed via `bitgn-local-sdk`):** same as Plan 1. New gRPC types touched: none — all of `StartPlaygroundRequest`, `EndTrialRequest`, `TreeRequest`, `ReadRequest`, `AnswerRequest`, `Outcome`, `ContextRequest`, `WriteRequest` are already imported by Plan 1's `phase0.py`.

---

### Task 1: Workspace tree walker (`src/bitgn_scraper/workspace_walk.py`)

Generalises `phase0._walk_tree_byte_total` into a reusable helper that returns a `list[FileRecord]` (already defined in `fingerprint.py`). Phase 1 uses this to scrape a workspace; Phase 3 uses it to re-walk and compare hashes.

**Files:**
- Create: `src/bitgn_scraper/workspace_walk.py`
- Create: `tests/scraper/test_workspace_walk.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_workspace_walk.py
"""Workspace tree walker tests using a fake PCM client."""
from __future__ import annotations

from dataclasses import dataclass, field

from bitgn_scraper.workspace_walk import walk_workspace


@dataclass
class _FakeEntry:
    name: str
    is_dir: bool
    children: list["_FakeEntry"] = field(default_factory=list)


@dataclass
class _FakeReadResp:
    content: str


@dataclass
class _FakeTreeResp:
    root: _FakeEntry


class _FakePcm:
    def __init__(self, tree: _FakeEntry, files: dict[str, str]) -> None:
        self._tree = tree
        self._files = files

    def tree(self, req):  # noqa: ARG002
        return _FakeTreeResp(root=self._tree)

    def read(self, req):
        path = req.path
        if path not in self._files:
            raise KeyError(path)
        return _FakeReadResp(content=self._files[path])


def test_walk_workspace_reads_two_files() -> None:
    tree = _FakeEntry(name="/", is_dir=True, children=[
        _FakeEntry(name="a.md", is_dir=False),
        _FakeEntry(name="sub", is_dir=True, children=[
            _FakeEntry(name="b.md", is_dir=False),
        ]),
    ])
    files = {"/a.md": "alpha", "/sub/b.md": "bravo"}
    pcm = _FakePcm(tree, files)

    records = walk_workspace(pcm)
    paths = sorted(r.path for r in records)
    assert paths == ["/a.md", "/sub/b.md"]
    rec_a = next(r for r in records if r.path == "/a.md")
    assert rec_a.byte_size == 5
    assert len(rec_a.sha256) == 64


def test_walk_workspace_skips_unreadable_with_marker() -> None:
    tree = _FakeEntry(name="/", is_dir=True, children=[
        _FakeEntry(name="a.md", is_dir=False),
        _FakeEntry(name="bad.bin", is_dir=False),
    ])
    files = {"/a.md": "alpha"}  # /bad.bin missing → KeyError on read
    pcm = _FakePcm(tree, files)

    records = walk_workspace(pcm)
    bad = next(r for r in records if r.path == "/bad.bin")
    assert bad.byte_size == 0
    assert bad.sha256 == "READ_ERROR"
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_workspace_walk.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement `workspace_walk.py`**

```python
# src/bitgn_scraper/workspace_walk.py
"""Walk a live PCM workspace and produce FileRecord rows.

Used by Phase 1 (initial scrape) and Phase 3 (integrity check).
A file that fails to read is recorded with byte_size=0, sha256='READ_ERROR'
so the scrape doesn't abort on a single bad file.
"""
from __future__ import annotations

import hashlib
from typing import Any

from bitgn_scraper.fingerprint import FileRecord


def walk_workspace(pcm: Any) -> list[FileRecord]:
    """Return a FileRecord per file in the workspace rooted at /."""
    from bitgn.vm.pcm_pb2 import ReadRequest, TreeRequest
    from connectrpc.errors import ConnectError

    tree_resp = pcm.tree(TreeRequest(root="/"))

    records: list[FileRecord] = []
    _collect(pcm, tree_resp.root, "", records, ConnectError)
    return records


def _collect(pcm: Any, entry: Any, prefix: str, out: list[FileRecord], rpc_error: type) -> None:
    """Recursive helper. Mutates `out`."""
    name = entry.name or ""
    path = prefix + ("/" + name if name and name != "/" else "")
    if entry.is_dir:
        for child in entry.children:
            _collect(pcm, child, path, out, rpc_error)
        return

    from bitgn.vm.pcm_pb2 import ReadRequest

    file_path = path or "/"
    try:
        resp = pcm.read(ReadRequest(path=file_path))
        content_bytes = resp.content.encode("utf-8")
        out.append(FileRecord(
            path=file_path,
            sha256=hashlib.sha256(content_bytes).hexdigest(),
            byte_size=len(content_bytes),
        ))
    except (rpc_error, KeyError, OSError):
        out.append(FileRecord(
            path=file_path,
            sha256="READ_ERROR",
            byte_size=0,
        ))
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_workspace_walk.py -v
```

Expected: PASS — 2 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_scraper/workspace_walk.py tests/scraper/test_workspace_walk.py
git commit -m "feat(scraper): workspace tree walker"
git push
```

---

### Task 2: Phase 1 scrape runner (`src/bitgn_scraper/scrape_runner.py`)

Per-task loop. For each task_id: repeatedly StartPlayground, walk workspace, compute instantiation_hash, persist if new; saturate when 5 consecutive calls produce only known hashes (cap at 30 attempts/task).

**Files:**
- Create: `src/bitgn_scraper/scrape_runner.py`
- Create: `tests/scraper/test_scrape_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_scrape_runner.py
"""Phase 1 scrape-runner tests with a fake harness/PCM pair."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from bitgn_scraper.db import init_schema
from bitgn_scraper.scrape_runner import scrape_task


@dataclass
class _Played:
    instruction: str
    trial_id: str
    harness_url: str


@dataclass
class _Tree:
    root: object


@dataclass
class _Entry:
    name: str
    is_dir: bool
    children: list["_Entry"] = field(default_factory=list)


@dataclass
class _Read:
    content: str


@dataclass
class _Ctx:
    time: str = "2026-04-26T12:00:00Z"
    unix_time: int = 1745668800


@dataclass
class _End:
    score: float = 0.0
    score_detail: list[str] = field(default_factory=list)


class _FakeHarness:
    def __init__(self, instruction_sequence: list[str]) -> None:
        self._instructions = list(instruction_sequence)
        self.start_calls = 0
        self.end_calls = 0

    def start_playground(self, req):  # noqa: ARG002
        idx = self.start_calls
        self.start_calls += 1
        instr = self._instructions[idx % len(self._instructions)]
        return _Played(
            instruction=instr,
            trial_id=f"trial_{idx}",
            harness_url=f"https://vm-{idx}.example",
        )

    def end_trial(self, req):  # noqa: ARG002
        self.end_calls += 1
        return _End()


class _FakePcmFactory:
    """Returns the same fake PCM for any harness_url."""

    def __init__(self) -> None:
        self.tree_root = _Entry(name="/", is_dir=True, children=[
            _Entry(name="a.md", is_dir=False),
        ])
        self.file_content = "alpha"

    def __call__(self, harness_url: str):  # noqa: ARG002
        outer = self

        class _Pcm:
            def tree(self, req):  # noqa: ARG002
                return _Tree(root=outer.tree_root)

            def read(self, req):  # noqa: ARG002
                return _Read(content=outer.file_content)

            def context(self, req=None):  # noqa: ARG002
                return _Ctx()

        return _Pcm()


def test_scrape_task_writes_one_instantiation(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    harness = _FakeHarness(["instruction A"])  # always returns same → only 1 instantiation
    factory = _FakePcmFactory()

    n_new = scrape_task(
        harness_client=harness,
        pcm_factory=factory,
        task_id="t001",
        benchmark_id="bitgn/pac1-prod",
        db_path=db_path,
        workspace_root=tmp_path / "workspaces",
        max_attempts=10,
        saturation_threshold=3,
    )

    assert n_new == 1
    # 1 new + 3 duplicate-saturating attempts = 4 start_playground calls
    assert harness.start_calls == 4
    # All trials are end_trial-ed
    assert harness.end_calls == 4

    with sqlite3.connect(db_path) as cx:
        rows = cx.execute("SELECT task_id, instruction, workspace_byte_total FROM task_instantiations").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "t001"
    assert rows[0][1] == "instruction A"
    assert rows[0][2] == 5  # len("alpha")


def test_scrape_task_captures_two_distinct_instantiations(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    # Alternate two distinct instructions → two distinct instantiation_hashes
    harness = _FakeHarness(["instruction A", "instruction B"])
    factory = _FakePcmFactory()

    n_new = scrape_task(
        harness_client=harness,
        pcm_factory=factory,
        task_id="t001",
        benchmark_id="bitgn/pac1-prod",
        db_path=db_path,
        workspace_root=tmp_path / "workspaces",
        max_attempts=20,
        saturation_threshold=5,
    )

    assert n_new == 2
    with sqlite3.connect(db_path) as cx:
        instr = sorted(r[0] for r in cx.execute("SELECT instruction FROM task_instantiations"))
    assert instr == ["instruction A", "instruction B"]
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_scrape_runner.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement `scrape_runner.py`**

```python
# src/bitgn_scraper/scrape_runner.py
"""Phase 1 scrape orchestrator — per-task loop with saturation heuristic.

For each task_id:
  - Repeatedly StartPlayground, walk workspace, compute instantiation_hash.
  - Stop after `saturation_threshold` consecutive duplicates, or `max_attempts` total.
  - Persist new instantiations to SQLite + flat files; EndTrial after each call.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bitgn_scraper.fingerprint import FileRecord, instantiation_hash
from bitgn_scraper.workspace_walk import walk_workspace


def scrape_task(
    *,
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_id: str,
    benchmark_id: str,
    db_path: Path,
    workspace_root: Path,
    max_attempts: int = 30,
    saturation_threshold: int = 5,
) -> int:
    """Scrape one task; return count of NEW instantiations persisted."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ContextRequest

    seen: set[str] = _load_existing_hashes(db_path, task_id)
    new_count = 0
    consecutive_dupes = 0

    for attempt in range(max_attempts):
        started = harness_client.start_playground(
            StartPlaygroundRequest(benchmark_id=benchmark_id, task_id=task_id)
        )
        try:
            pcm = pcm_factory(started.harness_url)
            ctx = pcm.context(ContextRequest())
            files = walk_workspace(pcm)
            inst_hash = instantiation_hash(started.instruction, files)

            if inst_hash in seen:
                consecutive_dupes += 1
                if consecutive_dupes >= saturation_threshold:
                    break
                continue

            seen.add(inst_hash)
            consecutive_dupes = 0
            new_count += 1
            _persist_instantiation(
                db_path=db_path,
                workspace_root=workspace_root,
                task_id=task_id,
                instantiation_hash_=inst_hash,
                instruction=started.instruction,
                ctx_time=ctx.time,
                ctx_unix=ctx.unix_time,
                benchmark_id=benchmark_id,
                files=files,
                pcm=pcm,
            )
        finally:
            harness_client.end_trial(EndTrialRequest(trial_id=started.trial_id))

    return new_count


def _load_existing_hashes(db_path: Path, task_id: str) -> set[str]:
    with sqlite3.connect(db_path) as cx:
        rows = cx.execute(
            "SELECT instantiation_hash FROM task_instantiations WHERE task_id = ?",
            (task_id,),
        ).fetchall()
    return {r[0] for r in rows}


def _persist_instantiation(
    *,
    db_path: Path,
    workspace_root: Path,
    task_id: str,
    instantiation_hash_: str,
    instruction: str,
    ctx_time: str,
    ctx_unix: int,
    benchmark_id: str,
    files: list[FileRecord],
    pcm: Any,
) -> None:
    """Write the row, the workspace_files index, and the flat files."""
    from bitgn.vm.pcm_pb2 import ReadRequest

    instruction_hash = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    tree_fp = _tree_fingerprint(files)
    workspace_dir_rel = f"{task_id}/{instantiation_hash_[:12]}"
    workspace_dir_abs = workspace_root / workspace_dir_rel
    workspace_dir_abs.mkdir(parents=True, exist_ok=True)

    byte_total = sum(f.byte_size for f in files)
    file_count = sum(1 for f in files if f.path != "/")
    scraped_at = datetime.now(tz=timezone.utc).isoformat()

    # Dump _meta.json
    (workspace_dir_abs / "_meta.json").write_text(json.dumps({
        "task_id": task_id,
        "instantiation_hash": instantiation_hash_,
        "instruction": instruction,
        "context_time": ctx_time,
        "scraped_at": scraped_at,
    }, indent=2, sort_keys=True))

    # Re-Read each file (workspace_walk only kept hashes/sizes) and dump to disk
    for rec in files:
        if rec.sha256 == "READ_ERROR":
            continue
        rel = rec.path.lstrip("/")
        if not rel:
            continue
        out = workspace_dir_abs / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = pcm.read(ReadRequest(path=rec.path))
            out.write_text(resp.content, encoding="utf-8")
        except Exception:
            # Already recorded as READ_ERROR upstream; skip disk write.
            pass

    with sqlite3.connect(db_path) as cx:
        cx.execute("PRAGMA foreign_keys = ON")
        cx.execute(
            """
            INSERT INTO task_instantiations
            (task_id, instantiation_hash, instruction, instruction_hash,
             tree_fingerprint, context_time, context_unix, benchmark_id,
             scraped_at, workspace_dir, workspace_byte_total, workspace_file_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, instantiation_hash_, instruction, instruction_hash,
                tree_fp, ctx_time, ctx_unix, benchmark_id,
                scraped_at, workspace_dir_rel, byte_total, file_count,
            ),
        )
        cx.executemany(
            """
            INSERT INTO workspace_files
            (task_id, instantiation_hash, path, is_dir, byte_size, sha256)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (task_id, instantiation_hash_, f.path, 0, f.byte_size, f.sha256)
                for f in files
            ],
        )
        cx.commit()


def _tree_fingerprint(files: list[FileRecord]) -> str:
    """SHA-256 over the sorted manifest. Stable across runs."""
    from bitgn_scraper.fingerprint import tree_fingerprint
    return tree_fingerprint(files)
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_scrape_runner.py -v
```

Expected: PASS — 2 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_scraper/scrape_runner.py tests/scraper/test_scrape_runner.py
git commit -m "feat(scraper): Phase 1 scrape orchestrator"
git push
```

---

### Task 3: Wire `scrape` CLI subcommand into `scripts/bitgn_scraper.py`

Adds a `scrape` subcommand that loops over the benchmark's task list and calls `scrape_task` for each. Reads `BITGN_API_KEY` and `BITGN_BASE_URL` like `phase0`. Writes results to the existing SQLite DB.

**Files:**
- Modify: `scripts/bitgn_scraper.py`
- Create: `src/bitgn_scraper/scrape_cli.py` — thin orchestrator (keeps `bitgn_scraper.py` thin)
- Create: `tests/scraper/test_scrape_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_scrape_cli.py
"""Smoke test that the scrape CLI argparses and dispatches."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bitgn_scraper.scrape_cli import build_parser


def test_scrape_cli_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.benchmark_id == "bitgn/pac1-prod"
    assert args.max_attempts == 30
    assert args.saturation_threshold == 5
    assert args.db_path == Path("artifacts/harness_db/bitgn_local.db")


def test_scrape_cli_parser_task_filter() -> None:
    parser = build_parser()
    args = parser.parse_args(["--task-ids", "t001,t002"])
    assert args.task_ids == "t001,t002"
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_scrape_cli.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement `src/bitgn_scraper/scrape_cli.py`**

```python
# src/bitgn_scraper/scrape_cli.py
"""Phase 1 CLI shim. Lists tasks via GetBenchmark, scrapes each."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bitgn_scraper scrape")
    p.add_argument("--benchmark-id", default="bitgn/pac1-prod")
    p.add_argument("--task-ids", default="",
                   help="comma-separated subset; empty = all tasks from GetBenchmark")
    p.add_argument("--max-attempts", type=int, default=30)
    p.add_argument("--saturation-threshold", type=int, default=5)
    p.add_argument("--db-path", type=Path,
                   default=Path("artifacts/harness_db/bitgn_local.db"))
    p.add_argument("--workspace-root", type=Path,
                   default=Path("artifacts/harness_db/workspaces"))
    return p


def run_scrape_cli() -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[2:])

    from bitgn_scraper.clients import build_harness_client, build_pcm_client
    from bitgn_scraper.db import init_schema
    from bitgn_scraper.scrape_runner import scrape_task

    init_schema(args.db_path)
    harness = build_harness_client()
    task_ids = _resolve_task_ids(harness, args.benchmark_id, args.task_ids)

    print(f"[scrape] {len(task_ids)} task(s) → {args.db_path}", flush=True)
    total_new = 0
    for i, tid in enumerate(task_ids, 1):
        print(f"[scrape] ({i}/{len(task_ids)}) {tid}", flush=True)
        n = scrape_task(
            harness_client=harness,
            pcm_factory=build_pcm_client,
            task_id=tid,
            benchmark_id=args.benchmark_id,
            db_path=args.db_path,
            workspace_root=args.workspace_root,
            max_attempts=args.max_attempts,
            saturation_threshold=args.saturation_threshold,
        )
        total_new += n
        print(f"  → {n} new instantiation(s)", flush=True)

    print(f"[scrape] done — {total_new} new instantiation(s) total", flush=True)
    return 0


def _resolve_task_ids(harness: Any, benchmark_id: str, override: str) -> list[str]:
    if override:
        return [t.strip() for t in override.split(",") if t.strip()]
    from bitgn.harness_pb2 import GetBenchmarkRequest
    resp = harness.get_benchmark(GetBenchmarkRequest(benchmark_id=benchmark_id))
    return list(resp.task_ids)
```

- [ ] **Step 4: Wire subcommand into `scripts/bitgn_scraper.py`**

Locate the existing `if __name__ == "__main__":` dispatch block in `scripts/bitgn_scraper.py`. Add a `scrape` branch that delegates to `bitgn_scraper.scrape_cli.run_scrape_cli`. Concretely, find the existing `phase0`/`seed` if-elif chain and add:

```python
        elif sys.argv[1] == "scrape":
            from bitgn_scraper.scrape_cli import run_scrape_cli
            sys.exit(run_scrape_cli())
```

Update the help text in `_build_parser` to mention `scrape`.

- [ ] **Step 5: Run test to verify it passes**

```
uv run pytest tests/scraper/test_scrape_cli.py -v
uv run python scripts/bitgn_scraper.py --help  # sanity: scrape listed
```

Expected: PASS — 2 tests green; help mentions `scrape`.

- [ ] **Step 6: Commit**

```bash
git add scripts/bitgn_scraper.py src/bitgn_scraper/scrape_cli.py tests/scraper/test_scrape_cli.py
git commit -m "feat(scraper): Phase 1 scrape CLI subcommand"
git push
```

---

### Task 4: Probe-output rule extractors (`src/bitgn_scraper/probe_extract.py`)

Extends `seed_rules.extract_rules` with the additional patterns Phase 2 will hit (e.g., `expected outcome OUTCOME_X, got OUTCOME_Y`, the `answer must include the X of` constraint pattern). Pure regex.

**Files:**
- Create: `src/bitgn_scraper/probe_extract.py`
- Create: `tests/scraper/test_probe_extract.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_probe_extract.py
"""Probe-output rule extractor tests."""
from __future__ import annotations

from bitgn_scraper.probe_extract import extract_probe_rules


def test_extract_expected_outcome_mismatch() -> None:
    rules = extract_probe_rules(
        "expected outcome OUTCOME_NONE_CLARIFICATION, got OUTCOME_OK"
    )
    kinds = {(r.rule_kind, r.rule_value) for r in rules}
    assert ("expected_outcome", "OUTCOME_NONE_CLARIFICATION") in kinds


def test_extract_answer_must_include() -> None:
    rules = extract_probe_rules(
        "answer must include the date of the project kickoff"
    )
    kinds = {(r.rule_kind, r.rule_value) for r in rules}
    assert ("answer_constraint", "date") in kinds


def test_extract_combines_seed_patterns_and_probe_patterns() -> None:
    # A score_detail line with both an expected-answer pattern (from seed_rules)
    # and a missing-write pattern (also from seed_rules) should produce two rules.
    detail = "answer is incorrect. Expected: '1989-02-16'; missing file write '/work/notes.md'"
    rules = extract_probe_rules(detail)
    kinds = {(r.rule_kind, r.rule_value) for r in rules}
    assert ("expected_answer", "1989-02-16") in kinds
    assert ("required_write", "/work/notes.md") in kinds


def test_extract_unmatched_returns_empty() -> None:
    assert extract_probe_rules("some unrelated string") == []
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_probe_extract.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement `probe_extract.py`**

```python
# src/bitgn_scraper/probe_extract.py
"""Extend seed-rules extractors with probe-specific patterns.

Phase 2 score_detail strings include patterns the seed_rules module
doesn't carry (e.g. expected-outcome mismatches). This module composes
seed_rules.extract_rules with two additional regex passes.
"""
from __future__ import annotations

import re

from bitgn_scraper.seed_rules import ExtractedRule, extract_rules

_EXPECTED_OUTCOME = re.compile(r"expected outcome (OUTCOME_\w+),\s+got\s+OUTCOME_\w+")
_ANSWER_CONSTRAINT = re.compile(r"answer must include the (\w+) of")


def extract_probe_rules(score_detail: str) -> list[ExtractedRule]:
    """Combine seed-rule patterns + probe-specific patterns."""
    rules = list(extract_rules(score_detail))

    for m in _EXPECTED_OUTCOME.finditer(score_detail):
        rules.append(ExtractedRule(
            rule_kind="expected_outcome",
            rule_value=m.group(1),
            confidence="high",
        ))
    for m in _ANSWER_CONSTRAINT.finditer(score_detail):
        rules.append(ExtractedRule(
            rule_kind="answer_constraint",
            rule_value=m.group(1),
            confidence="high",
        ))
    return rules
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_probe_extract.py -v
```

Expected: PASS — 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_scraper/probe_extract.py tests/scraper/test_probe_extract.py
git commit -m "feat(scraper): probe-output rule extractors"
git push
```

---

### Task 5: Probe definitions + per-instantiation runner (`src/bitgn_scraper/probes.py`)

The 7 probes (P1, P2, P2b, P3, P4, P5, P6). Strategy B: each probe opens a fresh trial, hashes the instruction, applies, EndTrials. Adaptive stopping: P1 returns score=1.0 → record + stop. P5 reached without 1.0 → semantic-similarity task; flag low-confidence.

**Files:**
- Create: `src/bitgn_scraper/probes.py`
- Create: `tests/scraper/test_probes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_probes.py
"""Probe runner tests with a fake harness/PCM."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from bitgn_scraper.db import init_schema
from bitgn_scraper.probes import probe_instantiation


@dataclass
class _Started:
    instruction: str
    trial_id: str
    harness_url: str


@dataclass
class _End:
    score: float
    score_detail: list[str] = field(default_factory=list)


class _ScriptedHarness:
    """Returns canned (instruction, score, score_detail) per StartPlayground call."""

    def __init__(self, script: list[tuple[str, float, list[str]]]) -> None:
        self._script = script
        self._idx = 0
        self.start_calls = 0
        self.end_calls = 0
        self._pending_end: list[_End] = []

    def start_playground(self, req):  # noqa: ARG002
        instr, score, detail = self._script[self._idx]
        self._idx += 1
        self.start_calls += 1
        self._pending_end.append(_End(score=score, score_detail=detail))
        return _Started(instruction=instr, trial_id=f"trial_{self.start_calls}",
                        harness_url=f"https://vm-{self.start_calls}.example")

    def end_trial(self, req):  # noqa: ARG002
        self.end_calls += 1
        # Pop oldest pending; the runner ends in same order it starts
        return self._pending_end.pop(0)


class _NoopPcm:
    """PCM that accepts writes and answers without doing anything."""
    def write(self, req):  # noqa: ARG002
        return None

    def answer(self, req):  # noqa: ARG002
        return None


def _factory(url: str):  # noqa: ARG001
    return _NoopPcm()


def test_probe_stops_at_p1_when_score_one(tmp_path: Path) -> None:
    """P1 returns score=1.0 → no further probes for this instantiation."""
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    harness = _ScriptedHarness([
        ("instruction X", 1.0, []),  # P1 wins
    ])

    n_probes = probe_instantiation(
        harness_client=harness,
        pcm_factory=_factory,
        task_id="t001",
        benchmark_id="bitgn/pac1-prod",
        instruction_hash="abc",  # caller-supplied; tests skip lookup
        known_rules={},          # nothing known yet
        db_path=db_path,
        run_diagnostic_p2b=False,
        run_diagnostic_p6=False,
    )
    assert n_probes == 1
    assert harness.start_calls == 1
    with sqlite3.connect(db_path) as cx:
        rows = cx.execute("SELECT probe_kind, score FROM probe_log ORDER BY probe_id").fetchall()
    assert rows == [("P1_empty", 1.0)]


def test_probe_extracts_expected_answer_at_p1(tmp_path: Path) -> None:
    """P1 returns the canonical expected-answer detail; P2 then uses it and wins."""
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    harness = _ScriptedHarness([
        ("instruction Y", 0.0, ["answer is incorrect. Expected: '1989-02-16'"]),  # P1
        ("instruction Y", 1.0, []),                                                # P2 with extracted answer
    ])

    n_probes = probe_instantiation(
        harness_client=harness,
        pcm_factory=_factory,
        task_id="t001",
        benchmark_id="bitgn/pac1-prod",
        instruction_hash="abc",
        known_rules={},
        db_path=db_path,
        run_diagnostic_p2b=False,
        run_diagnostic_p6=False,
    )
    assert n_probes == 2
    with sqlite3.connect(db_path) as cx:
        kinds = [r[0] for r in cx.execute("SELECT probe_kind FROM probe_log ORDER BY probe_id")]
        rules = cx.execute("SELECT rule_kind, rule_value FROM scoring_rules").fetchall()
    assert kinds == ["P1_empty", "P2_extracted"]
    assert ("expected_answer", "1989-02-16") in {(r[0], r[1]) for r in rules}
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_probes.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement `probes.py`**

```python
# src/bitgn_scraper/probes.py
"""Phase 2 probes — Strategy B (one trial per probe, post-hoc match by instruction_hash).

Probe order:
    P1_empty           — answer="", refs=[], writes={}, OUTCOME_OK
    P2_extracted       — answer=extracted_so_far, refs=[], writes={}, OUTCOME_OK
    P3_with_refs       — answer=extracted, refs=extracted, writes={}, OUTCOME_OK
    P4_with_writes     — answer=extracted, refs=extracted, writes=extracted, OUTCOME_OK
    P5_outcome_alt     — answer=extracted, refs=extracted, writes=extracted, OUTCOME_NONE_CLARIFICATION

Adaptive stopping: any probe returning score=1.0 ends the chain — the
remaining rules are already known.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bitgn_scraper.db import insert_scoring_rule
from bitgn_scraper.probe_extract import extract_probe_rules


_PROBE_ORDER = ["P1_empty", "P2_extracted", "P3_with_refs", "P4_with_writes", "P5_outcome_alt"]


def probe_instantiation(
    *,
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_id: str,
    benchmark_id: str,
    instruction_hash: str,
    known_rules: dict[str, list[str]],  # rule_kind → list of values, populated as probes fire
    db_path: Path,
    run_diagnostic_p2b: bool = False,
    run_diagnostic_p6: bool = False,
) -> int:
    """Run probes for one instantiation; return count of probes fired.

    Updates known_rules in-place so callers can pass pre-known rules in
    (e.g. from Phase 1.5 seed) and read the post-probe state out.
    """
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome, WriteRequest
    from connectrpc.errors import ConnectError

    n_fired = 0
    for probe_kind in _PROBE_ORDER:
        ans, refs, writes_map, outcome = _build_probe(probe_kind, known_rules)

        started = harness_client.start_playground(
            StartPlaygroundRequest(benchmark_id=benchmark_id, task_id=task_id)
        )
        try:
            pcm = pcm_factory(started.harness_url)
            for path, content in writes_map.items():
                pcm.write(WriteRequest(path=path, content=content))
            try:
                pcm.answer(AnswerRequest(message=ans, outcome=outcome))
            except ConnectError:
                # Should not occur on a fresh trial, but guard against it.
                pass
        finally:
            ended = harness_client.end_trial(EndTrialRequest(trial_id=started.trial_id))

        n_fired += 1
        score = float(ended.score)
        detail = list(ended.score_detail)
        probe_id = _persist_probe(
            db_path=db_path,
            task_id=task_id,
            instantiation_hash=instruction_hash,
            probe_kind=probe_kind,
            ans=ans, refs=refs, writes=writes_map, outcome=outcome,
            score=score, score_detail=detail, trial_id=started.trial_id,
        )
        # Extract any rule strings the grader leaked.
        for rule in extract_probe_rules(" ".join(detail)):
            insert_scoring_rule(
                db_path=db_path,
                task_id=task_id,
                instantiation_hash=instruction_hash,
                rule_kind=rule.rule_kind,
                rule_value=rule.rule_value,
                confidence=rule.confidence,
                derived_from=probe_id,
                notes=f"probe={probe_kind}",
            )
            known_rules.setdefault(rule.rule_kind, []).append(rule.rule_value)

        if score >= 1.0:
            break

    if run_diagnostic_p2b:
        n_fired += _run_p2b(harness_client, pcm_factory, task_id, benchmark_id,
                            instruction_hash, known_rules, db_path)
    if run_diagnostic_p6:
        n_fired += _run_p6(harness_client, pcm_factory, task_id, benchmark_id,
                           instruction_hash, db_path)
    return n_fired


def _build_probe(kind: str, known: dict[str, list[str]]) -> tuple[str, list[str], dict[str, str], int]:
    from bitgn.vm.pcm_pb2 import Outcome
    ans = (known.get("expected_answer") or [""])[0]
    refs = list(known.get("required_ref") or [])
    writes_paths = list(known.get("required_write") or [])
    writes = {p: f"local-probe content for {p}" for p in writes_paths}
    if kind == "P1_empty":
        return ("", [], {}, Outcome.OUTCOME_OK)
    if kind == "P2_extracted":
        return (ans, [], {}, Outcome.OUTCOME_OK)
    if kind == "P3_with_refs":
        return (ans, refs, {}, Outcome.OUTCOME_OK)
    if kind == "P4_with_writes":
        return (ans, refs, writes, Outcome.OUTCOME_OK)
    if kind == "P5_outcome_alt":
        return (ans, refs, writes, Outcome.OUTCOME_NONE_CLARIFICATION)
    raise ValueError(f"unknown probe kind: {kind}")


def _persist_probe(
    *,
    db_path: Path,
    task_id: str,
    instantiation_hash: str,
    probe_kind: str,
    ans: str,
    refs: list[str],
    writes: dict[str, str],
    outcome: int,
    score: float,
    score_detail: list[str],
    trial_id: str,
) -> int:
    """Insert into probe_log; return the new probe_id."""
    probed_at = datetime.now(tz=timezone.utc).isoformat()
    with sqlite3.connect(db_path) as cx:
        cx.execute("PRAGMA foreign_keys = ON")
        cur = cx.execute(
            """
            INSERT INTO probe_log
            (task_id, instantiation_hash, probe_kind, submitted_answer,
             submitted_refs, submitted_outcome, submitted_writes,
             score, score_detail_raw, trial_id, probed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, instantiation_hash, probe_kind, ans,
                json.dumps(refs), str(outcome), json.dumps(writes),
                score, json.dumps(score_detail), trial_id, probed_at,
            ),
        )
        cx.commit()
        return cur.lastrowid or 0


def _run_p2b(
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_id: str,
    benchmark_id: str,
    instantiation_hash: str,
    known: dict[str, list[str]],
    db_path: Path,
) -> int:
    """Single mutation probe — case-flip on the extracted answer.

    Diagnostic: tells us whether the grader is exact-match or has tolerance.
    """
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome
    from connectrpc.errors import ConnectError

    base = (known.get("expected_answer") or [""])[0]
    if not base:
        return 0
    mutated = base.swapcase() if base != base.swapcase() else base + " "

    started = harness_client.start_playground(
        StartPlaygroundRequest(benchmark_id=benchmark_id, task_id=task_id)
    )
    try:
        pcm = pcm_factory(started.harness_url)
        try:
            pcm.answer(AnswerRequest(message=mutated, outcome=Outcome.OUTCOME_OK))
        except ConnectError:
            pass
    finally:
        ended = harness_client.end_trial(EndTrialRequest(trial_id=started.trial_id))

    _persist_probe(
        db_path=db_path,
        task_id=task_id,
        instantiation_hash=instantiation_hash,
        probe_kind="P2b_mutation",
        ans=mutated, refs=[], writes={}, outcome=int(Outcome.OUTCOME_OK),
        score=float(ended.score), score_detail=list(ended.score_detail),
        trial_id=started.trial_id,
    )
    return 1


def _run_p6(
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_id: str,
    benchmark_id: str,
    instantiation_hash: str,
    db_path: Path,
) -> int:
    """Random-but-typed answer probe. Diagnostic on the small sample."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome
    from connectrpc.errors import ConnectError

    started = harness_client.start_playground(
        StartPlaygroundRequest(benchmark_id=benchmark_id, task_id=task_id)
    )
    try:
        pcm = pcm_factory(started.harness_url)
        try:
            pcm.answer(AnswerRequest(message="P6_RANDOM", outcome=Outcome.OUTCOME_OK))
        except ConnectError:
            pass
    finally:
        ended = harness_client.end_trial(EndTrialRequest(trial_id=started.trial_id))

    _persist_probe(
        db_path=db_path,
        task_id=task_id,
        instantiation_hash=instantiation_hash,
        probe_kind="P6_random",
        ans="P6_RANDOM", refs=[], writes={}, outcome=int(Outcome.OUTCOME_OK),
        score=float(ended.score), score_detail=list(ended.score_detail),
        trial_id=started.trial_id,
    )
    return 1
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_probes.py -v
```

Expected: PASS — 2 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_scraper/probes.py tests/scraper/test_probes.py
git commit -m "feat(scraper): Phase 2 probes + per-instantiation runner"
git push
```

---

### Task 6: Wire `probe` CLI subcommand

Iterates over every `task_instantiations` row, runs `probe_instantiation`. Sampling: `--p2b-sample`, `--p6-sample` integers (default 0 → off; spec says "20 sampled tasks per category" — caller picks).

**Files:**
- Modify: `scripts/bitgn_scraper.py`
- Create: `src/bitgn_scraper/probe_cli.py`
- Create: `tests/scraper/test_probe_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_probe_cli.py
"""Probe CLI parser smoke test."""
from __future__ import annotations

from pathlib import Path

from bitgn_scraper.probe_cli import build_parser


def test_probe_cli_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.benchmark_id == "bitgn/pac1-prod"
    assert args.p2b_sample == 0
    assert args.p6_sample == 0
    assert args.db_path == Path("artifacts/harness_db/bitgn_local.db")


def test_probe_cli_parser_diagnostics() -> None:
    parser = build_parser()
    args = parser.parse_args(["--p2b-sample", "10", "--p6-sample", "5"])
    assert args.p2b_sample == 10
    assert args.p6_sample == 5
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_probe_cli.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement `src/bitgn_scraper/probe_cli.py`**

```python
# src/bitgn_scraper/probe_cli.py
"""Phase 2 CLI shim. Iterates task_instantiations and probes each."""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bitgn_scraper probe")
    p.add_argument("--benchmark-id", default="bitgn/pac1-prod")
    p.add_argument("--task-ids", default="",
                   help="comma-separated subset; empty = all instantiations in DB")
    p.add_argument("--db-path", type=Path,
                   default=Path("artifacts/harness_db/bitgn_local.db"))
    p.add_argument("--p2b-sample", type=int, default=0,
                   help="number of instantiations to additionally hit with P2b mutation probe")
    p.add_argument("--p6-sample", type=int, default=0,
                   help="number of instantiations to additionally hit with P6 random probe")
    p.add_argument("--seed", type=int, default=0,
                   help="seed for diagnostic-sample selection")
    return p


def run_probe_cli() -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[2:])

    from bitgn_scraper.clients import build_harness_client, build_pcm_client
    from bitgn_scraper.probes import probe_instantiation

    harness = build_harness_client()

    rows = _load_instantiations(args.db_path, args.task_ids)
    if not rows:
        print("[probe] no instantiations in DB — run `scrape` first", flush=True)
        return 1

    rng = random.Random(args.seed)
    p2b_set = set(rng.sample(range(len(rows)), min(args.p2b_sample, len(rows))))
    p6_set = set(rng.sample(range(len(rows)), min(args.p6_sample, len(rows))))

    print(f"[probe] {len(rows)} instantiation(s) → {args.db_path}", flush=True)
    total = 0
    for i, (task_id, inst_hash) in enumerate(rows):
        print(f"[probe] ({i + 1}/{len(rows)}) {task_id} {inst_hash[:12]}", flush=True)
        n = probe_instantiation(
            harness_client=harness,
            pcm_factory=build_pcm_client,
            task_id=task_id,
            benchmark_id=args.benchmark_id,
            instruction_hash=inst_hash,
            known_rules={},
            db_path=args.db_path,
            run_diagnostic_p2b=(i in p2b_set),
            run_diagnostic_p6=(i in p6_set),
        )
        total += n
        print(f"  → {n} probe(s)", flush=True)

    print(f"[probe] done — {total} probe(s) total", flush=True)
    return 0


def _load_instantiations(db_path: Path, task_filter: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as cx:
        if task_filter:
            tids = [t.strip() for t in task_filter.split(",") if t.strip()]
            placeholders = ",".join("?" * len(tids))
            sql = (
                "SELECT task_id, instantiation_hash FROM task_instantiations "
                f"WHERE task_id IN ({placeholders}) ORDER BY task_id, instantiation_hash"
            )
            return list(cx.execute(sql, tids).fetchall())
        return list(cx.execute(
            "SELECT task_id, instantiation_hash FROM task_instantiations "
            "ORDER BY task_id, instantiation_hash"
        ).fetchall())
```

- [ ] **Step 4: Wire subcommand into `scripts/bitgn_scraper.py`**

Add the `probe` branch alongside `phase0`/`seed`/`scrape`:

```python
        elif sys.argv[1] == "probe":
            from bitgn_scraper.probe_cli import run_probe_cli
            sys.exit(run_probe_cli())
```

Update help text in `_build_parser`.

- [ ] **Step 5: Run test to verify it passes**

```
uv run pytest tests/scraper/test_probe_cli.py -v
uv run python scripts/bitgn_scraper.py --help  # sanity: probe listed
```

Expected: PASS — 2 tests green; help shows `probe`.

- [ ] **Step 6: Commit**

```bash
git add scripts/bitgn_scraper.py src/bitgn_scraper/probe_cli.py tests/scraper/test_probe_cli.py
git commit -m "feat(scraper): Phase 2 probe CLI subcommand"
git push
```

---

### Task 7: Phase 3 validators (`src/bitgn_scraper/validate.py`)

Three validators exposed as pure functions + a CLI wrapper.

1. `check_workspace_integrity(db_path, workspace_root) -> IntegrityReport` — re-hash on-disk files, compare vs `workspace_files.sha256`.
2. `check_probe_coverage(db_path) -> CoverageReport` — count tasks with high/low/no rules.
3. `check_determinism(db_path, harness_client, pcm_factory, task_ids) -> DeterminismReport` — re-scrape N tasks; compare to stored hashes.

**Files:**
- Create: `src/bitgn_scraper/validate.py`
- Create: `tests/scraper/test_validate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_validate.py
"""Phase 3 validator tests."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from bitgn_scraper.db import init_schema
from bitgn_scraper.validate import (
    check_workspace_integrity,
    check_probe_coverage,
)


def _seed_one(db_path: Path, task_id: str, inst_hash: str, rule_count: int) -> None:
    with sqlite3.connect(db_path) as cx:
        cx.execute("PRAGMA foreign_keys = ON")
        cx.execute("""
            INSERT INTO task_instantiations
            (task_id, instantiation_hash, instruction, instruction_hash,
             tree_fingerprint, context_time, context_unix, benchmark_id,
             scraped_at, workspace_dir, workspace_byte_total, workspace_file_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id, inst_hash, "instr", "h", "tfp", "t", 0,
              "bitgn/pac1-prod", "s", f"{task_id}/{inst_hash[:12]}", 0, 0))
        for i in range(rule_count):
            cx.execute("""
                INSERT INTO scoring_rules
                (task_id, instantiation_hash, rule_kind, rule_value,
                 confidence, derived_from, notes)
                VALUES (?, ?, ?, ?, ?, NULL, NULL)
            """, (task_id, inst_hash, "expected_answer", f"v{i}", "high"))
        cx.commit()


def test_integrity_clean_when_files_match(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    inst_hash = "a" * 64
    _seed_one(db_path, "t001", inst_hash, rule_count=0)

    # Create a matching workspace file
    ws = tmp_path / "workspaces" / "t001" / inst_hash[:12]
    ws.mkdir(parents=True)
    (ws / "file.md").write_text("alpha", encoding="utf-8")
    sha = hashlib.sha256(b"alpha").hexdigest()
    with sqlite3.connect(db_path) as cx:
        cx.execute("""
            INSERT INTO workspace_files
            (task_id, instantiation_hash, path, is_dir, byte_size, sha256)
            VALUES (?, ?, ?, 0, 5, ?)
        """, ("t001", inst_hash, "/file.md", sha))
        cx.commit()

    report = check_workspace_integrity(db_path, tmp_path / "workspaces")
    assert report.checked == 1
    assert report.mismatches == []


def test_integrity_flags_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    inst_hash = "b" * 64
    _seed_one(db_path, "t002", inst_hash, rule_count=0)
    ws = tmp_path / "workspaces" / "t002" / inst_hash[:12]
    ws.mkdir(parents=True)
    (ws / "file.md").write_text("alpha", encoding="utf-8")
    with sqlite3.connect(db_path) as cx:
        cx.execute("""
            INSERT INTO workspace_files
            (task_id, instantiation_hash, path, is_dir, byte_size, sha256)
            VALUES (?, ?, ?, 0, 5, ?)
        """, ("t002", inst_hash, "/file.md", "deadbeef" * 8))
        cx.commit()

    report = check_workspace_integrity(db_path, tmp_path / "workspaces")
    assert report.checked == 1
    assert len(report.mismatches) == 1
    assert report.mismatches[0].path == "/file.md"


def test_coverage_buckets_tasks_by_rule_confidence(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    _seed_one(db_path, "t001", "a" * 64, rule_count=2)
    _seed_one(db_path, "t002", "b" * 64, rule_count=0)
    report = check_probe_coverage(db_path)
    assert report.tasks_with_high_confidence == 1  # t001
    assert report.tasks_with_no_rules == 1          # t002
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_validate.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement `validate.py`**

```python
# src/bitgn_scraper/validate.py
"""Phase 3 validators — integrity, coverage, determinism."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class IntegrityMismatch:
    task_id: str
    instantiation_hash: str
    path: str
    expected_sha256: str
    actual_sha256: str


@dataclass(frozen=True)
class IntegrityReport:
    checked: int
    mismatches: list[IntegrityMismatch] = field(default_factory=list)
    missing_files: list[tuple[str, str, str]] = field(default_factory=list)


def check_workspace_integrity(
    db_path: Path, workspace_root: Path
) -> IntegrityReport:
    """Re-hash on-disk files, compare to workspace_files.sha256."""
    mismatches: list[IntegrityMismatch] = []
    missing: list[tuple[str, str, str]] = []
    checked = 0
    with sqlite3.connect(db_path) as cx:
        rows = cx.execute("""
            SELECT wf.task_id, wf.instantiation_hash, wf.path, wf.sha256, ti.workspace_dir
            FROM workspace_files wf
            JOIN task_instantiations ti
              ON ti.task_id = wf.task_id AND ti.instantiation_hash = wf.instantiation_hash
            WHERE wf.is_dir = 0
        """).fetchall()
    for task_id, inst, path, expected, ws_rel in rows:
        if expected == "READ_ERROR":
            continue
        rel = path.lstrip("/")
        if not rel:
            continue
        on_disk = workspace_root / ws_rel / rel
        if not on_disk.exists():
            missing.append((task_id, inst, path))
            continue
        actual = hashlib.sha256(on_disk.read_bytes()).hexdigest()
        checked += 1
        if actual != expected:
            mismatches.append(IntegrityMismatch(
                task_id=task_id, instantiation_hash=inst, path=path,
                expected_sha256=expected, actual_sha256=actual,
            ))
    return IntegrityReport(
        checked=checked, mismatches=mismatches, missing_files=missing,
    )


@dataclass(frozen=True)
class CoverageReport:
    total_tasks: int
    tasks_with_high_confidence: int
    tasks_with_low_confidence_only: int
    tasks_with_no_rules: int


def check_probe_coverage(db_path: Path) -> CoverageReport:
    """Bucket each task by the highest-confidence rule it has."""
    with sqlite3.connect(db_path) as cx:
        all_tasks = {r[0] for r in cx.execute(
            "SELECT DISTINCT task_id FROM task_instantiations"
        )}
        high = {r[0] for r in cx.execute(
            "SELECT DISTINCT task_id FROM scoring_rules WHERE confidence = 'high'"
        )}
        any_rule = {r[0] for r in cx.execute(
            "SELECT DISTINCT task_id FROM scoring_rules"
        )}
    no_rules = all_tasks - any_rule
    low_only = any_rule - high
    return CoverageReport(
        total_tasks=len(all_tasks),
        tasks_with_high_confidence=len(high),
        tasks_with_low_confidence_only=len(low_only),
        tasks_with_no_rules=len(no_rules),
    )


@dataclass(frozen=True)
class DeterminismFinding:
    task_id: str
    stored_hashes: list[str]
    new_hashes: list[str]
    overlap: int
    fully_saturated: bool


@dataclass(frozen=True)
class DeterminismReport:
    findings: list[DeterminismFinding] = field(default_factory=list)


def check_determinism(
    *,
    db_path: Path,
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_ids: list[str],
    benchmark_id: str = "bitgn/pac1-prod",
    n_samples_per_task: int = 5,
) -> DeterminismReport:
    """Re-scrape `n_samples_per_task` instantiations per task; compare hashes."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ContextRequest
    from bitgn_scraper.fingerprint import instantiation_hash
    from bitgn_scraper.workspace_walk import walk_workspace

    findings: list[DeterminismFinding] = []
    with sqlite3.connect(db_path) as cx:
        stored_by_task = {
            tid: [r[0] for r in cx.execute(
                "SELECT instantiation_hash FROM task_instantiations WHERE task_id = ?",
                (tid,),
            ).fetchall()]
            for tid in task_ids
        }

    for tid in task_ids:
        stored = stored_by_task.get(tid, [])
        new_hashes: list[str] = []
        for _ in range(n_samples_per_task):
            started = harness_client.start_playground(
                StartPlaygroundRequest(benchmark_id=benchmark_id, task_id=tid)
            )
            try:
                pcm = pcm_factory(started.harness_url)
                _ = pcm.context(ContextRequest())
                files = walk_workspace(pcm)
                new_hashes.append(instantiation_hash(started.instruction, files))
            finally:
                harness_client.end_trial(EndTrialRequest(trial_id=started.trial_id))
        overlap = len(set(new_hashes) & set(stored))
        findings.append(DeterminismFinding(
            task_id=tid,
            stored_hashes=sorted(stored),
            new_hashes=sorted(new_hashes),
            overlap=overlap,
            fully_saturated=overlap == len(set(new_hashes)),
        ))
    return DeterminismReport(findings=findings)
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_validate.py -v
```

Expected: PASS — 3 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_scraper/validate.py tests/scraper/test_validate.py
git commit -m "feat(scraper): Phase 3 validators"
git push
```

---

### Task 8: Wire `validate` CLI subcommand

**Files:**
- Modify: `scripts/bitgn_scraper.py`
- Create: `src/bitgn_scraper/validate_cli.py`
- Create: `tests/scraper/test_validate_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_validate_cli.py
"""Validate CLI parser smoke test."""
from __future__ import annotations

from pathlib import Path

from bitgn_scraper.validate_cli import build_parser


def test_validate_cli_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.db_path == Path("artifacts/harness_db/bitgn_local.db")
    assert args.workspace_root == Path("artifacts/harness_db/workspaces")
    assert args.skip_determinism is False
    assert args.determinism_samples == 5
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_validate_cli.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement `src/bitgn_scraper/validate_cli.py`**

```python
# src/bitgn_scraper/validate_cli.py
"""Phase 3 CLI shim. Runs integrity + coverage + (optional) determinism."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bitgn_scraper validate")
    p.add_argument("--db-path", type=Path,
                   default=Path("artifacts/harness_db/bitgn_local.db"))
    p.add_argument("--workspace-root", type=Path,
                   default=Path("artifacts/harness_db/workspaces"))
    p.add_argument("--skip-determinism", action="store_true",
                   help="skip the live re-scrape determinism check (no PROD calls)")
    p.add_argument("--determinism-task-ids", default="t001,t010,t020,t030,t050",
                   help="comma-separated tasks to re-scrape for determinism check")
    p.add_argument("--determinism-samples", type=int, default=5)
    p.add_argument("--out-root", type=Path,
                   default=Path("artifacts/harness_db/scrape_runs"))
    return p


def run_validate_cli() -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[2:])

    from bitgn_scraper.validate import (
        check_probe_coverage,
        check_workspace_integrity,
    )

    print(f"[validate] integrity check on {args.db_path}", flush=True)
    integrity = check_workspace_integrity(args.db_path, args.workspace_root)
    print(f"  → checked={integrity.checked} mismatches={len(integrity.mismatches)} "
          f"missing={len(integrity.missing_files)}", flush=True)

    print(f"[validate] coverage stats", flush=True)
    coverage = check_probe_coverage(args.db_path)
    print(f"  → total={coverage.total_tasks} high={coverage.tasks_with_high_confidence} "
          f"low_only={coverage.tasks_with_low_confidence_only} "
          f"no_rules={coverage.tasks_with_no_rules}", flush=True)

    out: dict = {
        "ran_at": datetime.now(tz=timezone.utc).isoformat(),
        "integrity": _integrity_to_dict(integrity),
        "coverage": asdict(coverage),
    }

    if not args.skip_determinism:
        print(f"[validate] determinism re-scrape on {args.determinism_task_ids}", flush=True)
        from bitgn_scraper.clients import build_harness_client, build_pcm_client
        from bitgn_scraper.validate import check_determinism

        harness = build_harness_client()
        tids = [t.strip() for t in args.determinism_task_ids.split(",") if t.strip()]
        det = check_determinism(
            db_path=args.db_path,
            harness_client=harness,
            pcm_factory=build_pcm_client,
            task_ids=tids,
            n_samples_per_task=args.determinism_samples,
        )
        for f in det.findings:
            print(f"  → {f.task_id}: stored={len(f.stored_hashes)} "
                  f"new_distinct={len(set(f.new_hashes))} overlap={f.overlap} "
                  f"saturated={f.fully_saturated}", flush=True)
        out["determinism"] = {
            "findings": [asdict(f) for f in det.findings],
        }

    out_dir = args.out_root / datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "validate_report.json"
    out_path.write_text(json.dumps(out, indent=2, sort_keys=True, default=str))
    print(f"[validate] wrote {out_path}", flush=True)
    return 0


def _integrity_to_dict(report) -> dict:
    return {
        "checked": report.checked,
        "mismatches": [
            {"task_id": m.task_id, "instantiation_hash": m.instantiation_hash,
             "path": m.path, "expected_sha256": m.expected_sha256,
             "actual_sha256": m.actual_sha256}
            for m in report.mismatches
        ],
        "missing_files": [
            {"task_id": t, "instantiation_hash": i, "path": p}
            for t, i, p in report.missing_files
        ],
    }
```

- [ ] **Step 4: Wire subcommand into `scripts/bitgn_scraper.py`**

```python
        elif sys.argv[1] == "validate":
            from bitgn_scraper.validate_cli import run_validate_cli
            sys.exit(run_validate_cli())
```

Update help text.

- [ ] **Step 5: Run test to verify it passes**

```
uv run pytest tests/scraper/test_validate_cli.py -v
uv run python scripts/bitgn_scraper.py --help  # sanity: validate listed
```

Expected: PASS — 1 test green; help shows `validate`.

- [ ] **Step 6: Commit**

```bash
git add scripts/bitgn_scraper.py src/bitgn_scraper/validate_cli.py tests/scraper/test_validate_cli.py
git commit -m "feat(scraper): Phase 3 validate CLI subcommand"
git push
```

---

### Task 9: Run scrape + probe + validate against a tiny PROD subset

This is **not a code task** — it's the integration sanity-check that proves all 8 prior tasks compose correctly against PROD before we run the full 104-task scrape (Task 10).

**Files:** none (consumes existing code)

- [ ] **Step 1: Source env**

```
set -a && source .worktrees/plan-b/.env && set +a
```

- [ ] **Step 2: Tiny scrape**

```
uv run python scripts/bitgn_scraper.py scrape \
  --task-ids t001,t010 \
  --max-attempts 5 \
  --saturation-threshold 2
```

Expected (~30 s): 1–3 instantiation rows per task in `task_instantiations`; `workspace_files` has matching rows; flat-file tree under `artifacts/harness_db/workspaces/t001/<hash>/` populated with markdown files.

- [ ] **Step 3: Tiny probe**

```
uv run python scripts/bitgn_scraper.py probe --task-ids t001,t010
```

Expected (~1 min): `probe_log` rows for each `(task, instantiation, probe_kind)` with `score` and `score_detail_raw` populated; `scoring_rules` has at least one `expected_answer` row for `t001` (matches the score-detail prefix we already saw in Phase 0 sanity).

- [ ] **Step 4: Validate**

```
uv run python scripts/bitgn_scraper.py validate \
  --determinism-task-ids t001,t010 \
  --determinism-samples 3
```

Expected: integrity `mismatches=0`; coverage shows `tasks_with_high_confidence>=1`; determinism findings printed for both tasks.

- [ ] **Step 5: Spot-check the data manually**

```
uv run python -c "
import sqlite3
cx = sqlite3.connect('artifacts/harness_db/bitgn_local.db')
print('instantiations:', cx.execute('SELECT COUNT(*) FROM task_instantiations').fetchone())
print('workspace_files:', cx.execute('SELECT COUNT(*) FROM workspace_files').fetchone())
print('probes:', cx.execute('SELECT COUNT(*) FROM probe_log').fetchone())
print('rules:', cx.execute('SELECT COUNT(*) FROM scoring_rules').fetchone())
for row in cx.execute('SELECT task_id, rule_kind, rule_value, confidence FROM scoring_rules LIMIT 5'):
    print(' ', row)
"
```

Expected: non-zero counts everywhere; rules look sensible.

If any step fails, fix the underlying code (likely a real-PROD-shape mismatch the unit tests didn't catch) before proceeding to Task 10.

---

### Task 10: Full PROD scrape + probe + validate

The big run. Consumes API budget. Expected wall time: ~30–60 min for scrape + ~60–90 min for probe (Strategy B = 5 probes × ~30 instantiations × 104 tasks = ~15 000 trials at ~0.4 s each).

**Files:** none (consumes existing code; produces SQLite + flat files + JSON reports)

- [ ] **Step 1: Source env + sanity baseline**

```
set -a && source .worktrees/plan-b/.env && set +a
uv run python scripts/verify_prod_grader.py --task-id t001  # confirm auth
```

- [ ] **Step 2: Full scrape**

```
uv run python scripts/bitgn_scraper.py scrape | tee artifacts/harness_db/scrape_runs/full_scrape.log
```

Expected (~30–60 min): all 104 tasks scraped; `task_instantiations` row count typically 200–500 (tasks vary in rotation pool size).

- [ ] **Step 3: Full probe**

```
uv run python scripts/bitgn_scraper.py probe --p2b-sample 20 --p6-sample 10 \
  | tee artifacts/harness_db/scrape_runs/full_probe.log
```

Expected (~60–90 min): every instantiation row hit with P1–P5 (terminating early on score=1.0); 20 P2b probes + 10 P6 probes scattered.

- [ ] **Step 4: Full validate**

```
uv run python scripts/bitgn_scraper.py validate \
  | tee artifacts/harness_db/scrape_runs/full_validate.log
```

Expected: integrity `mismatches=0`, `missing_files=0`; coverage report shows the residual hard set (tasks with `no_rules` after probing — these are the LLM-judge fallback candidates flagged in the spec's Out-of-scope section).

- [ ] **Step 5: Commit findings**

```bash
LATEST=$(ls -1d artifacts/harness_db/scrape_runs/2026* | tail -1)
git add -f "$LATEST/validate_report.json"
git add -f artifacts/harness_db/scrape_runs/*.log
git commit -m "data(scraper): full PROD scrape + probe + validate"
git push
```

The SQLite file and the workspace tree stay gitignored (`artifacts/harness_db/*.db`, `artifacts/harness_db/workspaces/`). Only the JSON report + run logs go in version control.

- [ ] **Step 6: Summarise findings**

Write `artifacts/harness_db/scrape_runs/<TIMESTAMP>/PROBE_SUMMARY.md` covering:
- total instantiations across 104 tasks
- per-task rotation pool size distribution (median, max, p99)
- coverage breakdown (high-confidence / low-only / no-rules)
- list of tasks with `no_rules` (the residual hard set; input to the LLM-judge follow-up)
- any `expected_outcome` patterns hit (suggests P5 actually fired and the task uses outcome-mismatch scoring)

```bash
git add artifacts/harness_db/scrape_runs/<TIMESTAMP>/PROBE_SUMMARY.md
git commit -m "docs(scraper): summarise full-scrape findings"
git push
```

---

## Acceptance criteria for this plan

1. `uv run pytest tests/scraper/ -v` — all green (target: ≥50 tests, including the 37 from Plan 1).
2. `uv run python scripts/bitgn_scraper.py --help` — lists all 5 subcommands (`phase0`, `seed`, `scrape`, `probe`, `validate`).
3. After Task 9: `task_instantiations`, `workspace_files`, `probe_log`, `scoring_rules` all have rows for `t001` + `t010`.
4. After Task 10: full validate report shows `integrity.mismatches=0`. Coverage report explicitly enumerates the residual `no_rules` task set.
5. No regressions: `uv run pytest -q` finishes green (excluding any pre-Plan-1 failing tests; baseline before starting).

## What this plan does NOT deliver

- No local gRPC harness server — that's Plan 2.
- No agent LLM-content trace flag — that's Plan 3.
- No LLM-as-judge fallback for `no_rules` tasks — flagged as a follow-up in the spec.
- No removal of `scripts/local_pcm.py` / `scripts/local_bench.py` — they keep working off filesystem snapshots.
