# BitGN Scraper Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundation pieces of the BitGN local-harness scraper — package skeleton, SQLite store, auth-wrapped SDK clients, the Phase 0 lifecycle spike, and the Phase 1.5 free seed-rule miner — so that a follow-up plan can implement the Phase 1/2/3 scrape+probe layer with empirical Phase 0 data in hand.

**Architecture:** Standalone Python package `src/bitgn_scraper/` plus `scripts/bitgn_scraper.py` CLI front-end. SQLite store at `artifacts/harness_db/bitgn_local.db` (schema only — no data yet). Phase 0 spike runner emits `artifacts/harness_db/scrape_runs/<ts>/lifecycle_spike.json`. Phase 1.5 mines existing PROD JSONL traces + server logs, writes rows into the new `scoring_rules` table.

**Tech Stack:** Python 3.12, `bitgn-local-sdk` (already a dep), Connect-RPC sync clients, SQLite (stdlib `sqlite3`), pytest.

**Spec reference:** `docs/superpowers/specs/2026-04-26-bitgn-local-harness-clone-design.md` — Phases 0 and 1.5.

**Out of scope for this plan:** Phase 1 (workspace scrape), Phase 2 (probe matrix), Phase 3 (self-validate), Component 2 (gRPC server), Component 3 (LLM trace gate). Those get separate plans, the next of which (`2026-04-26-bitgn-scraper-and-probe.md`) will be written after this plan's Phase 0 spike has run and we know the empirical answers to the spec's open questions.

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/bitgn_scraper/__init__.py` | Package marker. Exports a `__version__`. |
| `src/bitgn_scraper/clients.py` | Builds authenticated Connect-RPC clients (Harness + PCM). Reads `BITGN_API_KEY`, `BITGN_BASE_URL`. Single-purpose factory. |
| `src/bitgn_scraper/db.py` | SQLite schema, connection helpers, `init_schema()`, simple typed accessors. No business logic. |
| `src/bitgn_scraper/fingerprint.py` | `tree_fingerprint(file_records)` — deterministic SHA-256 of a sorted manifest. Shared by Phase 0 (rotation detection) and Phase 1 (instantiation hash). |
| `src/bitgn_scraper/phase0.py` | The 7 lifecycle sub-spikes plus a runner that aggregates results. One Python file, multiple functions, single JSON output. |
| `src/bitgn_scraper/seed_outcomes.py` | Pure JSONL parser: walks `logs/prod_cf90740_full/<run>/*.jsonl`, extracts `kind=outcome` rows with `score=0.0` and a non-empty `score_detail`, returns rule-extraction inputs. |
| `src/bitgn_scraper/seed_server_logs.py` | Pure text parser: scans `vm-*.eu.bitgn.com.txt` and `t*-*.log` files for `[ ERR ] AI agent score 0.00 / ...` lines, extracts the score-detail tail. |
| `src/bitgn_scraper/seed_rules.py` | Regex extractors that turn `score_detail` strings into `(rule_kind, rule_value)` rows. Shared by `seed_outcomes`, `seed_server_logs`, and (later) Phase 2 probe parsing. |
| `scripts/bitgn_scraper.py` | CLI entry. Subcommands: `phase0`, `seed`. Each subcommand is a thin shim over the package. |
| `tests/scraper/__init__.py` | Test package marker. |
| `tests/scraper/test_fingerprint.py` | Unit tests for fingerprint determinism. |
| `tests/scraper/test_db.py` | Schema initialization and round-trip insert/select. |
| `tests/scraper/test_seed_rules.py` | Regex extraction unit tests using fixture strings copied from real PROD logs. |
| `tests/scraper/test_seed_outcomes.py` | JSONL miner unit tests. |
| `tests/scraper/test_seed_server_logs.py` | Server-log miner unit tests. |
| `tests/scraper/test_phase0_shape.py` | Output-shape tests for the lifecycle JSON (no live API calls). |
| `tests/scraper/fixtures/sample_outcome.jsonl` | One real `kind=outcome` failed-task line for the JSONL miner test. |
| `tests/scraper/fixtures/sample_server.log` | Trimmed real server-log snippet ending in the `[ ERR ]` line. |
| `artifacts/harness_db/.gitkeep` | Reserve the directory; the DB file itself is gitignored. |

**Library inventory (already installed via `bitgn-local-sdk`):**
- `bitgn.harness_connect.HarnessServiceClientSync`
- `bitgn.harness_pb2`: `StartPlaygroundRequest`, `EndTrialRequest`, `GetTrialRequest`, `TrialState`, `GetBenchmarkRequest`
- `bitgn.vm.pcm_connect.PcmRuntimeClientSync`
- `bitgn.vm.pcm_pb2`: `TreeRequest`, `ReadRequest`, `ContextRequest`, `WriteRequest`, `AnswerRequest`, `Outcome`
- `connectrpc.interceptor.MetadataInterceptorSync`

---

### Task 1: Package skeleton + CLI shell

**Files:**
- Create: `src/bitgn_scraper/__init__.py`
- Create: `scripts/bitgn_scraper.py`
- Create: `tests/scraper/__init__.py`
- Create: `tests/scraper/test_cli_shell.py`
- Create: `artifacts/harness_db/.gitkeep`
- Modify: `.gitignore` (append `artifacts/harness_db/*.db`, `artifacts/harness_db/scrape_runs/`)

- [ ] **Step 1: Write the failing test for CLI shell**

```python
# tests/scraper/test_cli_shell.py
"""Smoke tests for the scraper CLI shell.

Verifies argparse plumbing only — no real subcommand work. The actual
phase0 / seed subcommands are tested in their own modules.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run(*argv: str, expect_rc: int) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "scripts/bitgn_scraper.py", *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == expect_rc, (
        f"argv={argv!r} rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return proc


def test_help_lists_subcommands() -> None:
    proc = _run("--help", expect_rc=0)
    out = proc.stdout
    assert "phase0" in out
    assert "seed" in out


def test_no_subcommand_exits_nonzero() -> None:
    _run(expect_rc=2)


def test_unknown_subcommand_exits_nonzero() -> None:
    _run("nope", expect_rc=2)
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_cli_shell.py -v
```

Expected: FAIL — `scripts/bitgn_scraper.py` doesn't exist yet.

- [ ] **Step 3: Create the package marker**

```python
# src/bitgn_scraper/__init__.py
"""BitGN PROD harness scraper.

Standalone tooling that walks BitGN's PROD playground API to populate
a local SQLite store with task workspaces, instructions, and grader
rules. Designed as a drop-in data source for the local gRPC harness
clone (built in a separate plan).
"""

__version__ = "0.1.0"
```

- [ ] **Step 4: Create the CLI shell**

```python
# scripts/bitgn_scraper.py
"""BitGN scraper CLI.

Subcommands:
  phase0  — run the lifecycle spike against PROD; write
            artifacts/harness_db/scrape_runs/<ts>/lifecycle_spike.json
  seed    — mine existing JSONL traces + server logs for free
            grader-rule seeds; populate scoring_rules in the SQLite DB

Both subcommands are thin shims over functions in src/bitgn_scraper/.
"""
from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bitgn_scraper",
        description="BitGN PROD harness scraper.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("phase0", help="run the lifecycle spike (Phase 0)")
    sub.add_parser("seed", help="mine existing logs for free grader rules (Phase 1.5)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "phase0":
        from bitgn_scraper.phase0 import run_phase0_cli
        return run_phase0_cli()
    if args.cmd == "seed":
        from bitgn_scraper.seed_rules import run_seed_cli
        return run_seed_cli()
    parser.error(f"unknown command: {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Create tests/scraper/__init__.py**

```python
# tests/scraper/__init__.py
```

- [ ] **Step 6: Create the directory placeholder**

Run:
```bash
mkdir -p artifacts/harness_db
touch artifacts/harness_db/.gitkeep
```

- [ ] **Step 7: Update .gitignore**

Append the following lines to `.gitignore` (do not replace existing content):

```
# scraper artifacts
artifacts/harness_db/*.db
artifacts/harness_db/*.db-journal
artifacts/harness_db/*.db-wal
artifacts/harness_db/scrape_runs/
artifacts/harness_db/workspaces/
```

- [ ] **Step 8: Run test to verify it passes**

```
uv run pytest tests/scraper/test_cli_shell.py -v
```

Expected: PASS for `test_help_lists_subcommands`, `test_no_subcommand_exits_nonzero`, `test_unknown_subcommand_exits_nonzero`. (The `phase0`/`seed` subcommands will fail at import — that's fine, those modules don't exist yet and aren't exercised by these three tests.)

- [ ] **Step 9: Commit**

```bash
git add src/bitgn_scraper/__init__.py scripts/bitgn_scraper.py \
        tests/scraper/__init__.py tests/scraper/test_cli_shell.py \
        artifacts/harness_db/.gitkeep .gitignore
git commit -m "feat(scraper): package skeleton + CLI shell"
```

---

### Task 2: SQLite schema (`src/bitgn_scraper/db.py`)

**Files:**
- Create: `src/bitgn_scraper/db.py`
- Create: `tests/scraper/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_db.py
"""Schema + helper tests for src/bitgn_scraper/db.py."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bitgn_scraper.db import (
    EXPECTED_TABLES,
    connect,
    init_schema,
    insert_scoring_rule,
)


def test_init_schema_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r[0] for r in rows}
    for table in EXPECTED_TABLES:
        assert table in names, f"missing table {table!r}; got {names!r}"


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    init_schema(db_path)  # should not raise
    with connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM scoring_rules").fetchone()[0]
    assert n == 0


def test_insert_scoring_rule_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    rule_id = insert_scoring_rule(
        db_path,
        task_id="t000",
        instantiation_hash="abc123",
        rule_kind="expected_answer",
        rule_value="1989-02-16",
        confidence="high",
        derived_from=None,
        notes="seeded from cf90740 outcome trace",
    )
    assert rule_id > 0
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT task_id, rule_kind, rule_value, confidence, notes "
            "FROM scoring_rules WHERE rule_id=?", (rule_id,)
        ).fetchone()
    assert row == (
        "t000",
        "expected_answer",
        "1989-02-16",
        "high",
        "seeded from cf90740 outcome trace",
    )


def test_insert_scoring_rule_rejects_bad_confidence(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    with pytest.raises(ValueError, match="confidence must be"):
        insert_scoring_rule(
            db_path,
            task_id="t000",
            instantiation_hash="abc",
            rule_kind="expected_answer",
            rule_value="x",
            confidence="bogus",
            derived_from=None,
            notes=None,
        )
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_db.py -v
```

Expected: FAIL — `bitgn_scraper.db` doesn't exist.

- [ ] **Step 3: Implement db.py**

```python
# src/bitgn_scraper/db.py
"""SQLite schema + thin typed accessors for the scraper store.

The schema mirrors the spec at
docs/superpowers/specs/2026-04-26-bitgn-local-harness-clone-design.md,
"Storage Layer". Each table has one responsibility:

  task_instantiations  one row per (task_id, instantiation_hash)
  workspace_files      one row per file inside an instantiation
  probe_log            append-only log of grader probes
  scoring_rules        parsed grader rules with provenance

This module never touches the network and never calls into other
scraper modules; it's the bottom of the import graph.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

EXPECTED_TABLES: tuple[str, ...] = (
    "task_instantiations",
    "workspace_files",
    "probe_log",
    "scoring_rules",
)

VALID_CONFIDENCE: frozenset[str] = frozenset({"high", "medium", "low"})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_instantiations (
    task_id              TEXT NOT NULL,
    instantiation_hash   TEXT NOT NULL,
    instruction          TEXT NOT NULL,
    instruction_hash     TEXT NOT NULL,
    tree_fingerprint     TEXT NOT NULL,
    context_time         TEXT NOT NULL,
    context_unix         INTEGER NOT NULL,
    benchmark_id         TEXT NOT NULL,
    scraped_at           TEXT NOT NULL,
    workspace_dir        TEXT NOT NULL,
    workspace_byte_total INTEGER NOT NULL,
    workspace_file_count INTEGER NOT NULL,
    PRIMARY KEY (task_id, instantiation_hash)
);

CREATE INDEX IF NOT EXISTS idx_task ON task_instantiations(task_id);

CREATE TABLE IF NOT EXISTS workspace_files (
    task_id            TEXT NOT NULL,
    instantiation_hash TEXT NOT NULL,
    path               TEXT NOT NULL,
    is_dir             INTEGER NOT NULL,
    byte_size          INTEGER NOT NULL,
    sha256             TEXT NOT NULL,
    PRIMARY KEY (task_id, instantiation_hash, path),
    FOREIGN KEY (task_id, instantiation_hash)
        REFERENCES task_instantiations(task_id, instantiation_hash)
);

CREATE TABLE IF NOT EXISTS probe_log (
    probe_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL,
    instantiation_hash TEXT NOT NULL,
    probe_kind         TEXT NOT NULL,
    submitted_answer   TEXT,
    submitted_refs     TEXT,
    submitted_outcome  TEXT,
    submitted_writes   TEXT,
    score              REAL,
    score_detail_raw   TEXT,
    trial_id           TEXT,
    probed_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_probe_task ON probe_log(task_id);

CREATE TABLE IF NOT EXISTS scoring_rules (
    rule_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL,
    instantiation_hash TEXT NOT NULL,
    rule_kind          TEXT NOT NULL,
    rule_value         TEXT NOT NULL,
    confidence         TEXT NOT NULL,
    derived_from       INTEGER,
    notes              TEXT,
    FOREIGN KEY (derived_from) REFERENCES probe_log(probe_id)
);

CREATE INDEX IF NOT EXISTS idx_rules_task ON scoring_rules(task_id, instantiation_hash);
"""


def init_schema(db_path: str | Path) -> None:
    """Create all tables if they don't exist. Idempotent."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context-managed connection with foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def insert_scoring_rule(
    db_path: str | Path,
    *,
    task_id: str,
    instantiation_hash: str,
    rule_kind: str,
    rule_value: str,
    confidence: str,
    derived_from: int | None,
    notes: str | None,
) -> int:
    """Insert one row into scoring_rules. Returns the new rule_id."""
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(
            f"confidence must be one of {sorted(VALID_CONFIDENCE)!r}, got {confidence!r}"
        )
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO scoring_rules "
            "(task_id, instantiation_hash, rule_kind, rule_value, "
            " confidence, derived_from, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, instantiation_hash, rule_kind, rule_value,
             confidence, derived_from, notes),
        )
        conn.commit()
        return int(cur.lastrowid)
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_db.py -v
```

Expected: PASS — all 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_scraper/db.py tests/scraper/test_db.py
git commit -m "feat(scraper): SQLite schema + scoring_rules accessor"
```

---

### Task 3: Tree fingerprint helper (`src/bitgn_scraper/fingerprint.py`)

**Files:**
- Create: `src/bitgn_scraper/fingerprint.py`
- Create: `tests/scraper/test_fingerprint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_fingerprint.py
"""Tree-fingerprint determinism tests."""
from __future__ import annotations

from bitgn_scraper.fingerprint import FileRecord, instantiation_hash, tree_fingerprint


def test_tree_fingerprint_is_deterministic() -> None:
    files = [
        FileRecord(path="10_entities/cast/nina.md", sha256="aaa", byte_size=329),
        FileRecord(path="50_finance/x.md", sha256="bbb", byte_size=128),
    ]
    h1 = tree_fingerprint(files)
    h2 = tree_fingerprint(list(reversed(files)))  # order-independent
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_tree_fingerprint_changes_on_content() -> None:
    base = [FileRecord(path="a", sha256="x", byte_size=1)]
    other = [FileRecord(path="a", sha256="y", byte_size=1)]
    assert tree_fingerprint(base) != tree_fingerprint(other)


def test_tree_fingerprint_changes_on_size() -> None:
    base = [FileRecord(path="a", sha256="x", byte_size=1)]
    other = [FileRecord(path="a", sha256="x", byte_size=2)]
    assert tree_fingerprint(base) != tree_fingerprint(other)


def test_tree_fingerprint_changes_on_path() -> None:
    base = [FileRecord(path="a", sha256="x", byte_size=1)]
    other = [FileRecord(path="b", sha256="x", byte_size=1)]
    assert tree_fingerprint(base) != tree_fingerprint(other)


def test_tree_fingerprint_empty() -> None:
    h = tree_fingerprint([])
    assert len(h) == 64


def test_instantiation_hash_combines_inputs() -> None:
    files = [FileRecord(path="a", sha256="x", byte_size=1)]
    h_a = instantiation_hash("instr-A", files)
    h_b = instantiation_hash("instr-B", files)
    assert h_a != h_b
    assert len(h_a) == 64

    different_files = [FileRecord(path="a", sha256="y", byte_size=1)]
    h_c = instantiation_hash("instr-A", different_files)
    assert h_a != h_c
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_fingerprint.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement fingerprint.py**

```python
# src/bitgn_scraper/fingerprint.py
"""Deterministic hashes for workspace trees and instantiations.

Two trials with identical instructions but different file contents
must hash to different instantiation_hashes — see spec critique fix
#2. Combining instruction text and tree fingerprint achieves this
without relying on either alone.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class FileRecord:
    path: str
    sha256: str
    byte_size: int


def tree_fingerprint(files: Sequence[FileRecord]) -> str:
    """SHA-256 hex of a sorted manifest of (path, byte_size, sha256)."""
    h = hashlib.sha256()
    for rec in sorted(files, key=lambda r: r.path):
        h.update(f"{rec.path}\t{rec.byte_size}\t{rec.sha256}\n".encode("utf-8"))
    return h.hexdigest()


def instantiation_hash(instruction: str, files: Sequence[FileRecord]) -> str:
    """Hash that uniquely identifies an (instruction, workspace) pair."""
    h = hashlib.sha256()
    h.update(instruction.encode("utf-8"))
    h.update(b"\x00")
    h.update(tree_fingerprint(files).encode("ascii"))
    return h.hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_fingerprint.py -v
```

Expected: PASS — 6 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_scraper/fingerprint.py tests/scraper/test_fingerprint.py
git commit -m "feat(scraper): tree fingerprint + instantiation hash"
```

---

### Task 4: Score-detail rule extractors (`src/bitgn_scraper/seed_rules.py`)

This task implements the regex extractors that turn `score_detail` strings into rule rows. Used immediately by Phase 1.5 (this plan) and later by Phase 2 probes (next plan).

**Files:**
- Create: `src/bitgn_scraper/seed_rules.py`
- Create: `tests/scraper/test_seed_rules.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_seed_rules.py
"""score_detail string parsing tests.

Fixture strings copied verbatim from real PROD logs:
  - cf90740 22LAfu4 t000 outcome trace
  - cf90740 22LAfu4 t066 outcome trace
  - vm-03owny32f4y68f9cda.eu.bitgn.com.txt server log
"""
from __future__ import annotations

from bitgn_scraper.seed_rules import ExtractedRule, extract_rules


def test_extracts_expected_answer() -> None:
    rules = extract_rules("answer is incorrect. Expected: '1989-02-16'")
    assert rules == [ExtractedRule(rule_kind="expected_answer", rule_value="1989-02-16")]


def test_extracts_required_write() -> None:
    rules = extract_rules(
        "missing file write '50_finance/purchases/2026_01_31__eur_000050__bill__hearthline_sensor_bundle.md'"
    )
    assert rules == [ExtractedRule(
        rule_kind="required_write",
        rule_value="50_finance/purchases/2026_01_31__eur_000050__bill__hearthline_sensor_bundle.md",
    )]


def test_extracts_required_ref() -> None:
    rules = extract_rules(
        "answer missing required reference '20_projects/cabin/plan.md'"
    )
    assert rules == [ExtractedRule(
        rule_kind="required_ref",
        rule_value="20_projects/cabin/plan.md",
    )]


def test_extracts_expected_outcome() -> None:
    rules = extract_rules(
        "expected outcome OUTCOME_OK, got OUTCOME_NONE_CLARIFICATION"
    )
    assert rules == [ExtractedRule(
        rule_kind="expected_outcome",
        rule_value="OUTCOME_OK",
    )]


def test_extracts_multiple_rules_from_one_string() -> None:
    """t066 had concatenated missing-write strings in one detail entry."""
    rules = extract_rules(
        "missing file write '50_finance/purchases/A.md' / "
        "missing file write '50_finance/purchases/B.md'"
    )
    assert ExtractedRule(rule_kind="required_write", rule_value="50_finance/purchases/A.md") in rules
    assert ExtractedRule(rule_kind="required_write", rule_value="50_finance/purchases/B.md") in rules
    assert len(rules) == 2


def test_returns_empty_for_unrecognized_string() -> None:
    rules = extract_rules("the agent panicked")
    assert rules == []


def test_handles_double_quotes_variant() -> None:
    """Some log lines use double quotes instead of single."""
    rules = extract_rules('answer is incorrect. Expected: "1989-02-16"')
    assert rules == [ExtractedRule(rule_kind="expected_answer", rule_value="1989-02-16")]
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_seed_rules.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement seed_rules.py**

```python
# src/bitgn_scraper/seed_rules.py
"""Regex extractors that turn grader score_detail strings into rules.

This module is shared by:
  - Phase 1.5 (mining existing PROD JSONL traces + server logs)
  - Phase 2  (parsing live probe responses)

It MUST stay pure: no I/O, no DB, no network. Add new patterns by
appending to PATTERNS. Each pattern is a (regex, rule_kind) pair.
The regex's first capture group becomes the rule_value; if there is
a second capture group, see the per-pattern handling in extract_rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Pattern


@dataclass(frozen=True)
class ExtractedRule:
    rule_kind: str
    rule_value: str


_QUOTE = r"['\"]"

# Patterns ordered by specificity. Each entry compiles to a finditer
# call on the full score_detail string so that concatenated detail
# entries (e.g. "missing X / missing Y") yield multiple rules.
_PATTERNS: list[tuple[Pattern[str], str]] = [
    (re.compile(rf"answer is incorrect\. Expected:\s*{_QUOTE}([^'\"]+){_QUOTE}"), "expected_answer"),
    (re.compile(rf"missing file write\s*{_QUOTE}([^'\"]+){_QUOTE}"), "required_write"),
    (re.compile(rf"answer missing required reference\s*{_QUOTE}([^'\"]+){_QUOTE}"), "required_ref"),
    (re.compile(r"expected outcome\s+(\w+),\s*got\s+(\w+)"), "expected_outcome"),
]


def extract_rules(score_detail: str) -> list[ExtractedRule]:
    """Run every pattern across the input string and collect all matches."""
    out: list[ExtractedRule] = []
    for pattern, kind in _PATTERNS:
        for m in pattern.finditer(score_detail):
            value = m.group(1)
            out.append(ExtractedRule(rule_kind=kind, rule_value=value))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_seed_rules.py -v
```

Expected: PASS — 7 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_scraper/seed_rules.py tests/scraper/test_seed_rules.py
git commit -m "feat(scraper): score_detail rule extractors"
```

---

### Task 5: Outcome-JSONL miner (`src/bitgn_scraper/seed_outcomes.py`)

**Files:**
- Create: `src/bitgn_scraper/seed_outcomes.py`
- Create: `tests/scraper/test_seed_outcomes.py`
- Create: `tests/scraper/fixtures/sample_outcome.jsonl`

- [ ] **Step 1: Create the fixture**

Copy two real lines from `logs/prod_cf90740_full/20260425_181902/t000.jsonl` (one `kind=meta`, one `kind=outcome` with score=0.0). Use this fixture content:

```jsonl
{"kind":"meta","agent_version":"0.1.18","agent_commit":"cf90740","model":"gpt-5.3-codex","backend":"openai_compat","reasoning_effort":"medium","benchmark":"bitgn/pac1-prod","task_id":"t000","task_index":0,"started_at":"2026-04-25T18:19:03.255908+00:00","trace_schema_version":"1.0.0","harness_url":"https://vm-03p6voi9ip8mbnb02d.eu.bitgn.com","cancelled":false,"intent_head":"When was my partner born? Answer YYYY-MM-DD. Date only"}
{"kind":"task","task_id":"t000","task_text":"When was my partner born? Answer YYYY-MM-DD. Date only"}
{"kind":"outcome","terminated_by":"report_completion","reported":"OUTCOME_OK","enforcer_bypassed":false,"error_kind":null,"error_msg":null,"total_steps":2,"total_llm_calls":2,"total_prompt_tokens":15930,"total_completion_tokens":1119,"total_cached_tokens":0,"total_reasoning_tokens":558,"score":0.0,"score_detail":["answer is incorrect. Expected: '1989-02-16'"]}
```

Save to: `tests/scraper/fixtures/sample_outcome.jsonl`

- [ ] **Step 2: Write the failing test**

```python
# tests/scraper/test_seed_outcomes.py
"""Outcome-JSONL miner tests."""
from __future__ import annotations

from pathlib import Path

from bitgn_scraper.seed_outcomes import OutcomeFinding, mine_outcomes_dir, mine_outcomes_file

FIXTURE = Path(__file__).parent / "fixtures" / "sample_outcome.jsonl"


def test_mine_single_file_extracts_failed_outcome() -> None:
    findings = mine_outcomes_file(FIXTURE)
    assert len(findings) == 1
    f = findings[0]
    assert f.task_id == "t000"
    assert f.score == 0.0
    assert f.score_detail == ["answer is incorrect. Expected: '1989-02-16'"]
    assert f.intent_head == "When was my partner born? Answer YYYY-MM-DD. Date only"
    assert f.benchmark_id == "bitgn/pac1-prod"
    assert f.source_path == str(FIXTURE)


def test_mine_dir_walks_jsonl_files(tmp_path: Path) -> None:
    # Copy fixture twice with different names
    (tmp_path / "t000.jsonl").write_bytes(FIXTURE.read_bytes())
    (tmp_path / "t001.jsonl").write_bytes(FIXTURE.read_bytes())
    findings = mine_outcomes_dir(tmp_path)
    assert len(findings) == 2


def test_mine_skips_passing_outcomes(tmp_path: Path) -> None:
    passing = (
        '{"kind":"meta","task_id":"t999","intent_head":"x","benchmark":"b"}\n'
        '{"kind":"outcome","score":1.0,"score_detail":null}\n'
    )
    p = tmp_path / "t999.jsonl"
    p.write_text(passing)
    findings = mine_outcomes_file(p)
    assert findings == []


def test_mine_skips_outcomes_without_detail(tmp_path: Path) -> None:
    """Score=0.0 with empty detail can't seed a rule — skip."""
    no_detail = (
        '{"kind":"meta","task_id":"t777","intent_head":"x","benchmark":"b"}\n'
        '{"kind":"outcome","score":0.0,"score_detail":[]}\n'
    )
    p = tmp_path / "t777.jsonl"
    p.write_text(no_detail)
    findings = mine_outcomes_file(p)
    assert findings == []
```

- [ ] **Step 3: Run test to verify it fails**

```
uv run pytest tests/scraper/test_seed_outcomes.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 4: Implement seed_outcomes.py**

```python
# src/bitgn_scraper/seed_outcomes.py
"""Mine existing PROD JSONL traces for failed-task score_detail strings.

Each agent run writes one .jsonl per task. We pick the kind=meta line
(for task_id, intent_head, benchmark) and the kind=outcome line (for
score and score_detail). Returns one OutcomeFinding per failed task
with non-empty detail.

These findings feed seed_rules.extract_rules to produce confidence='high'
rule rows without any new API calls — they are already-paid-for evidence
sitting in the repo from prior PROD runs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OutcomeFinding:
    task_id: str
    intent_head: str
    benchmark_id: str
    score: float
    score_detail: list[str]
    source_path: str


def mine_outcomes_file(path: Path) -> list[OutcomeFinding]:
    """Parse one JSONL trace; return failed-task findings (may be 0)."""
    meta: dict | None = None
    outcome: dict | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("kind")
        if kind == "meta":
            meta = obj
        elif kind == "outcome":
            outcome = obj
    if meta is None or outcome is None:
        return []
    score = float(outcome.get("score", 0.0))
    detail = outcome.get("score_detail") or []
    if score >= 1.0:
        return []
    if not detail:
        return []
    return [OutcomeFinding(
        task_id=meta.get("task_id", ""),
        intent_head=meta.get("intent_head", ""),
        benchmark_id=meta.get("benchmark", ""),
        score=score,
        score_detail=list(detail),
        source_path=str(path),
    )]


def mine_outcomes_dir(root: Path) -> list[OutcomeFinding]:
    """Walk every *.jsonl file under root, aggregate findings."""
    out: list[OutcomeFinding] = []
    for p in sorted(root.rglob("*.jsonl")):
        out.extend(mine_outcomes_file(p))
    return out
```

- [ ] **Step 5: Run test to verify it passes**

```
uv run pytest tests/scraper/test_seed_outcomes.py -v
```

Expected: PASS — 4 tests green.

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_scraper/seed_outcomes.py \
        tests/scraper/test_seed_outcomes.py \
        tests/scraper/fixtures/sample_outcome.jsonl
git commit -m "feat(scraper): mine failed-task outcomes from JSONL traces"
```

---

### Task 6: Server-log miner (`src/bitgn_scraper/seed_server_logs.py`)

**Files:**
- Create: `src/bitgn_scraper/seed_server_logs.py`
- Create: `tests/scraper/test_seed_server_logs.py`
- Create: `tests/scraper/fixtures/sample_server.log`

- [ ] **Step 1: Create the fixture**

Save to: `tests/scraper/fixtures/sample_server.log`

```
2026-04-13T10:34:11Z [ INFO ] starting agent run for t030
2026-04-13T10:34:13Z [ INFO ] agent reading 50_finance/spool/some_doc.md
2026-04-13T10:35:04Z [ INFO ] agent submitted answer: "PLA spool from acme"
2026-04-13T10:35:05Z [ ERR ] AI agent score 0.00 / answer is incorrect. Expected: '3D-Druck PLA-Filament 1.75mm'
2026-04-13T10:35:06Z [ INFO ] trial complete
```

- [ ] **Step 2: Write the failing test**

```python
# tests/scraper/test_seed_server_logs.py
"""Server-log miner tests."""
from __future__ import annotations

from pathlib import Path

from bitgn_scraper.seed_server_logs import ServerLogFinding, mine_server_log

FIXTURE = Path(__file__).parent / "fixtures" / "sample_server.log"


def test_mine_extracts_score_and_detail() -> None:
    findings = mine_server_log(FIXTURE)
    assert len(findings) == 1
    f = findings[0]
    assert f.score == 0.0
    assert f.score_detail == "answer is incorrect. Expected: '3D-Druck PLA-Filament 1.75mm'"
    assert f.source_path == str(FIXTURE)


def test_mine_returns_empty_when_no_score_line(tmp_path: Path) -> None:
    p = tmp_path / "noscore.log"
    p.write_text("[ INFO ] nothing here\n")
    assert mine_server_log(p) == []


def test_mine_handles_passing_score_line(tmp_path: Path) -> None:
    """`AI agent score 1.00` → no findings (we only seed from failures)."""
    p = tmp_path / "pass.log"
    p.write_text("[ ERR ] AI agent score 1.00 / great job\n")
    assert mine_server_log(p) == []
```

- [ ] **Step 3: Run test to verify it fails**

```
uv run pytest tests/scraper/test_seed_server_logs.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 4: Implement seed_server_logs.py**

```python
# src/bitgn_scraper/seed_server_logs.py
"""Mine BitGN server-side .log files for failed-task score lines.

Server logs (vm-*.eu.bitgn.com.txt and t*-*.log files at repo root)
end each trial with one or more lines like:

  2026-04-13T10:35:05Z [ ERR ] AI agent score 0.00 / <score_detail>

We extract just the (score, score_detail) tail. The task_id is unknown
from the log alone — caller decides how to attribute findings (typically
by VM hostname → task mapping or filename convention).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SCORE_LINE = re.compile(
    r"\[\s*ERR\s*\]\s*AI agent score\s+([0-9.]+)\s*/\s*(.+?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ServerLogFinding:
    score: float
    score_detail: str
    source_path: str


def mine_server_log(path: Path) -> list[ServerLogFinding]:
    """Return failed-task score lines from a server log."""
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[ServerLogFinding] = []
    for m in _SCORE_LINE.finditer(text):
        score = float(m.group(1))
        if score >= 1.0:
            continue
        out.append(ServerLogFinding(
            score=score,
            score_detail=m.group(2),
            source_path=str(path),
        ))
    return out
```

- [ ] **Step 5: Run test to verify it passes**

```
uv run pytest tests/scraper/test_seed_server_logs.py -v
```

Expected: PASS — 3 tests green.

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_scraper/seed_server_logs.py \
        tests/scraper/test_seed_server_logs.py \
        tests/scraper/fixtures/sample_server.log
git commit -m "feat(scraper): mine failed-task score lines from server logs"
```

---

### Task 7: Seed-rules orchestrator + CLI subcommand

Wires Tasks 4–6 together: walks the existing JSONL trace dir + the server log files, runs the regex extractors, writes confidence='high' rows into `scoring_rules`. Reports counts.

**Files:**
- Modify: `src/bitgn_scraper/seed_rules.py` — append `run_seed_cli()` + helpers
- Modify: `tests/scraper/test_seed_rules.py` — add orchestration tests

- [ ] **Step 1: Write the failing test (append to test_seed_rules.py)**

Append the following at the end of `tests/scraper/test_seed_rules.py`:

```python
# --- Orchestration tests (Task 7) ---

import json
from pathlib import Path

import pytest

from bitgn_scraper.db import init_schema, connect
from bitgn_scraper.seed_rules import seed_from_outcomes, seed_from_server_logs


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    init_schema(p)
    return p


def test_seed_from_outcomes_writes_rules(tmp_path: Path, db: Path) -> None:
    fixture = tmp_path / "t000.jsonl"
    fixture.write_text(
        '{"kind":"meta","task_id":"t000","intent_head":"x","benchmark":"bitgn/pac1-prod"}\n'
        '{"kind":"outcome","score":0.0,"score_detail":["answer is incorrect. Expected: \'1989-02-16\'"]}\n'
    )
    n_rules = seed_from_outcomes(db_path=db, jsonl_root=tmp_path)
    assert n_rules == 1
    with connect(db) as conn:
        row = conn.execute(
            "SELECT task_id, rule_kind, rule_value, confidence "
            "FROM scoring_rules"
        ).fetchone()
    assert row == ("t000", "expected_answer", "1989-02-16", "high")


def test_seed_from_server_logs_attributes_by_filename(tmp_path: Path, db: Path) -> None:
    """Filename convention: `t<NN>-*.log` → task_id=t<NN>."""
    log = tmp_path / "t030-202604131034.log"
    log.write_text(
        "[ ERR ] AI agent score 0.00 / answer is incorrect. Expected: '3D-Druck PLA-Filament 1.75mm'\n"
    )
    n_rules = seed_from_server_logs(db_path=db, log_paths=[log])
    assert n_rules == 1
    with connect(db) as conn:
        row = conn.execute(
            "SELECT task_id, rule_kind, rule_value FROM scoring_rules"
        ).fetchone()
    assert row[0] == "t030"
    assert row[1] == "expected_answer"


def test_seed_skips_unparseable_detail(tmp_path: Path, db: Path) -> None:
    """If extract_rules returns empty, no rows are written."""
    fixture = tmp_path / "t111.jsonl"
    fixture.write_text(
        '{"kind":"meta","task_id":"t111","intent_head":"x","benchmark":"b"}\n'
        '{"kind":"outcome","score":0.0,"score_detail":["unparseable garbage"]}\n'
    )
    n_rules = seed_from_outcomes(db_path=db, jsonl_root=tmp_path)
    assert n_rules == 0
    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM scoring_rules").fetchone()[0]
    assert n == 0
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_seed_rules.py -v
```

Expected: FAIL — `seed_from_outcomes`, `seed_from_server_logs`, `run_seed_cli` not defined.

- [ ] **Step 3: Append the orchestration code to seed_rules.py**

Append to the end of `src/bitgn_scraper/seed_rules.py`:

```python
# --- Orchestration ---

import argparse
import os
import re
import sys
from pathlib import Path

from bitgn_scraper.db import insert_scoring_rule
from bitgn_scraper.seed_outcomes import mine_outcomes_dir
from bitgn_scraper.seed_server_logs import mine_server_log

_TASK_ID_RE = re.compile(r"\b(t\d{3})\b")
_DEFAULT_DB = Path("artifacts/harness_db/bitgn_local.db")
_DEFAULT_JSONL_ROOT = Path("logs/prod_cf90740_full")
_DEFAULT_SERVER_LOGS = [
    Path("vm-03owny32f4y68f9cda.eu.bitgn.com.txt"),
    Path("vm-03owny3353zxxh4fm7.eu.bitgn.com.txt"),
    Path("vm-03ox0hre13aqu0pme3.eu.bitgn.com.txt"),
    Path("vm-03ox0hreyjfinmhrvo.eu.bitgn.com.txt"),
    Path("other-prod-run.txt"),
]


def _task_id_from_path(p: Path) -> str | None:
    m = _TASK_ID_RE.search(p.name)
    return m.group(1) if m else None


def seed_from_outcomes(*, db_path: Path, jsonl_root: Path) -> int:
    """Mine JSONL outcome events, write confidence='high' rules, return count."""
    written = 0
    for finding in mine_outcomes_dir(jsonl_root):
        for detail in finding.score_detail:
            for rule in extract_rules(detail):
                insert_scoring_rule(
                    db_path,
                    task_id=finding.task_id,
                    instantiation_hash="",  # unknown — pre-Phase-1 seed
                    rule_kind=rule.rule_kind,
                    rule_value=rule.rule_value,
                    confidence="high",
                    derived_from=None,
                    notes=f"seeded from outcome JSONL: {finding.source_path}",
                )
                written += 1
    return written


def seed_from_server_logs(*, db_path: Path, log_paths: list[Path]) -> int:
    """Mine server-side .log files, write confidence='high' rules, return count."""
    written = 0
    for path in log_paths:
        if not path.exists():
            continue
        task_id = _task_id_from_path(path) or ""
        for finding in mine_server_log(path):
            for rule in extract_rules(finding.score_detail):
                insert_scoring_rule(
                    db_path,
                    task_id=task_id,
                    instantiation_hash="",
                    rule_kind=rule.rule_kind,
                    rule_value=rule.rule_value,
                    confidence="high",
                    derived_from=None,
                    notes=f"seeded from server log: {finding.source_path}",
                )
                written += 1
    return written


def run_seed_cli() -> int:
    """Entry point invoked by scripts/bitgn_scraper.py seed."""
    parser = argparse.ArgumentParser(prog="bitgn_scraper seed")
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument("--jsonl-root", type=Path, default=_DEFAULT_JSONL_ROOT)
    args = parser.parse_args(sys.argv[2:])  # skip "scripts/.. seed"

    from bitgn_scraper.db import init_schema
    init_schema(args.db)

    n_jsonl = seed_from_outcomes(db_path=args.db, jsonl_root=args.jsonl_root)
    n_logs = seed_from_server_logs(db_path=args.db, log_paths=_DEFAULT_SERVER_LOGS)

    print(f"seed: wrote {n_jsonl} rules from JSONL outcomes")
    print(f"seed: wrote {n_logs} rules from server logs")
    print(f"seed: db at {args.db}")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_seed_rules.py -v
```

Expected: PASS — all original 7 + 3 new tests green.

- [ ] **Step 5: Smoke-test the CLI against real repo data**

```
uv run python scripts/bitgn_scraper.py seed
```

Expected: at least 2 rules written from JSONL (cf90740 t000 + t066 failures), additional rules from server logs. Print lines should look like:

```
seed: wrote 2 rules from JSONL outcomes
seed: wrote 5 rules from server logs
seed: db at artifacts/harness_db/bitgn_local.db
```

Verify the DB:
```
sqlite3 artifacts/harness_db/bitgn_local.db \
  "SELECT task_id, rule_kind, rule_value FROM scoring_rules ORDER BY task_id;"
```

Expected output: rows for t000 (`expected_answer = '1989-02-16'`), t066 (two `required_write` rules), plus server-log derived rows for t030 etc.

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_scraper/seed_rules.py tests/scraper/test_seed_rules.py
git commit -m "feat(scraper): seed CLI wires JSONL + server-log mining into DB"
```

---

### Task 8: Authenticated SDK client factory (`src/bitgn_scraper/clients.py`)

**Files:**
- Create: `src/bitgn_scraper/clients.py`
- Create: `tests/scraper/test_clients.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_clients.py
"""SDK client factory tests.

Network is never touched: we just verify the auth interceptor is wired
and that BITGN_BASE_URL is honored.
"""
from __future__ import annotations

import pytest

from bitgn_scraper.clients import build_harness_client, build_pcm_client


def test_build_harness_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BITGN_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="BITGN_API_KEY"):
        build_harness_client()


def test_build_harness_client_uses_default_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_API_KEY", "fake")
    monkeypatch.delenv("BITGN_BASE_URL", raising=False)
    client = build_harness_client()
    # The Connect-RPC sync client doesn't expose .base directly; check
    # the captured argument via the factory's debug attribute.
    assert client._scraper_base_url == "https://api.bitgn.com"


def test_build_harness_client_honors_base_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_API_KEY", "fake")
    monkeypatch.setenv("BITGN_BASE_URL", "https://staging.bitgn.com/")
    client = build_harness_client()
    assert client._scraper_base_url == "https://staging.bitgn.com"


def test_build_pcm_client_requires_harness_url() -> None:
    with pytest.raises(ValueError, match="harness_url"):
        build_pcm_client("")
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_clients.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement clients.py**

```python
# src/bitgn_scraper/clients.py
"""Authenticated Connect-RPC client factories.

Reads BITGN_API_KEY (required) and BITGN_BASE_URL (optional, default
https://api.bitgn.com) from the env. Mirrors the auth pattern from
scripts/verify_prod_grader.py so the scraper and the existing probe
script both go through the same interceptor.
"""
from __future__ import annotations

import os
from typing import Any

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from connectrpc.interceptor import MetadataInterceptorSync


class _AuthInterceptor(MetadataInterceptorSync):
    def __init__(self, key: str) -> None:
        self._key = key

    def on_start_sync(self, ctx: Any) -> None:
        ctx.request_headers()["authorization"] = f"Bearer {self._key}"
        return None


def _api_key() -> str:
    key = os.environ.get("BITGN_API_KEY")
    if not key:
        raise RuntimeError(
            "BITGN_API_KEY is not set. Source .env first: "
            "`set -a && source .worktrees/plan-b/.env && set +a`"
        )
    return key


def build_harness_client() -> HarnessServiceClientSync:
    """Build an authenticated HarnessService client pointed at PROD."""
    key = _api_key()
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com").rstrip("/")
    client = HarnessServiceClientSync(base, interceptors=(_AuthInterceptor(key),))
    # Pin the base URL on the client object so tests + diagnostics can
    # introspect what we connected to without re-reading the env.
    client._scraper_base_url = base  # type: ignore[attr-defined]
    return client


def build_pcm_client(harness_url: str) -> PcmRuntimeClientSync:
    """Build an authenticated PCM client for a specific trial sandbox."""
    if not harness_url:
        raise ValueError("harness_url is required (got empty string)")
    key = _api_key()
    base = harness_url.rstrip("/")
    return PcmRuntimeClientSync(base, interceptors=(_AuthInterceptor(key),))
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_clients.py -v
```

Expected: PASS — 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_scraper/clients.py tests/scraper/test_clients.py
git commit -m "feat(scraper): authenticated Harness + PCM client factories"
```

---

### Task 9: Phase 0 lifecycle spike runner (`src/bitgn_scraper/phase0.py`)

This implements all 7 Phase 0 sub-spikes from the spec. Each sub-spike is a separate function that returns a structured result. The orchestrator runs them all and writes one `lifecycle_spike.json`.

**Test strategy:** Pure unit tests cover the result-struct shapes and JSON serialisation. The actual `StartPlayground` calls are exercised only when `run_phase0_cli()` is invoked manually — that's an integration step the implementer runs once and inspects the output.

**Files:**
- Create: `src/bitgn_scraper/phase0.py`
- Create: `tests/scraper/test_phase0_shape.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scraper/test_phase0_shape.py
"""Phase 0 result-shape tests (no live API calls)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from bitgn_scraper.phase0 import (
    LifecycleReport,
    RotationFinding,
    UrlLifetimeFinding,
    AnswerReplayFinding,
    RateLimitFinding,
    SizeSanityFinding,
    StateIsolationFinding,
    AutoTerminationFinding,
    serialize_report,
)


def test_serialize_report_round_trip() -> None:
    report = LifecycleReport(
        started_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
        rotation=RotationFinding(
            task_id="t001",
            n_calls=20,
            distinct_instructions=5,
            sample_instructions=["a", "b", "c"],
        ),
        url_lifetime=UrlLifetimeFinding(
            trial_id="trial_x",
            harness_url="https://vm-x.eu.bitgn.com",
            probe_offsets_seconds=[0, 5, 30, 300, 1800],
            reachable_at_offset=[True, True, True, False, False],
        ),
        auto_termination=AutoTerminationFinding(
            trial_id="trial_y",
            probe_offsets_seconds=[600, 1800, 7200],
            reachable_at_offset=[True, False, False],
            inferred_max_lifetime_seconds=1800,
        ),
        state_isolation=StateIsolationFinding(
            wrote_path="/_probe.txt",
            second_trial_saw_write=False,
        ),
        answer_replay=AnswerReplayFinding(
            first_answer="alpha",
            second_answer="beta",
            graded_against="beta",
        ),
        rate_limit=RateLimitFinding(
            n_parallel_calls=20,
            n_throttled=0,
            throttle_status_codes=[],
        ),
        size_sanity=SizeSanityFinding(
            sampled_task_ids=["t001", "t005", "t010", "t020", "t050"],
            byte_totals=[1024, 4096, 8192, 16384, 32768],
            max_byte_total=32768,
        ),
    )
    blob = serialize_report(report)
    parsed = json.loads(blob)
    assert parsed["rotation"]["task_id"] == "t001"
    assert parsed["url_lifetime"]["reachable_at_offset"] == [True, True, True, False, False]
    assert parsed["state_isolation"]["second_trial_saw_write"] is False
    assert parsed["size_sanity"]["max_byte_total"] == 32768
    # ISO timestamp serialised as a string
    assert isinstance(parsed["started_at"], str)
    assert parsed["started_at"].startswith("2026-04-26T12:00:00")
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/scraper/test_phase0_shape.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement phase0.py**

```python
# src/bitgn_scraper/phase0.py
"""Phase 0 lifecycle spike — answers the spec's empirical open questions.

Sub-spikes:
  1. rotation              — does StartPlayground rotate instruction text?
  2. url_lifetime          — how long does harness_url stay reachable post-EndTrial?
  3. auto_termination      — does an unended trial auto-terminate? At what age?
  4. state_isolation       — does a write in trial N persist to trial N+1?
  5. answer_replay         — does the grader use the first or last Answer?
  6. rate_limit            — what concurrency level triggers throttling?
  7. size_sanity           — how big are the largest workspaces?

Output: artifacts/harness_db/scrape_runs/<ts>/lifecycle_spike.json
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RotationFinding:
    task_id: str
    n_calls: int
    distinct_instructions: int
    sample_instructions: list[str]


@dataclass(frozen=True)
class UrlLifetimeFinding:
    trial_id: str
    harness_url: str
    probe_offsets_seconds: list[int]
    reachable_at_offset: list[bool]


@dataclass(frozen=True)
class AutoTerminationFinding:
    trial_id: str
    probe_offsets_seconds: list[int]
    reachable_at_offset: list[bool]
    inferred_max_lifetime_seconds: int | None


@dataclass(frozen=True)
class StateIsolationFinding:
    wrote_path: str
    second_trial_saw_write: bool


@dataclass(frozen=True)
class AnswerReplayFinding:
    first_answer: str
    second_answer: str
    graded_against: str  # "first" | "second" | "unknown"


@dataclass(frozen=True)
class RateLimitFinding:
    n_parallel_calls: int
    n_throttled: int
    throttle_status_codes: list[int]


@dataclass(frozen=True)
class SizeSanityFinding:
    sampled_task_ids: list[str]
    byte_totals: list[int]
    max_byte_total: int


@dataclass(frozen=True)
class LifecycleReport:
    started_at: datetime
    rotation: RotationFinding
    url_lifetime: UrlLifetimeFinding
    auto_termination: AutoTerminationFinding
    state_isolation: StateIsolationFinding
    answer_replay: AnswerReplayFinding
    rate_limit: RateLimitFinding
    size_sanity: SizeSanityFinding


def serialize_report(report: LifecycleReport) -> str:
    """JSON-encode a LifecycleReport with ISO datetime."""
    payload: dict[str, Any] = asdict(report)
    payload["started_at"] = report.started_at.isoformat()
    return json.dumps(payload, indent=2, sort_keys=True)


def _spike_rotation(client: Any, task_id: str, n_calls: int) -> RotationFinding:
    """Sub-spike 1 — rotation detection."""
    from bitgn.harness_pb2 import StartPlaygroundRequest

    instructions: list[str] = []
    for _ in range(n_calls):
        resp = client.start_playground(
            StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
        )
        instructions.append(resp.instruction)
    distinct = sorted(set(instructions))
    return RotationFinding(
        task_id=task_id,
        n_calls=n_calls,
        distinct_instructions=len(distinct),
        sample_instructions=distinct[:5],
    )


def _spike_url_lifetime(client: Any, task_id: str) -> UrlLifetimeFinding:
    """Sub-spike 2 — harness_url lifetime after EndTrial."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn_scraper.clients import build_pcm_client
    from bitgn.vm.pcm_pb2 import ContextRequest

    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    client.end_trial(EndTrialRequest(trial_id=started.trial_id))

    offsets = [0, 5, 30, 300, 1800]
    reachable: list[bool] = []
    pcm = build_pcm_client(started.harness_url)
    t0 = time.time()
    for off in offsets:
        target = t0 + off
        sleep_for = max(0.0, target - time.time())
        if sleep_for > 0:
            time.sleep(sleep_for)
        try:
            pcm.context(ContextRequest())
            reachable.append(True)
        except Exception:
            reachable.append(False)
    return UrlLifetimeFinding(
        trial_id=started.trial_id,
        harness_url=started.harness_url,
        probe_offsets_seconds=offsets,
        reachable_at_offset=reachable,
    )


def _spike_auto_termination(client: Any, task_id: str) -> AutoTerminationFinding:
    """Sub-spike 3 — does an unended trial auto-terminate?"""
    from bitgn.harness_pb2 import StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ContextRequest
    from bitgn_scraper.clients import build_pcm_client

    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    pcm = build_pcm_client(started.harness_url)
    offsets = [600, 1800, 7200]
    reachable: list[bool] = []
    t0 = time.time()
    for off in offsets:
        target = t0 + off
        sleep_for = max(0.0, target - time.time())
        if sleep_for > 0:
            time.sleep(sleep_for)
        try:
            pcm.context(ContextRequest())
            reachable.append(True)
        except Exception:
            reachable.append(False)
    inferred: int | None = None
    for i, ok in enumerate(reachable):
        if not ok:
            inferred = offsets[i - 1] if i > 0 else 0
            break
    return AutoTerminationFinding(
        trial_id=started.trial_id,
        probe_offsets_seconds=offsets,
        reachable_at_offset=reachable,
        inferred_max_lifetime_seconds=inferred,
    )


def _spike_state_isolation(client: Any, task_id: str) -> StateIsolationFinding:
    """Sub-spike 4 — does a Write in trial N persist to trial N+1?"""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ReadRequest, WriteRequest
    from bitgn_scraper.clients import build_pcm_client

    probe_path = "/_scraper_probe.txt"
    probe_content = "scraper-probe"

    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    pcm1 = build_pcm_client(started.harness_url)
    pcm1.write(WriteRequest(path=probe_path, content=probe_content))
    client.end_trial(EndTrialRequest(trial_id=started.trial_id))

    started2 = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    pcm2 = build_pcm_client(started2.harness_url)
    saw = False
    try:
        resp = pcm2.read(ReadRequest(path=probe_path))
        saw = (resp.content == probe_content)
    except Exception:
        saw = False
    client.end_trial(EndTrialRequest(trial_id=started2.trial_id))
    return StateIsolationFinding(
        wrote_path=probe_path,
        second_trial_saw_write=saw,
    )


def _spike_answer_replay(client: Any, task_id: str) -> AnswerReplayFinding:
    """Sub-spike 5 — does the grader use the first or last Answer?"""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome
    from bitgn_scraper.clients import build_pcm_client

    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    pcm = build_pcm_client(started.harness_url)
    pcm.answer(AnswerRequest(message="alpha", outcome=Outcome.OUTCOME_OK))
    pcm.answer(AnswerRequest(message="beta", outcome=Outcome.OUTCOME_OK))

    ended = client.end_trial(EndTrialRequest(trial_id=started.trial_id))
    detail = " ".join(list(ended.score_detail) or [])
    if "alpha" in detail and "beta" not in detail:
        graded = "first"
    elif "beta" in detail and "alpha" not in detail:
        graded = "second"
    else:
        graded = "unknown"
    return AnswerReplayFinding(
        first_answer="alpha",
        second_answer="beta",
        graded_against=graded,
    )


def _spike_rate_limit(client: Any, task_id: str, n_parallel: int) -> RateLimitFinding:
    """Sub-spike 6 — n_parallel concurrent StartPlayground calls."""
    import concurrent.futures
    from bitgn.harness_pb2 import StartPlaygroundRequest

    def _one() -> tuple[bool, int | None]:
        try:
            client.start_playground(
                StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
            )
            return (True, None)
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            return (False, int(code) if isinstance(code, int) else None)

    throttled_codes: list[int] = []
    n_throttled = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_parallel) as ex:
        futs = [ex.submit(_one) for _ in range(n_parallel)]
        for fut in concurrent.futures.as_completed(futs):
            ok, code = fut.result()
            if not ok:
                n_throttled += 1
                if code is not None:
                    throttled_codes.append(code)
    return RateLimitFinding(
        n_parallel_calls=n_parallel,
        n_throttled=n_throttled,
        throttle_status_codes=sorted(set(throttled_codes)),
    )


def _spike_size_sanity(client: Any, task_ids: list[str]) -> SizeSanityFinding:
    """Sub-spike 7 — sample workspace byte totals."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ReadRequest, TreeRequest
    from bitgn_scraper.clients import build_pcm_client

    totals: list[int] = []
    for tid in task_ids:
        started = client.start_playground(
            StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=tid)
        )
        pcm = build_pcm_client(started.harness_url)
        tree = pcm.tree(TreeRequest(root="/"))
        total = _walk_tree_byte_total(pcm, tree.root, "")
        totals.append(total)
        client.end_trial(EndTrialRequest(trial_id=started.trial_id))
    return SizeSanityFinding(
        sampled_task_ids=task_ids,
        byte_totals=totals,
        max_byte_total=max(totals) if totals else 0,
    )


def _walk_tree_byte_total(pcm: Any, entry: Any, prefix: str) -> int:
    """Recursive tree-walk; sums byte counts of all files."""
    from bitgn.vm.pcm_pb2 import ReadRequest

    name = entry.name or ""
    path = prefix + ("/" + name if name and name != "/" else "")
    if entry.is_dir:
        return sum(_walk_tree_byte_total(pcm, c, path) for c in entry.children)
    try:
        resp = pcm.read(ReadRequest(path=path or "/"))
        return len(resp.content.encode("utf-8"))
    except Exception:
        return 0


def run_phase0_cli() -> int:
    """Run all 7 sub-spikes and dump lifecycle_spike.json."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="bitgn_scraper phase0")
    parser.add_argument("--task-id", default="t001",
                        help="task to use for single-task spikes (rotation, lifetime, ...)")
    parser.add_argument("--n-rotation-calls", type=int, default=20)
    parser.add_argument("--n-rate-parallel", type=int, default=20)
    parser.add_argument("--size-sample", default="t001,t010,t020,t030,t050")
    parser.add_argument("--out-root", type=Path,
                        default=Path("artifacts/harness_db/scrape_runs"))
    args = parser.parse_args(sys.argv[2:])

    from bitgn_scraper.clients import build_harness_client
    client = build_harness_client()

    started_at = datetime.now(tz=timezone.utc)
    print(f"[phase0] starting at {started_at.isoformat()}", flush=True)

    print(f"[phase0] (1/7) rotation on {args.task_id}", flush=True)
    rotation = _spike_rotation(client, args.task_id, args.n_rotation_calls)

    print(f"[phase0] (2/7) url_lifetime on {args.task_id}", flush=True)
    url_lifetime = _spike_url_lifetime(client, args.task_id)

    print(f"[phase0] (3/7) auto_termination on {args.task_id} — long-running", flush=True)
    auto_termination = _spike_auto_termination(client, args.task_id)

    print(f"[phase0] (4/7) state_isolation on {args.task_id}", flush=True)
    state_isolation = _spike_state_isolation(client, args.task_id)

    print(f"[phase0] (5/7) answer_replay on {args.task_id}", flush=True)
    answer_replay = _spike_answer_replay(client, args.task_id)

    print(f"[phase0] (6/7) rate_limit n={args.n_rate_parallel}", flush=True)
    rate_limit = _spike_rate_limit(client, args.task_id, args.n_rate_parallel)

    print(f"[phase0] (7/7) size_sanity on {args.size_sample}", flush=True)
    size_sanity = _spike_size_sanity(client, args.size_sample.split(","))

    report = LifecycleReport(
        started_at=started_at,
        rotation=rotation,
        url_lifetime=url_lifetime,
        auto_termination=auto_termination,
        state_isolation=state_isolation,
        answer_replay=answer_replay,
        rate_limit=rate_limit,
        size_sanity=size_sanity,
    )

    out_dir = args.out_root / started_at.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "lifecycle_spike.json"
    out_path.write_text(serialize_report(report))
    print(f"[phase0] wrote {out_path}", flush=True)
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/scraper/test_phase0_shape.py -v
```

Expected: PASS — 1 test green.

- [ ] **Step 5: Run the full scraper unit suite**

```
uv run pytest tests/scraper/ -v
```

Expected: PASS — every test from Tasks 1–9 green.

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_scraper/phase0.py tests/scraper/test_phase0_shape.py
git commit -m "feat(scraper): Phase 0 lifecycle spike runner"
```

---

### Task 10: Run the Phase 0 spike against PROD and capture findings

This is **not a code task** — it's the empirical step that produces the data the next plan needs.

**Files:** none (consumes existing code)

- [ ] **Step 1: Source env**

```
set -a && source .env && set +a
```

(If `.env` is missing, look at `.worktrees/plan-b/.env` per `feedback_no_task_hints` memory style.)

- [ ] **Step 2: Sanity-check auth before launching the long spike**

```
uv run python scripts/verify_prod_grader.py --task-id t001
```

Expected: real `score`/`score_detail` printed. If it fails, fix auth before continuing.

- [ ] **Step 3: Run a fast subset to verify the runner works**

```
uv run python scripts/bitgn_scraper.py phase0 \
  --n-rotation-calls 3 \
  --n-rate-parallel 3 \
  --size-sample t001
```

Expected (~1–2 minutes): `lifecycle_spike.json` written, all 7 finding objects populated, no Python tracebacks.

- [ ] **Step 4: Run the full spike**

```
uv run python scripts/bitgn_scraper.py phase0
```

Expected wall time: ~2 hours, dominated by the auto-termination probe (sleeps up to 7200 s). If you can't afford that, drop `--probe-offsets` to a shorter set in a follow-up patch — but the default values are what the next plan will assume.

Note: this step intentionally runs in the foreground rather than the background so the implementer can watch for early errors. If you'd rather background it, use `nohup uv run python scripts/bitgn_scraper.py phase0 > phase0.out 2>&1 &`.

- [ ] **Step 5: Commit the findings JSON**

```bash
git add -f artifacts/harness_db/scrape_runs/<TIMESTAMP>/lifecycle_spike.json
git commit -m "data(scraper): Phase 0 lifecycle spike findings vs PROD"
```

The `-f` is required because the directory is gitignored — we want this single JSON file in version control as the canonical empirical record.

- [ ] **Step 6: Summarise findings**

Read `lifecycle_spike.json` and write a one-paragraph human summary to `artifacts/harness_db/scrape_runs/<TIMESTAMP>/SUMMARY.md` covering: rotation pool size, sandbox lifetime post-EndTrial, auto-termination threshold, state isolation, answer-replay semantics, rate-limit ceiling, max workspace size. This summary is the input to the next plan (Phase 1+2+3).

```bash
git add artifacts/harness_db/scrape_runs/<TIMESTAMP>/SUMMARY.md
git commit -m "docs(scraper): summarise Phase 0 findings for next plan"
```

---

## Acceptance criteria for this plan

1. `uv run pytest tests/scraper/ -v` — all green.
2. `uv run python scripts/bitgn_scraper.py seed` — populates `scoring_rules` with at least 2 rules from JSONL outcomes (cf90740 t000 + t066 minimum) and ≥3 from server logs.
3. `uv run python scripts/bitgn_scraper.py phase0` — produces a `lifecycle_spike.json` with all 7 finding objects populated, plus a human SUMMARY.md.
4. No regressions in pre-existing tests: `uv run pytest -q` finishes green (excluding any tests that were already failing pre-plan; the implementer should baseline this on the first task).
5. Existing JSONL-trace tools (`scripts/intent_report.py`, `scripts/aggregate_findCS_findCI.py`) still parse traces correctly — sanity-check with one invocation each.

## What this plan does NOT deliver

- No Phase 1 workspace scrape (per-task tree-walk + read all files).
- No Phase 2 grader probe matrix (P1–P5, P2b, P6).
- No Phase 3 self-validation (determinism diff, integrity check, coverage report).
- No local gRPC harness server.
- No agent LLM-content trace gate.

Each of those gets its own plan, written after this plan ships and we have Phase 0 empirical data to ground the next-plan design.
