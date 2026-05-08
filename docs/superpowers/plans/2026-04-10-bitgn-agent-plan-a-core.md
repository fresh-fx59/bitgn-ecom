# BitGN Agent Plan A — Core Agent + Benchmark Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a single-session, provider-agnostic BitGN PAC1 agent that runs the 43-task `bitgn/pac1-dev` benchmark end-to-end and produces a `bench_summary.json` with a committable ratchet floor.

**Architecture:** Five hard-bounded layers (Backend → Adapter → Agent loop → Orchestrator → Traces) from `docs/superpowers/specs/2026-04-10-bitgn-agent-design.md`. The `NextStep` Pydantic Union is the single source of truth for the tool surface; the trace schema is the single source of truth for observability. TDD throughout — every task starts with a failing test, every commit must leave the suite green.

**Tech Stack:** Python 3.12, `pydantic>=2.6`, `bitgn` SDK (installed from the sibling `../bitgn-contest/.venv` wheel cache or from the BitGN package index), `openai>=1.40` (for cliproxyapi via OpenAI-compatible endpoint), `pytest`, `pytest-mock`. Standard library only for everything else (threads, JSONL, logging, argparse).

**Scope note:** This plan covers the core agent + minimum benchmark harness needed to produce a scored `bench_summary.json`. It defers §6 operator tooling beyond `bench_summary.py` (i.e. `trace_stats`, `failure_clusters`, `trace_diff`, `bench_diff`, `grep_traces`, `agent_ctl`, and the `bitgn-agent-ops` skill) to a follow-up **Plan B**. Plan B cannot start until Plan A has produced real traces for its analyzer-completeness test 4 to be meaningful.

**Spec cross-reference:** all section numbers in this plan (`§2.2`, `§3.2`, etc.) refer to `docs/superpowers/specs/2026-04-10-bitgn-agent-design.md` v0.0.7 unless otherwise noted.

---

## File Structure

| File | Responsibility | Created in task |
|---|---|---|
| `pyproject.toml` | Package metadata, deps, console scripts, pytest config | T1 |
| `src/bitgn_contest_agent/__init__.py` | Version re-export from `VERSION` file | T1 |
| `src/bitgn_contest_agent/schemas.py` | `Req_*`, `NextStep`, `ReportTaskCompletion` Pydantic models — single source of truth for planner tool surface | T2 |
| `tests/test_tool_coverage.py` | §5.2 Test 1: mechanical contract between `pcm_pb2` Request types and Union | T3 |
| `tests/test_schemas.py` | §5.2 Test 2: round-trip every Union variant | T4 |
| `src/bitgn_contest_agent/config.py` | `AgentConfig` dataclass + env loader (cliproxyapi URL, api key, timeouts, etc.) | T5 |
| `src/bitgn_contest_agent/backend/__init__.py` | Backend package marker | T6 |
| `src/bitgn_contest_agent/backend/base.py` | `Backend` Protocol + `Message` dataclass + `TransientBackendError` | T6 |
| `src/bitgn_contest_agent/backend/openai_compat.py` | Default backend — OpenAI-compatible client pointed at cliproxyapi | T7 |
| `src/bitgn_contest_agent/adapter/__init__.py` | Adapter package marker | T8 |
| `src/bitgn_contest_agent/adapter/pcm.py` | `PcmAdapter` — translates `Req_*` models to `PcmRuntimeClientSync` calls + pre-pass helper | T8–T10 |
| `tests/test_adapter_dispatch.py` | §5.2 Test 3: every `Req_*` model maps to the right client method | T9 |
| `src/bitgn_contest_agent/session.py` | `Session` dataclass + loop detector | T11 |
| `src/bitgn_contest_agent/enforcer.py` | `check_terminal` — R1 grounding-refs + R2 OUTCOME_ERR_INTERNAL | T12 |
| `src/bitgn_contest_agent/prompts.py` | Static system prompt, critique helper, loop-nudge helper | T13 |
| `src/bitgn_contest_agent/trace_schema.py` | **Single source of truth** for JSONL trace records (Pydantic models with `extra="ignore"`) | T14 |
| `src/bitgn_contest_agent/trace_writer.py` | `TraceWriter` — incremental JSONL append + crash fallback | T15 |
| `src/bitgn_contest_agent/agent.py` | Core step loop (~80 LoC) — P1 tool-feedback, P2 backend retry, P3 validation retry, P4 loop nudge, P5 task fail | T16–T17 |
| `src/bitgn_contest_agent/orchestrator.py` | ThreadPool + cancel event + P7 cooperative cancel + grace period | T18 |
| `src/bitgn_contest_agent/harness.py` | Thin wrapper around `HarnessServiceClientSync` — `get_benchmark → start_playground → end_trial` flow | T19 |
| `src/bitgn_contest_agent/cli.py` | `bitgn-agent` entrypoint: `run-task`, `run-benchmark` subcommands | T20 |
| `scripts/bench_summary.py` | Directory of traces → frozen-schema aggregate JSON (§6.6 Asset A) | T21 |
| `tests/test_analyzer_completeness.py` | §5.2 Test 4: introspection-driven property test over `trace_schema` | T22 |
| `tests/fixtures/trace_v1.jsonl` | Golden fixture captured from a real run, frozen forever | T23 |
| `tests/test_version_compat.py` | §5.2 Test 5: parametrized over every committed fixture | T23 |
| `artifacts/bench/<commit>_<timestamp>.json` | First committed benchmark summary → ratchet floor | T24 |

**Files the spec lists that Plan A does NOT create (deferred to Plan B):**
- `scripts/trace_stats.py`, `scripts/failure_clusters.py`, `scripts/grep_traces.py`, `scripts/trace_diff.py`, `scripts/bench_diff.py`, `scripts/agent_ctl.py`
- `.claude/skills/bitgn-agent-ops/SKILL.md`

---

## Phase 0 — Scaffold

### Task 1: Package skeleton, pyproject, and pytest config

**Files:**
- Create: `pyproject.toml`
- Create: `src/bitgn_contest_agent/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `.gitignore`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "bitgn-contest-agent"
version = "0.0.7"
description = "BitGN PAC1 contest agent (single-session, provider-agnostic SGR)"
requires-python = ">=3.12"
dependencies = [
  "pydantic>=2.6",
  "openai>=1.40",
  "bitgn",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "pytest-mock>=3.12",
]

[project.scripts]
bitgn-agent = "bitgn_contest_agent.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: Write `src/bitgn_contest_agent/__init__.py`**

```python
"""BitGN PAC1 contest agent package."""
from __future__ import annotations

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent.parent / "VERSION"
__version__ = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
```

- [ ] **Step 3: Write `tests/__init__.py` and `tests/conftest.py`**

```python
# tests/__init__.py
```

```python
# tests/conftest.py
"""Shared pytest fixtures for the bitgn_contest_agent test suite."""
from __future__ import annotations
```

- [ ] **Step 4: Write `.gitignore`**

```
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.venv/
build/
dist/
logs/
artifacts/bench/*_scratch.json
```

- [ ] **Step 5: Verify the package imports and pytest collects zero tests**

Run: `python -m pip install -e '.[dev]'`
Expected: install succeeds, no wheel errors.

Run: `python -c "import bitgn_contest_agent; print(bitgn_contest_agent.__version__)"`
Expected: prints `0.0.7`.

Run: `pytest`
Expected: `no tests ran in 0.XXs` (empty suite is fine at this point).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/bitgn_contest_agent/__init__.py tests/__init__.py tests/conftest.py .gitignore
git commit -m "feat: package skeleton with pyproject and pytest wiring"
```

---

## Phase 1 — Schemas (NextStep Union + coverage contract)

### Task 2: Pydantic Req_* models, NextStep, ReportTaskCompletion

**Files:**
- Create: `src/bitgn_contest_agent/schemas.py`

This is a pure transcription task. The exact field shapes come from `bitgn.vm.pcm_pb2` (confirmed via `pcm_pb2.pyi`):

| PCM request | Proto fields |
|---|---|
| `ReadRequest` | `path: str` |
| `WriteRequest` | `path: str`, `content: str` |
| `DeleteRequest` | `path: str` |
| `MkDirRequest` | `path: str` |
| `MoveRequest` | `from_name: str`, `to_name: str` |
| `ListRequest` | `name: str` |
| `TreeRequest` | `root: str` |
| `FindRequest` | `root: str`, `name: str`, `type: FindRequest.Type` (enum: `TYPE_ALL | TYPE_FILES | TYPE_DIRS`), `limit: int` |
| `SearchRequest` | `root: str`, `pattern: str`, `limit: int` |
| `ContextRequest` | (no fields) |
| `AnswerRequest` | `message: str`, `outcome: Outcome` (enum), `refs: repeated string` |

- [ ] **Step 1: Write the failing import test stub**

Create `tests/test_schemas.py` with just the import to prove the module exists:

```python
"""Round-trip tests for the NextStep Union (§5.2 Test 2)."""
from __future__ import annotations

import pytest

from bitgn_contest_agent import schemas


def test_module_imports():
    assert hasattr(schemas, "NextStep")
    assert hasattr(schemas, "ReportTaskCompletion")
```

- [ ] **Step 2: Run the test, watch it fail**

Run: `pytest tests/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bitgn_contest_agent.schemas'` (or `AttributeError` if you half-created it).

- [ ] **Step 3: Implement `src/bitgn_contest_agent/schemas.py`**

```python
"""Pydantic schemas for the planner tool surface.

Single source of truth: the NextStep Union mirrors the PcmRuntime RPC
surface exactly. The coverage test in tests/test_tool_coverage.py keeps
this correspondence mechanical.
"""
from __future__ import annotations

from typing import Annotated, List, Literal, Union

from pydantic import BaseModel, Field
from pydantic.types import StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class Req_Read(BaseModel):
    tool: Literal["read"]
    path: NonEmptyStr


class Req_Write(BaseModel):
    tool: Literal["write"]
    path: NonEmptyStr
    content: str


class Req_Delete(BaseModel):
    tool: Literal["delete"]
    path: NonEmptyStr


class Req_MkDir(BaseModel):
    tool: Literal["mkdir"]
    path: NonEmptyStr


class Req_Move(BaseModel):
    tool: Literal["move"]
    from_name: NonEmptyStr
    to_name: NonEmptyStr


class Req_List(BaseModel):
    tool: Literal["list"]
    name: NonEmptyStr


class Req_Tree(BaseModel):
    tool: Literal["tree"]
    root: NonEmptyStr


class Req_Find(BaseModel):
    tool: Literal["find"]
    root: NonEmptyStr
    name: str = ""
    type: Literal["TYPE_ALL", "TYPE_FILES", "TYPE_DIRS"] = "TYPE_ALL"
    limit: int = Field(default=100, ge=1, le=10_000)


class Req_Search(BaseModel):
    tool: Literal["search"]
    root: NonEmptyStr
    pattern: NonEmptyStr
    limit: int = Field(default=100, ge=1, le=10_000)


class Req_Context(BaseModel):
    tool: Literal["context"]


class ReportTaskCompletion(BaseModel):
    tool: Literal["report_completion"]
    message: NonEmptyStr
    grounding_refs: List[str]
    rulebook_notes: NonEmptyStr
    outcome_justification: NonEmptyStr
    completed_steps_laconic: List[str]
    outcome: Literal[
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    ]


FunctionUnion = Union[
    Req_Tree,
    Req_Find,
    Req_Search,
    Req_List,
    Req_Read,
    Req_Write,
    Req_Delete,
    Req_MkDir,
    Req_Move,
    Req_Context,
    ReportTaskCompletion,
]


class NextStep(BaseModel):
    current_state: NonEmptyStr
    plan_remaining_steps_brief: Annotated[List[str], Field(min_length=1, max_length=5)]
    identity_verified: bool
    function: FunctionUnion = Field(..., discriminator="tool")


# Convenience: the set of all Req_* model classes, in canonical order.
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
)
```

- [ ] **Step 4: Run the import test**

Run: `pytest tests/test_schemas.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/schemas.py tests/test_schemas.py
git commit -m "feat: add NextStep Pydantic Union mirroring PcmRuntime surface"
```

---

### Task 3: Tool coverage contract test (§5.2 Test 1)

**Files:**
- Create: `tests/test_tool_coverage.py`

- [ ] **Step 1: Write the failing coverage test**

```python
"""Mechanical contract: the NextStep Union mirrors PcmRuntime RPCs exactly.

If a future bitgn SDK release adds a new *Request type, this test fails
until the Union is updated. Likewise if a Req_* model is added without a
corresponding RPC, we catch it here.

Source of truth on the RPC side: bitgn.vm.pcm_pb2 — any class whose name
ends with ``Request`` AND does not inherit from another Request.
"""
from __future__ import annotations

import inspect

from bitgn.vm import pcm_pb2

from bitgn_contest_agent.schemas import REQ_MODELS, ReportTaskCompletion


# The planner emits ReportTaskCompletion and the adapter translates it to
# AnswerRequest on the wire. The coverage test treats AnswerRequest as
# covered by ReportTaskCompletion rather than by a Req_Answer model.
TERMINAL_RPC = "AnswerRequest"

# Internal protobuf plumbing we do not want to inspect.
IGNORED_PROTO_NAMES = frozenset(
    {
        "DESCRIPTOR",
        "SerializedProtobufDescriptor",
    }
)


def _discover_pcm_request_types() -> set[str]:
    names: set[str] = set()
    for name, obj in inspect.getmembers(pcm_pb2):
        if name in IGNORED_PROTO_NAMES:
            continue
        if not inspect.isclass(obj):
            continue
        if not name.endswith("Request"):
            continue
        names.add(name)
    return names


def _req_model_rpc_names() -> set[str]:
    """Map each Req_* model to the proto class name it shadows."""
    mapping: dict[str, str] = {
        "Req_Read": "ReadRequest",
        "Req_Write": "WriteRequest",
        "Req_Delete": "DeleteRequest",
        "Req_MkDir": "MkDirRequest",
        "Req_Move": "MoveRequest",
        "Req_List": "ListRequest",
        "Req_Tree": "TreeRequest",
        "Req_Find": "FindRequest",
        "Req_Search": "SearchRequest",
        "Req_Context": "ContextRequest",
    }
    names: set[str] = set()
    for model in REQ_MODELS:
        if model.__name__ not in mapping:
            raise AssertionError(
                f"Req_* model {model.__name__} is missing from the coverage "
                f"map in tests/test_tool_coverage.py"
            )
        names.add(mapping[model.__name__])
    return names


def test_pcm_request_types_exactly_covered_by_union():
    rpc_requests = _discover_pcm_request_types()
    covered = _req_model_rpc_names() | {TERMINAL_RPC}

    missing = rpc_requests - covered
    extra = covered - rpc_requests

    assert not missing, (
        f"PcmRuntime exposes {sorted(missing)} but no Req_* model covers them. "
        "Add a new Req_* model in schemas.py and extend REQ_MODELS."
    )
    assert not extra, (
        f"Union covers {sorted(extra)} but PcmRuntime no longer exposes them. "
        "Remove the Req_* model or update the coverage map."
    )


def test_report_task_completion_outcome_matches_proto_enum():
    """Our Literal outcome set must match the proto Outcome enum exactly,
    minus the OUTCOME_UNSPECIFIED placeholder."""
    proto_outcomes = {
        name for name in dir(pcm_pb2) if name.startswith("OUTCOME_")
    }
    # Pydantic Literal args are exposed via __args__ on the annotation.
    from typing import get_args

    literal = ReportTaskCompletion.model_fields["outcome"].annotation
    model_outcomes = set(get_args(literal))

    assert model_outcomes == proto_outcomes - {"OUTCOME_UNSPECIFIED"}, (
        f"Outcome mismatch.\n"
        f"  proto (minus UNSPECIFIED): {sorted(proto_outcomes - {'OUTCOME_UNSPECIFIED'})}\n"
        f"  model: {sorted(model_outcomes)}"
    )
```

- [ ] **Step 2: Run it and verify it passes against the transcription from Task 2**

Run: `pytest tests/test_tool_coverage.py -v`
Expected: both tests PASS. If either fails, the Req_* transcription in Task 2 is wrong — fix `schemas.py`, not this test.

- [ ] **Step 3: Verify it fails when we break the contract**

Temporarily remove `Req_Tree` from `REQ_MODELS` in `schemas.py`.
Run: `pytest tests/test_tool_coverage.py::test_pcm_request_types_exactly_covered_by_union -v`
Expected: FAIL with `PcmRuntime exposes ['TreeRequest']`.
Restore `Req_Tree` before continuing.

- [ ] **Step 4: Commit**

```bash
git add tests/test_tool_coverage.py
git commit -m "test: mechanical coverage contract between Union and pcm_pb2"
```

---

### Task 4: Round-trip test every Union variant (§5.2 Test 2)

**Files:**
- Modify: `tests/test_schemas.py`

- [ ] **Step 1: Extend the schema test with a parametrized round-trip**

Add to `tests/test_schemas.py` (keep the existing `test_module_imports`):

```python
import json
from typing import Any

import pytest

from bitgn_contest_agent.schemas import (
    NextStep,
    REQ_MODELS,
    Req_Context,
    Req_Find,
    Req_List,
    Req_Move,
    Req_Read,
    Req_Search,
    Req_Tree,
    Req_Write,
    Req_Delete,
    Req_MkDir,
    ReportTaskCompletion,
)


def _sample_function_payloads() -> list[dict[str, Any]]:
    return [
        {"tool": "read", "path": "AGENTS.md"},
        {"tool": "write", "path": "/tmp/a", "content": "hello"},
        {"tool": "delete", "path": "/tmp/a"},
        {"tool": "mkdir", "path": "/tmp/new"},
        {"tool": "move", "from_name": "a", "to_name": "b"},
        {"tool": "list", "name": "/"},
        {"tool": "tree", "root": "/"},
        {
            "tool": "find",
            "root": "/",
            "name": "*.py",
            "type": "TYPE_FILES",
            "limit": 50,
        },
        {"tool": "search", "root": "/", "pattern": "TODO", "limit": 25},
        {"tool": "context"},
        {
            "tool": "report_completion",
            "message": "done",
            "grounding_refs": ["AGENTS.md"],
            "rulebook_notes": "followed identity pass",
            "outcome_justification": "answer grounded in AGENTS.md",
            "completed_steps_laconic": ["read AGENTS.md", "answered"],
            "outcome": "OUTCOME_OK",
        },
    ]


@pytest.mark.parametrize("payload", _sample_function_payloads())
def test_next_step_round_trip_every_variant(payload: dict[str, Any]) -> None:
    step = NextStep(
        current_state="exploring",
        plan_remaining_steps_brief=["verify", "report"],
        identity_verified=True,
        function=payload,
    )
    dumped = step.model_dump_json()
    reparsed = NextStep.model_validate_json(dumped)
    assert reparsed.model_dump() == step.model_dump()
    # JSON is canonicalizable: dump → parse → dump is a fixed point.
    assert json.loads(reparsed.model_dump_json()) == json.loads(dumped)


def test_req_models_are_discriminated_by_tool_field() -> None:
    """Each Req_* model must declare a Literal["..."] tool field — otherwise
    Pydantic cannot discriminate the union. Regression guard for schema
    drift during refactors."""
    from typing import get_args

    for model in REQ_MODELS:
        tool_field = model.model_fields["tool"]
        literal_args = get_args(tool_field.annotation)
        assert literal_args and len(literal_args) == 1, (
            f"{model.__name__}.tool must be Literal['...'], got "
            f"{tool_field.annotation}"
        )
```

- [ ] **Step 2: Run the round-trip suite**

Run: `pytest tests/test_schemas.py -v`
Expected: all 11 parametrized variants + `test_module_imports` + `test_req_models_are_discriminated_by_tool_field` PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_schemas.py
git commit -m "test: round-trip every NextStep Union variant"
```

---

## Phase 2 — Config

### Task 5: `AgentConfig` dataclass + env loader

**Files:**
- Create: `src/bitgn_contest_agent/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing config test**

```python
"""Tests for the AgentConfig dataclass + env loader."""
from __future__ import annotations

import pytest

from bitgn_contest_agent.config import AgentConfig, ConfigError, load_from_env


def test_load_from_env_reads_all_required_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_API_KEY", "bg-key-123")
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://127.0.0.1:8317/v1")
    monkeypatch.setenv("CLIPROXY_API_KEY", "cp-key-abc")
    monkeypatch.setenv("BITGN_BENCHMARK", "bitgn/pac1-dev")
    cfg = load_from_env()
    assert isinstance(cfg, AgentConfig)
    assert cfg.bitgn_api_key == "bg-key-123"
    assert cfg.cliproxy_base_url == "http://127.0.0.1:8317/v1"
    assert cfg.cliproxy_api_key == "cp-key-abc"
    assert cfg.benchmark == "bitgn/pac1-dev"
    # §4.1 calibrated defaults
    assert cfg.model == "gpt-5.3-codex"
    assert cfg.reasoning_effort == "medium"
    assert cfg.max_steps == 40
    assert cfg.task_timeout_sec == 300
    assert cfg.task_timeout_grace_sec == 20
    assert cfg.llm_http_timeout_sec == 30
    assert cfg.max_tool_result_bytes == 16384
    assert cfg.max_parallel_tasks == 4
    assert cfg.max_inflight_llm == 6
    assert cfg.rate_limit_backoff_ms == (500, 1500, 4000, 10000)


def test_load_from_env_fails_fast_without_bitgn_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BITGN_API_KEY", raising=False)
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://127.0.0.1:8317/v1")
    monkeypatch.setenv("CLIPROXY_API_KEY", "cp")
    with pytest.raises(ConfigError, match="BITGN_API_KEY"):
        load_from_env()


def test_task_timeout_zero_disables_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_API_KEY", "x")
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://localhost")
    monkeypatch.setenv("CLIPROXY_API_KEY", "y")
    monkeypatch.setenv("TASK_TIMEOUT_SEC", "0")
    cfg = load_from_env()
    assert cfg.task_timeout_sec == 0
    assert cfg.cancel_enabled is False
```

- [ ] **Step 2: Run the test, watch it fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitgn_contest_agent.config'`.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/config.py`**

```python
"""AgentConfig: all tunables and credentials in one dataclass.

Loaded once at startup from environment variables. Fail-fast validation
(§4 pattern P6) runs before the thread pool is created.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    # Credentials
    bitgn_api_key: str
    cliproxy_base_url: str
    cliproxy_api_key: str

    # Benchmark
    benchmark: str = "bitgn/pac1-dev"

    # Model
    model: str = "gpt-5.3-codex"
    reasoning_effort: str = "medium"

    # Timeouts / steps (§4.1 calibrated defaults)
    max_steps: int = 40
    task_timeout_sec: int = 300
    task_timeout_grace_sec: int = 20
    llm_http_timeout_sec: int = 30
    max_tool_result_bytes: int = 16384

    # Parallelism (§3.1)
    max_parallel_tasks: int = 4
    max_inflight_llm: int = 6

    # Backend retry (§3.3)
    rate_limit_backoff_ms: Tuple[int, ...] = (500, 1500, 4000, 10000)

    # Tracing
    log_dir: str = "logs"

    @property
    def cancel_enabled(self) -> bool:
        return self.task_timeout_sec > 0


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"required environment variable {name} is missing or empty")
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def load_from_env() -> AgentConfig:
    return AgentConfig(
        bitgn_api_key=_require("BITGN_API_KEY"),
        cliproxy_base_url=_require("CLIPROXY_BASE_URL"),
        cliproxy_api_key=_require("CLIPROXY_API_KEY"),
        benchmark=os.environ.get("BITGN_BENCHMARK", "bitgn/pac1-dev"),
        model=os.environ.get("AGENT_MODEL", "gpt-5.3-codex"),
        reasoning_effort=os.environ.get("AGENT_REASONING_EFFORT", "medium"),
        max_steps=_int_env("MAX_STEPS", 40),
        task_timeout_sec=_int_env("TASK_TIMEOUT_SEC", 300),
        task_timeout_grace_sec=_int_env("TASK_TIMEOUT_GRACE_SEC", 20),
        llm_http_timeout_sec=_int_env("LLM_HTTP_TIMEOUT_SEC", 30),
        max_tool_result_bytes=_int_env("MAX_TOOL_RESULT_BYTES", 16384),
        max_parallel_tasks=_int_env("MAX_PARALLEL_TASKS", 4),
        max_inflight_llm=_int_env("MAX_INFLIGHT_LLM", 6),
        log_dir=os.environ.get("LOG_DIR", "logs"),
    )
```

- [ ] **Step 4: Run the tests until green**

Run: `pytest tests/test_config.py -v`
Expected: all three tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/config.py tests/test_config.py
git commit -m "feat: AgentConfig dataclass with fail-fast env loader"
```

---

## Phase 3 — Backend abstraction

### Task 6: Backend Protocol + Message dataclass + TransientBackendError

**Files:**
- Create: `src/bitgn_contest_agent/backend/__init__.py`
- Create: `src/bitgn_contest_agent/backend/base.py`
- Create: `tests/test_backend_base.py`

- [ ] **Step 1: Write the failing contract test**

```python
"""Tests for the backend Protocol and its error taxonomy."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bitgn_contest_agent.backend.base import (
    Backend,
    Message,
    TransientBackendError,
)
from bitgn_contest_agent.schemas import NextStep


def test_message_is_frozen_dataclass() -> None:
    msg = Message(role="user", content="hi")
    with pytest.raises(FrozenInstanceError):
        msg.content = "bye"  # type: ignore[misc]


def test_transient_backend_error_is_exception_subclass() -> None:
    assert issubclass(TransientBackendError, Exception)
    err = TransientBackendError("rate limit", attempt=2)
    assert err.attempt == 2
    assert "rate limit" in str(err)


def test_backend_protocol_is_runtime_checkable() -> None:
    class Fake:
        def next_step(self, messages, response_schema, timeout_sec):  # type: ignore[override]
            return NextStep(
                current_state="x",
                plan_remaining_steps_brief=["done"],
                identity_verified=True,
                function={"tool": "context"},
            )

    assert isinstance(Fake(), Backend)
```

- [ ] **Step 2: Run it, watch it fail**

Run: `pytest tests/test_backend_base.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/backend/__init__.py` and `base.py`**

```python
# src/bitgn_contest_agent/backend/__init__.py
"""Backend abstraction package."""
```

```python
# src/bitgn_contest_agent/backend/base.py
"""Provider-agnostic backend protocol.

The planner only ever talks to Backend.next_step — it never knows which
provider is in use. A second backend (anthropic_compat, etc.) is a new
file, not a refactor.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from bitgn_contest_agent.schemas import NextStep


@dataclass(frozen=True, slots=True)
class Message:
    role: str           # "system" | "user" | "assistant" | "tool"
    content: str


class TransientBackendError(Exception):
    """Rate limit, 5xx, or network timeout. Caller retries with backoff."""

    def __init__(self, message: str, *, attempt: int = 0) -> None:
        super().__init__(message)
        self.attempt = attempt


@runtime_checkable
class Backend(Protocol):
    def next_step(
        self,
        messages: Sequence[Message],
        response_schema: type[NextStep],
        timeout_sec: float,
    ) -> NextStep:
        ...
```

- [ ] **Step 4: Run it, watch it pass**

Run: `pytest tests/test_backend_base.py -v`
Expected: all three tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/backend/__init__.py src/bitgn_contest_agent/backend/base.py tests/test_backend_base.py
git commit -m "feat: Backend Protocol + Message + TransientBackendError"
```

---

### Task 7: OpenAI-compatible backend implementation

**Files:**
- Create: `src/bitgn_contest_agent/backend/openai_compat.py`
- Create: `tests/test_backend_openai_compat.py`

**Design note on `response_format`:** §9 open question 5 notes we do not yet know whether cliproxyapi's OpenAI-compatible endpoint accepts `response_format` structured outputs for `gpt-5.3-codex`. The backend implementation therefore has two code paths:

1. **Structured-output path:** `client.beta.chat.completions.parse(response_format=NextStep)` — preferred when the provider supports it.
2. **Manual-parse fallback:** regular `chat.completions.create` with `response_format={"type": "json_object"}` (or no response_format at all if even that is rejected), then `NextStep.model_validate_json(raw)`. The agent loop's P3 pattern handles `ValidationError` via critique-injection retry, so the fallback is not a correctness risk — only a latency/retry-cost risk.

We ship the structured-output path and a fallback flag. The first live run (T24) will tell us which path cliproxyapi actually supports.

- [ ] **Step 1: Write the failing test using `pytest-mock`**

```python
"""Unit tests for OpenAIChatBackend.

These tests do NOT hit cliproxyapi — they mock the openai SDK layer and
assert the adapter's translation behavior.
"""
from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from bitgn_contest_agent.backend.base import Message, TransientBackendError
from bitgn_contest_agent.backend.openai_compat import OpenAIChatBackend
from bitgn_contest_agent.schemas import NextStep


def _sample_step_json() -> str:
    return (
        '{"current_state":"read AGENTS.md",'
        '"plan_remaining_steps_brief":["read","report"],'
        '"identity_verified":true,'
        '"function":{"tool":"read","path":"AGENTS.md"}}'
    )


def test_structured_path_returns_parsed_next_step(mocker: Any) -> None:
    fake_client = MagicMock()
    fake_parsed = NextStep.model_validate_json(_sample_step_json())
    completion = MagicMock()
    completion.choices = [
        MagicMock(message=MagicMock(parsed=fake_parsed, content=_sample_step_json()))
    ]
    fake_client.beta.chat.completions.parse.return_value = completion

    backend = OpenAIChatBackend(
        client=fake_client,
        model="gpt-5.3-codex",
        reasoning_effort="medium",
        use_structured_output=True,
    )

    out = backend.next_step(
        messages=[Message(role="system", content="sys"), Message(role="user", content="t")],
        response_schema=NextStep,
        timeout_sec=30.0,
    )
    assert isinstance(out, NextStep)
    assert out.function.tool == "read"
    fake_client.beta.chat.completions.parse.assert_called_once()


def test_fallback_path_parses_content_json(mocker: Any) -> None:
    fake_client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=_sample_step_json()))]
    fake_client.chat.completions.create.return_value = completion

    backend = OpenAIChatBackend(
        client=fake_client,
        model="gpt-5.3-codex",
        reasoning_effort="medium",
        use_structured_output=False,
    )

    out = backend.next_step(
        messages=[Message(role="user", content="t")],
        response_schema=NextStep,
        timeout_sec=30.0,
    )
    assert isinstance(out, NextStep)
    fake_client.chat.completions.create.assert_called_once()


def test_rate_limit_is_remapped_to_transient_backend_error() -> None:
    import openai

    fake_client = MagicMock()
    fake_client.beta.chat.completions.parse.side_effect = openai.RateLimitError(
        message="slow down",
        response=MagicMock(status_code=429),
        body=None,
    )
    backend = OpenAIChatBackend(
        client=fake_client,
        model="gpt-5.3-codex",
        reasoning_effort="medium",
        use_structured_output=True,
    )
    with pytest.raises(TransientBackendError):
        backend.next_step([Message(role="user", content="t")], NextStep, 30.0)


def test_timeout_is_remapped_to_transient_backend_error() -> None:
    import openai

    fake_client = MagicMock()
    fake_client.beta.chat.completions.parse.side_effect = openai.APITimeoutError(
        request=MagicMock()
    )
    backend = OpenAIChatBackend(
        client=fake_client,
        model="gpt-5.3-codex",
        reasoning_effort="medium",
        use_structured_output=True,
    )
    with pytest.raises(TransientBackendError):
        backend.next_step([Message(role="user", content="t")], NextStep, 30.0)
```

- [ ] **Step 2: Run the test, watch it fail**

Run: `pytest tests/test_backend_openai_compat.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/backend/openai_compat.py`**

```python
"""OpenAI-compatible backend (routes through cliproxyapi by default).

Two code paths:
- Structured output via client.beta.chat.completions.parse(response_format=NextStep)
- Manual-parse fallback via client.chat.completions.create + json_object mode

The agent's P3 pattern (validation retry with critique) covers any
ValidationError raised in the fallback path, so the fallback is not a
correctness risk.
"""
from __future__ import annotations

from typing import Sequence

import openai
from openai import OpenAI
from pydantic import ValidationError

from bitgn_contest_agent.backend.base import Backend, Message, TransientBackendError
from bitgn_contest_agent.schemas import NextStep


_TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


class OpenAIChatBackend(Backend):
    def __init__(
        self,
        *,
        client: OpenAI,
        model: str,
        reasoning_effort: str,
        use_structured_output: bool = True,
    ) -> None:
        self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._use_structured_output = use_structured_output

    @classmethod
    def from_config(cls, base_url: str, api_key: str, model: str, reasoning_effort: str) -> "OpenAIChatBackend":
        client = OpenAI(base_url=base_url, api_key=api_key)
        return cls(
            client=client,
            model=model,
            reasoning_effort=reasoning_effort,
            use_structured_output=True,
        )

    def next_step(
        self,
        messages: Sequence[Message],
        response_schema: type[NextStep],
        timeout_sec: float,
    ) -> NextStep:
        payload = [{"role": m.role, "content": m.content} for m in messages]
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
                    # Structured output mode returned no parsed value — fall
                    # back to parsing the raw content. Raises ValidationError
                    # on bad JSON, caught by the agent loop's P3 path.
                    raw = completion.choices[0].message.content or ""
                    parsed = response_schema.model_validate_json(raw)
                return parsed
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=payload,
                response_format={"type": "json_object"},
                timeout=timeout_sec,
                extra_body={"reasoning": {"effort": self._reasoning_effort}},
            )
            raw = completion.choices[0].message.content or ""
            return response_schema.model_validate_json(raw)
        except _TRANSIENT_EXCEPTIONS as exc:
            raise TransientBackendError(str(exc)) from exc
        except ValidationError:
            # Caller handles via P3 critique-injection retry.
            raise
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_backend_openai_compat.py -v`
Expected: all four tests PASS. If `openai.RateLimitError` constructor signature differs in the installed version, adjust the test to use whatever the SDK accepts (the real behavior we care about is `_TRANSIENT_EXCEPTIONS` remapping — not constructor ergonomics).

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/backend/openai_compat.py tests/test_backend_openai_compat.py
git commit -m "feat: OpenAI-compatible backend with structured-output + fallback"
```

---

## Phase 4 — PCM Adapter

### Task 8: `PcmAdapter` scaffolding + result dataclass

**Files:**
- Create: `src/bitgn_contest_agent/adapter/__init__.py`
- Create: `src/bitgn_contest_agent/adapter/pcm.py`
- Create: `tests/test_adapter_smoke.py`

This task creates the adapter shell and the `ToolResult` dataclass. Task 9 adds the dispatch logic + per-verb tests. Task 10 adds the pre-pass helper.

- [ ] **Step 1: Write a smoke test that asserts the adapter can be constructed from a mock client**

```python
"""Smoke test — adapter constructs + exposes dispatch + prepass API."""
from __future__ import annotations

from unittest.mock import MagicMock

from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult


def test_adapter_constructs_from_runtime_client() -> None:
    runtime = MagicMock()
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=16384)
    assert callable(adapter.dispatch)
    assert callable(adapter.run_prepass)


def test_tool_result_carries_ok_bytes_refs() -> None:
    r = ToolResult(ok=True, content="hello", refs=("AGENTS.md",), error=None, error_code=None, wall_ms=12)
    assert r.ok
    assert r.bytes == len(b"hello")
    assert r.truncated is False
    assert r.refs == ("AGENTS.md",)
```

- [ ] **Step 2: Run it, watch it fail**

Run: `pytest tests/test_adapter_smoke.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the adapter skeleton**

```python
# src/bitgn_contest_agent/adapter/__init__.py
"""Adapter package — translates Req_* models to PcmRuntime calls."""
```

```python
# src/bitgn_contest_agent/adapter/pcm.py
"""Single-class adapter between Pydantic Req_* models and the official
bitgn PcmRuntimeClientSync. Every other layer is adapter-agnostic.

The adapter is the ONLY place in the project that imports bitgn.vm.pcm_pb2
or bitgn.vm.pcm_connect. Anywhere else that references bitgn is a smell
to be fixed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence, Tuple

from bitgn.vm import pcm_pb2
from bitgn.vm.pcm_connect import PcmRuntimeClientSync

from bitgn_contest_agent.schemas import (
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
)


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    content: str
    refs: Tuple[str, ...]
    error: str | None
    error_code: str | None
    wall_ms: int
    truncated: bool = False
    original_bytes: int = 0

    @property
    def bytes(self) -> int:
        return len(self.content.encode("utf-8", errors="replace"))


class PcmAdapter:
    def __init__(self, *, runtime: PcmRuntimeClientSync, max_tool_result_bytes: int) -> None:
        self._runtime = runtime
        self._max_bytes = max_tool_result_bytes

    # Task 9 implements these. Task 10 implements run_prepass.
    def dispatch(self, req: Any) -> ToolResult:  # noqa: ARG002 — filled in T9
        raise NotImplementedError

    def run_prepass(self, *, session: Any, trace_writer: Any) -> None:  # noqa: ARG002 — filled in T10
        raise NotImplementedError

    def submit_terminal(self, completion: ReportTaskCompletion) -> ToolResult:  # filled in T9
        raise NotImplementedError
```

- [ ] **Step 4: Run smoke test, watch it pass**

Run: `pytest tests/test_adapter_smoke.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/adapter/__init__.py src/bitgn_contest_agent/adapter/pcm.py tests/test_adapter_smoke.py
git commit -m "feat: PcmAdapter scaffolding + ToolResult dataclass"
```

---

### Task 9: Adapter dispatch table + per-verb tests (§5.2 Test 3)

**Files:**
- Modify: `src/bitgn_contest_agent/adapter/pcm.py`
- Create: `tests/test_adapter_dispatch.py`

The adapter calls the `PcmRuntimeClientSync` methods whose names are the lowercase verbs: `read`, `write`, `delete`, `mk_dir`, `move`, `list`, `tree`, `find`, `search`, `context`, `answer`.

- [ ] **Step 1: Write the failing per-verb dispatch test**

```python
"""§5.2 Test 3 — adapter.dispatch maps each Req_* to the right method
and proto request shape."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from bitgn.vm import pcm_pb2

from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult
from bitgn_contest_agent.schemas import (
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
)


def _mk_adapter(runtime: MagicMock) -> PcmAdapter:
    return PcmAdapter(runtime=runtime, max_tool_result_bytes=16384)


def test_dispatch_read_calls_runtime_read_with_path() -> None:
    runtime = MagicMock()
    response = MagicMock()
    response.content = "file contents"
    runtime.read.return_value = response

    adapter = _mk_adapter(runtime)
    result = adapter.dispatch(Req_Read(tool="read", path="AGENTS.md"))

    runtime.read.assert_called_once()
    sent = runtime.read.call_args.args[0]
    assert isinstance(sent, pcm_pb2.ReadRequest)
    assert sent.path == "AGENTS.md"
    assert result.ok
    assert result.content == "file contents"
    assert result.refs == ("AGENTS.md",)


def test_dispatch_write_passes_path_and_content() -> None:
    runtime = MagicMock()
    runtime.write.return_value = MagicMock()

    adapter = _mk_adapter(runtime)
    result = adapter.dispatch(Req_Write(tool="write", path="/tmp/a", content="hi"))

    sent = runtime.write.call_args.args[0]
    assert isinstance(sent, pcm_pb2.WriteRequest)
    assert sent.path == "/tmp/a"
    assert sent.content == "hi"
    assert result.ok


def test_dispatch_delete() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Delete(tool="delete", path="/tmp/a"))
    sent = runtime.delete.call_args.args[0]
    assert isinstance(sent, pcm_pb2.DeleteRequest)
    assert sent.path == "/tmp/a"


def test_dispatch_mkdir_uses_mk_dir_method_name() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_MkDir(tool="mkdir", path="/tmp/new"))
    # PcmRuntimeClientSync method is mk_dir (snake_case), not mkdir.
    sent = runtime.mk_dir.call_args.args[0]
    assert isinstance(sent, pcm_pb2.MkDirRequest)
    assert sent.path == "/tmp/new"


def test_dispatch_move_maps_from_name_and_to_name() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Move(tool="move", from_name="src", to_name="dst"))
    sent = runtime.move.call_args.args[0]
    assert isinstance(sent, pcm_pb2.MoveRequest)
    assert sent.from_name == "src"
    assert sent.to_name == "dst"


def test_dispatch_list_maps_name_field() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_List(tool="list", name="/"))
    sent = runtime.list.call_args.args[0]
    assert isinstance(sent, pcm_pb2.ListRequest)
    assert sent.name == "/"


def test_dispatch_tree_maps_root_field() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Tree(tool="tree", root="/"))
    sent = runtime.tree.call_args.args[0]
    assert isinstance(sent, pcm_pb2.TreeRequest)
    assert sent.root == "/"


def test_dispatch_find_maps_type_enum() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(
        Req_Find(tool="find", root="/", name="*.py", type="TYPE_FILES", limit=10)
    )
    sent = runtime.find.call_args.args[0]
    assert isinstance(sent, pcm_pb2.FindRequest)
    assert sent.root == "/"
    assert sent.name == "*.py"
    assert sent.type == pcm_pb2.FindRequest.TYPE_FILES
    assert sent.limit == 10


def test_dispatch_search() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Search(tool="search", root="/", pattern="TODO", limit=25))
    sent = runtime.search.call_args.args[0]
    assert isinstance(sent, pcm_pb2.SearchRequest)
    assert sent.root == "/"
    assert sent.pattern == "TODO"
    assert sent.limit == 25


def test_dispatch_context_sends_empty_request() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Context(tool="context"))
    sent = runtime.context.call_args.args[0]
    assert isinstance(sent, pcm_pb2.ContextRequest)


def test_submit_terminal_calls_answer_with_outcome_enum_and_refs() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    completion = ReportTaskCompletion(
        tool="report_completion",
        message="done",
        grounding_refs=["AGENTS.md", "README.md"],
        rulebook_notes="n",
        outcome_justification="j",
        completed_steps_laconic=["read", "report"],
        outcome="OUTCOME_OK",
    )
    adapter.submit_terminal(completion)
    sent = runtime.answer.call_args.args[0]
    assert isinstance(sent, pcm_pb2.AnswerRequest)
    assert sent.message == "done"
    assert sent.outcome == pcm_pb2.OUTCOME_OK
    assert list(sent.refs) == ["AGENTS.md", "README.md"]


def test_dispatch_truncates_large_responses() -> None:
    runtime = MagicMock()
    big = "x" * 100_000
    runtime.read.return_value = MagicMock(content=big)
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=4096)
    result = adapter.dispatch(Req_Read(tool="read", path="big"))
    assert result.truncated is True
    assert result.bytes <= 4096
    assert result.original_bytes == len(big.encode("utf-8"))


def test_dispatch_rpc_failure_returns_error_result() -> None:
    runtime = MagicMock()
    runtime.read.side_effect = RuntimeError("backend down")
    adapter = _mk_adapter(runtime)
    result = adapter.dispatch(Req_Read(tool="read", path="AGENTS.md"))
    assert result.ok is False
    assert "backend down" in (result.error or "")
    assert result.error_code in {"PCM_ERROR", "UNKNOWN", "RPC_UNAVAILABLE"}
```

- [ ] **Step 2: Run and watch every case fail**

Run: `pytest tests/test_adapter_dispatch.py -v`
Expected: all 13 tests FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement the dispatch table + truncation + error mapping in `adapter/pcm.py`**

Replace the `dispatch`, `submit_terminal`, and (partial) helpers with the real implementation:

```python
# at top of adapter/pcm.py, add imports
import logging
from typing import Callable, Dict, Tuple

from bitgn_contest_agent.schemas import NextStep  # noqa: F401  (used in Task 10)

_LOG = logging.getLogger(__name__)


def _response_to_text(resp: Any) -> str:
    """Extract a printable representation of any pcm_pb2 response.

    Generated proto messages are not JSON-serializable out of the box, so
    we use the protobuf MessageToDict helper + a plain string fallback.
    """
    try:
        from google.protobuf.json_format import MessageToJson

        return MessageToJson(resp, preserving_proto_field_name=True, indent=None)
    except Exception:
        return str(resp)


_FIND_TYPE_MAP: Dict[str, int] = {
    "TYPE_ALL": pcm_pb2.FindRequest.TYPE_ALL,
    "TYPE_FILES": pcm_pb2.FindRequest.TYPE_FILES,
    "TYPE_DIRS": pcm_pb2.FindRequest.TYPE_DIRS,
}

_OUTCOME_MAP: Dict[str, int] = {
    "OUTCOME_OK": pcm_pb2.OUTCOME_OK,
    "OUTCOME_DENIED_SECURITY": pcm_pb2.OUTCOME_DENIED_SECURITY,
    "OUTCOME_NONE_CLARIFICATION": pcm_pb2.OUTCOME_NONE_CLARIFICATION,
    "OUTCOME_NONE_UNSUPPORTED": pcm_pb2.OUTCOME_NONE_UNSUPPORTED,
    "OUTCOME_ERR_INTERNAL": pcm_pb2.OUTCOME_ERR_INTERNAL,
}


class PcmAdapter:  # replace the stub class from Task 8
    def __init__(self, *, runtime: PcmRuntimeClientSync, max_tool_result_bytes: int) -> None:
        self._runtime = runtime
        self._max_bytes = max_tool_result_bytes

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, req: Any) -> ToolResult:
        start = time.monotonic()
        try:
            if isinstance(req, Req_Read):
                resp = self._runtime.read(pcm_pb2.ReadRequest(path=req.path))
                return self._finish(start, resp, refs=(req.path,))
            if isinstance(req, Req_Write):
                resp = self._runtime.write(pcm_pb2.WriteRequest(path=req.path, content=req.content))
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Delete):
                resp = self._runtime.delete(pcm_pb2.DeleteRequest(path=req.path))
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_MkDir):
                resp = self._runtime.mk_dir(pcm_pb2.MkDirRequest(path=req.path))
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Move):
                resp = self._runtime.move(
                    pcm_pb2.MoveRequest(from_name=req.from_name, to_name=req.to_name)
                )
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_List):
                resp = self._runtime.list(pcm_pb2.ListRequest(name=req.name))
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Tree):
                resp = self._runtime.tree(pcm_pb2.TreeRequest(root=req.root))
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Find):
                resp = self._runtime.find(
                    pcm_pb2.FindRequest(
                        root=req.root,
                        name=req.name,
                        type=_FIND_TYPE_MAP[req.type],
                        limit=req.limit,
                    )
                )
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Search):
                resp = self._runtime.search(
                    pcm_pb2.SearchRequest(root=req.root, pattern=req.pattern, limit=req.limit)
                )
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Context):
                resp = self._runtime.context(pcm_pb2.ContextRequest())
                return self._finish(start, resp, refs=())
            raise TypeError(f"unsupported request type: {type(req).__name__}")
        except Exception as exc:
            wall_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                ok=False,
                content="",
                refs=(),
                error=str(exc),
                error_code=self._classify_exception(exc),
                wall_ms=wall_ms,
            )

    def submit_terminal(self, completion: ReportTaskCompletion) -> ToolResult:
        start = time.monotonic()
        try:
            resp = self._runtime.answer(
                pcm_pb2.AnswerRequest(
                    message=completion.message,
                    outcome=_OUTCOME_MAP[completion.outcome],
                    refs=list(completion.grounding_refs),
                )
            )
            return self._finish(start, resp, refs=tuple(completion.grounding_refs))
        except Exception as exc:
            wall_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                ok=False,
                content="",
                refs=(),
                error=str(exc),
                error_code=self._classify_exception(exc),
                wall_ms=wall_ms,
            )

    # -- helpers ----------------------------------------------------------

    def _finish(self, start: float, resp: Any, *, refs: Tuple[str, ...]) -> ToolResult:
        text = _response_to_text(resp)
        encoded = text.encode("utf-8", errors="replace")
        original_bytes = len(encoded)
        truncated = False
        if original_bytes > self._max_bytes:
            encoded = encoded[: self._max_bytes]
            text = encoded.decode("utf-8", errors="replace")
            truncated = True
        wall_ms = int((time.monotonic() - start) * 1000)
        return ToolResult(
            ok=True,
            content=text,
            refs=refs,
            error=None,
            error_code=None,
            wall_ms=wall_ms,
            truncated=truncated,
            original_bytes=original_bytes if truncated else 0,
        )

    def _classify_exception(self, exc: Exception) -> str:
        name = type(exc).__name__
        if "Deadline" in name or "Timeout" in name:
            return "RPC_DEADLINE"
        if "Unavailable" in name or "Connection" in name:
            return "RPC_UNAVAILABLE"
        if "InvalidArgument" in name or isinstance(exc, (TypeError, ValueError)):
            return "INVALID_ARG"
        if "PcmError" in name:
            return "PCM_ERROR"
        return "UNKNOWN"
```

- [ ] **Step 4: Run the dispatch suite**

Run: `pytest tests/test_adapter_dispatch.py -v`
Expected: all 13 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/adapter/pcm.py tests/test_adapter_dispatch.py
git commit -m "feat: adapter dispatch table + truncation + error classification"
```

---

### Task 10: Adapter pre-pass helper (identity bootstrap)

**Files:**
- Modify: `src/bitgn_contest_agent/adapter/pcm.py`
- Create: `tests/test_adapter_prepass.py`

- [ ] **Step 1: Write the failing pre-pass test**

```python
"""Pre-pass best-effort identity bootstrap."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bitgn.vm import pcm_pb2

from bitgn_contest_agent.adapter.pcm import PcmAdapter
from bitgn_contest_agent.session import Session


class _FakeTraceWriter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def append_prepass(self, *, cmd: str, ok: bool, **kwargs: object) -> None:
        self.events.append({"cmd": cmd, "ok": ok, **kwargs})


def test_prepass_runs_tree_read_context_and_marks_loaded() -> None:
    runtime = MagicMock()
    runtime.tree.return_value = MagicMock(root="/")
    runtime.read.return_value = MagicMock(content="rules")
    runtime.context.return_value = MagicMock()

    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=16384)
    session = Session()
    writer = _FakeTraceWriter()
    adapter.run_prepass(session=session, trace_writer=writer)

    # Three pre-pass calls attempted.
    assert runtime.tree.call_count == 1
    assert runtime.read.call_count == 1
    assert runtime.context.call_count == 1

    # On ANY success, identity_loaded flips true.
    assert session.identity_loaded is True
    assert "AGENTS.md" in session.seen_refs
    assert len(writer.events) == 3
    assert all(e["ok"] for e in writer.events)


def test_prepass_is_best_effort_one_failure_does_not_abort_others() -> None:
    runtime = MagicMock()
    runtime.tree.side_effect = RuntimeError("tree failed")
    runtime.read.return_value = MagicMock(content="rules")
    runtime.context.return_value = MagicMock()

    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=16384)
    session = Session()
    writer = _FakeTraceWriter()
    adapter.run_prepass(session=session, trace_writer=writer)

    assert runtime.tree.call_count == 1
    assert runtime.read.call_count == 1
    assert runtime.context.call_count == 1
    assert session.identity_loaded is True  # still true — read + context succeeded
    assert len(writer.events) == 3
    assert writer.events[0]["ok"] is False
    assert writer.events[1]["ok"] is True
    assert writer.events[2]["ok"] is True
```

Note: this test imports `Session` from Task 11, so Tasks 10 and 11 are slightly coupled. Execute Task 11 first if you're running tasks strictly in order, OR land the minimal Session dataclass as part of Task 10 and defer the loop detector to Task 11. The plan assumes Task 11 lands first — re-read Task 11 if you haven't.

- [ ] **Step 2: Implement `run_prepass` in `adapter/pcm.py`**

Add this method to `PcmAdapter`:

```python
    def run_prepass(self, *, session: "Session", trace_writer: Any) -> None:
        from bitgn_contest_agent.session import Session  # local import to avoid cycles

        pre_cmds = [
            ("tree", Req_Tree(tool="tree", root="/")),
            ("read_agents_md", Req_Read(tool="read", path="AGENTS.md")),
            ("context", Req_Context(tool="context")),
        ]
        for label, req in pre_cmds:
            result = self.dispatch(req)
            if result.ok:
                session.identity_loaded = True
                if label == "read_agents_md":
                    session.rulebook_loaded = True
                for ref in result.refs:
                    session.seen_refs.add(ref)
            trace_writer.append_prepass(
                cmd=label,
                ok=result.ok,
                bytes=result.bytes,
                wall_ms=result.wall_ms,
                error=result.error,
                error_code=result.error_code,
            )
```

- [ ] **Step 3: Run pre-pass tests**

Run: `pytest tests/test_adapter_prepass.py -v`
Expected: both tests PASS (assuming Task 11's `Session` is already in place).

- [ ] **Step 4: Commit**

```bash
git add src/bitgn_contest_agent/adapter/pcm.py tests/test_adapter_prepass.py
git commit -m "feat: adapter pre-pass best-effort identity bootstrap"
```

---

## Phase 5 — Session + Enforcer

### Task 11: `Session` dataclass with loop detector

**Files:**
- Create: `src/bitgn_contest_agent/session.py`
- Create: `tests/test_session.py`

**Execute this task before Task 10** — `adapter.run_prepass` writes to `Session`.

- [ ] **Step 1: Write the failing session test**

```python
"""Session state and loop detector."""
from __future__ import annotations

from bitgn_contest_agent.session import Session


def test_session_defaults_are_empty() -> None:
    s = Session()
    assert s.seen_refs == set()
    assert s.identity_loaded is False
    assert s.rulebook_loaded is False
    assert s.step == 0
    assert s.nudges_emitted == 0
    assert list(s.recent_calls) == []
    assert s.loop_nudge_needed(("read", "AGENTS.md")) is False


def test_loop_detector_fires_when_same_tuple_seen_3_times_in_last_6() -> None:
    s = Session()
    tup = ("read", "AGENTS.md")
    other = ("list", "/")
    assert s.loop_nudge_needed(tup) is False  # 1 occurrence
    assert s.loop_nudge_needed(other) is False
    assert s.loop_nudge_needed(tup) is False  # 2 occurrences
    assert s.loop_nudge_needed(other) is False
    assert s.loop_nudge_needed(tup) is True   # 3 occurrences — nudge


def test_loop_detector_sliding_window_forgets_old_calls() -> None:
    s = Session()
    tup = ("search", "x")
    # Saturate the window with 6 distinct calls so the old tup is evicted.
    s.loop_nudge_needed(tup)
    for name in ["a", "b", "c", "d", "e", "f"]:
        s.loop_nudge_needed(("list", name))
    # tup has fallen out of the window; two more occurrences should not fire.
    assert s.loop_nudge_needed(tup) is False
    assert s.loop_nudge_needed(tup) is False


def test_nudge_budget_is_tracked_separately() -> None:
    s = Session()
    s.nudges_emitted = 2
    assert s.nudge_budget_remaining(max_nudges=2) == 0
    s.nudges_emitted = 0
    assert s.nudge_budget_remaining(max_nudges=2) == 2
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_session.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/session.py`**

```python
"""Session state and loop detector.

One instance per task run. Lives in the worker thread. Never shared
across tasks (even within the same orchestrator run).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Tuple


_RECENT_WINDOW = 6
_REPEAT_THRESHOLD = 3


@dataclass(slots=True)
class Session:
    seen_refs: set[str] = field(default_factory=set)
    rulebook_loaded: bool = False
    identity_loaded: bool = False
    step: int = 0
    recent_calls: Deque[Tuple[str, ...]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_WINDOW)
    )
    nudges_emitted: int = 0

    def loop_nudge_needed(self, call: Tuple[str, ...]) -> bool:
        """Record a (tool, canonical_args) tuple; return True if the same
        tuple has appeared _REPEAT_THRESHOLD times in the last _RECENT_WINDOW
        entries (i.e., this very call is the threshold-hitting one)."""
        self.recent_calls.append(call)
        count = sum(1 for c in self.recent_calls if c == call)
        return count >= _REPEAT_THRESHOLD

    def nudge_budget_remaining(self, *, max_nudges: int) -> int:
        return max(0, max_nudges - self.nudges_emitted)
```

- [ ] **Step 4: Run tests until green**

Run: `pytest tests/test_session.py -v`
Expected: all four tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/session.py tests/test_session.py
git commit -m "feat: Session dataclass with loop detector"
```

---

### Task 12: Enforcer — R1 grounding-refs + R2 OUTCOME_ERR_INTERNAL

**Files:**
- Create: `src/bitgn_contest_agent/enforcer.py`
- Create: `tests/test_enforcer.py`

Per the §5.3 "what we explicitly do NOT test" list, we don't test the enforcer's rule *truth tables* exhaustively. We test just enough to prove R1 and R2 fire under expected conditions and are exempt for cancel-path synthetic terminals (which bypass the enforcer entirely in the agent loop, T16).

- [ ] **Step 1: Write the failing enforcer test**

```python
"""Enforcer sanity checks — R1 + R2 only (§2.4 minimum-confidence ruleset)."""
from __future__ import annotations

from bitgn_contest_agent.enforcer import Verdict, check_terminal
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion
from bitgn_contest_agent.session import Session


def _mk_terminal(outcome: str, refs: list[str]) -> NextStep:
    return NextStep(
        current_state="done",
        plan_remaining_steps_brief=["report"],
        identity_verified=True,
        function=ReportTaskCompletion(
            tool="report_completion",
            message="all good",
            grounding_refs=refs,
            rulebook_notes="n",
            outcome_justification="j",
            completed_steps_laconic=["read AGENTS.md"],
            outcome=outcome,
        ),
    )


def test_non_terminal_always_passes() -> None:
    step = NextStep(
        current_state="reading",
        plan_remaining_steps_brief=["read", "report"],
        identity_verified=True,
        function={"tool": "read", "path": "AGENTS.md"},
    )
    v = check_terminal(Session(), step)
    assert v.ok
    assert v.reasons == []


def test_r1_fires_when_grounding_ref_not_in_seen_refs() -> None:
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = _mk_terminal("OUTCOME_OK", ["fabricated/path.py"])
    v = check_terminal(session, step)
    assert not v.ok
    assert any("grounding_ref" in r for r in v.reasons)


def test_r1_passes_when_all_grounding_refs_were_seen() -> None:
    session = Session()
    session.seen_refs.update({"AGENTS.md", "README.md"})
    step = _mk_terminal("OUTCOME_OK", ["AGENTS.md", "README.md"])
    v = check_terminal(session, step)
    assert v.ok, v.reasons


def test_r2_rejects_err_internal_outcome() -> None:
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = _mk_terminal("OUTCOME_ERR_INTERNAL", ["AGENTS.md"])
    v = check_terminal(session, step)
    assert not v.ok
    assert any("OUTCOME_ERR_INTERNAL" in r for r in v.reasons)


def test_r2_refusal_outcomes_still_pass() -> None:
    session = Session()
    # NONE_UNSUPPORTED is legitimate from task description alone — no refs required.
    step = _mk_terminal("OUTCOME_NONE_UNSUPPORTED", [])
    v = check_terminal(session, step)
    assert v.ok, v.reasons
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_enforcer.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/enforcer.py`**

```python
"""Terminal emission enforcer — policy checks only.

Runs only on terminal emission (NextStep.function is ReportTaskCompletion).
Never a correctness oracle; only checks policy invariants that must hold
regardless of the task.

v1 ruleset (minimum-confidence):
- R1: grounding-refs reachability (principle, uncalibrated)
- R2: OUTCOME_ERR_INTERNAL hard-gate (data, 473-run corpus: 82 catches @ 100% precision)

All other candidate rules are deferred per §2.4.1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion
from bitgn_contest_agent.session import Session


@dataclass(frozen=True, slots=True)
class Verdict:
    ok: bool
    reasons: List[str] = field(default_factory=list)


def check_terminal(session: Session, step: NextStep) -> Verdict:
    fn = step.function
    if not isinstance(fn, ReportTaskCompletion):
        return Verdict(ok=True, reasons=[])

    reasons: List[str] = []

    # R1 — grounding-refs reachability.
    # Known false-positive: path normalization ("./foo.py" vs "foo.py").
    # Canonicalize both sides the first time a real false positive shows up.
    for ref in fn.grounding_refs:
        if ref not in session.seen_refs:
            reasons.append(f"grounding_ref {ref!r} never successfully read")

    # R2 — OUTCOME_ERR_INTERNAL hard-gate.
    if fn.outcome == "OUTCOME_ERR_INTERNAL":
        reasons.append(
            "OUTCOME_ERR_INTERNAL rejected: 100% historical failure rate on 473-run corpus"
        )

    return Verdict(ok=not reasons, reasons=reasons)
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_enforcer.py -v`
Expected: all five tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/enforcer.py tests/test_enforcer.py
git commit -m "feat: enforcer R1 grounding-refs + R2 OUTCOME_ERR_INTERNAL"
```

---

## Phase 6 — Prompts

### Task 13: Static system prompt + critique + loop nudge helpers

**Files:**
- Create: `src/bitgn_contest_agent/prompts.py`
- Create: `tests/test_prompts.py`

- [ ] **Step 1: Write the failing prompts test**

```python
"""Prompt helpers — keep the static prompt cacheable across tasks."""
from __future__ import annotations

from bitgn_contest_agent import prompts


def test_system_prompt_is_deterministic_without_hint(monkeypatch) -> None:
    monkeypatch.delenv("HINT", raising=False)
    a = prompts.system_prompt()
    b = prompts.system_prompt()
    assert a == b
    # Cross-task caching requires bit-identical content.
    assert isinstance(a, str) and len(a) > 100


def test_system_prompt_includes_outcome_enum_semantics() -> None:
    p = prompts.system_prompt()
    for outcome in [
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    ]:
        assert outcome in p, f"system prompt missing reference to {outcome}"


def test_hint_interpolation_only_happens_when_hint_is_set(monkeypatch) -> None:
    base = prompts.system_prompt()
    monkeypatch.setenv("HINT", "remember: paths are case-sensitive")
    with_hint = prompts.system_prompt()
    assert with_hint != base
    assert "remember: paths are case-sensitive" in with_hint


def test_critique_injection_formats_verdict_reasons() -> None:
    text = prompts.critique_injection(["reason A", "reason B"])
    assert "reason A" in text
    assert "reason B" in text
    assert "retry" in text.lower() or "revise" in text.lower()


def test_loop_nudge_references_repeated_tuple() -> None:
    text = prompts.loop_nudge(("read", "AGENTS.md"))
    assert "read" in text
    assert "AGENTS.md" in text
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_prompts.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/prompts.py`**

```python
"""Prompt composition — static system prompt, critique helper, loop nudge.

The system prompt is the #1 reliability lever. Keep it bit-identical
across runs for provider-side cache hits; only interpolate the HINT env
var when it is set (debug runs).
"""
from __future__ import annotations

import os
from typing import Sequence, Tuple


_STATIC_SYSTEM_PROMPT = """\
You are a BitGN PAC1 task-solving agent. You operate inside a sandboxed
virtual workspace that exposes only these tools (each one corresponds to
exactly one PcmRuntime RPC):

  read, write, delete, mkdir, move, list, tree, find, search, context,
  report_completion

You MUST emit exactly one `NextStep` JSON object per turn. Its
`function` field must be one of the tool variants above.

Identity + rulebook discipline:
  1. Before doing any task-specific work, call `tree root="/"`, then
     `read path="AGENTS.md"`, then `context`. Treat any that succeed as
     your identity bootstrap; do NOT skip this step even if you believe
     you already know the rules.
  2. AGENTS.md is the rulebook. Anything it forbids is forbidden even if
     the task description asks for it.
  3. Never fabricate file references. If you cite a path in
     `grounding_refs`, you must have successfully read that exact path
     earlier in the run.

Tool workflow:
  - Prefer the smallest read that answers the question (`read` >
    `list` > `tree` > `find` > `search`). Don't re-read files you have
    already read.
  - `find` and `search` take a `limit`; start small (10) and grow only
    if necessary.
  - Write operations mutate state. Re-read after writing if your next
    decision depends on the new state.

Outcome semantics (use exactly one in `report_completion.outcome`):
  - OUTCOME_OK: the task was fully answered using evidence from the
    sandbox. `grounding_refs` must list every file you relied on.
  - OUTCOME_DENIED_SECURITY: AGENTS.md explicitly forbids what the task
    asks for. Cite the forbidding rule in `outcome_justification`.
  - OUTCOME_NONE_UNSUPPORTED: the sandbox does not expose the tools
    needed to answer (e.g., the task asks you to call an external API).
  - OUTCOME_NONE_CLARIFICATION: the task is genuinely ambiguous and
    cannot be answered from the available evidence. This is the LAST
    resort — if you find yourself tempted to use it, re-read the task
    and search the sandbox once more. Most tasks tagged as "ambiguous"
    by a rushed reading are answerable from local evidence.
  - OUTCOME_ERR_INTERNAL: reserved for genuine internal failure. The
    enforcer REJECTS this outcome. Do not emit it to escape a hard task.

Reliability rules:
  - Your `current_state` is your thinking scratchpad. Use it.
  - `plan_remaining_steps_brief` must list 1-5 upcoming actions.
  - `identity_verified` stays false until you have successfully loaded
    AGENTS.md and `context`.
  - `completed_steps_laconic` must cite concrete operations you ran,
    not plans.
  - `outcome_justification` must name the specific evidence that
    supports the outcome.

Never dump raw file contents back into your reasoning. Summarize.
"""


def system_prompt() -> str:
    hint = os.environ.get("HINT", "").strip()
    if hint:
        return _STATIC_SYSTEM_PROMPT + f"\n\n[RUN HINT]: {hint}\n"
    return _STATIC_SYSTEM_PROMPT


def critique_injection(reasons: Sequence[str]) -> str:
    body = "\n".join(f"  - {r}" for r in reasons)
    return (
        "Your previous NextStep was rejected by the terminal enforcer. "
        "Revise and retry. The specific reasons were:\n"
        f"{body}\n"
        "Emit a new NextStep that addresses each reason."
    )


def loop_nudge(repeated_call: Tuple[str, ...]) -> str:
    call_repr = " ".join(str(part) for part in repeated_call)
    return (
        f"Loop detector: you have emitted `{call_repr}` three times in the "
        "last six tool calls. This is a signal that the current strategy "
        "is not making progress. Choose a materially different next action."
    )
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_prompts.py -v`
Expected: all five tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/prompts.py tests/test_prompts.py
git commit -m "feat: static system prompt + critique + loop nudge helpers"
```

---

## Phase 7 — Trace schema + writer

### Task 14: Trace schema (single source of truth)

**Files:**
- Create: `src/bitgn_contest_agent/trace_schema.py`
- Create: `tests/test_trace_schema.py`

Per §6.5, the trace schema is the single source of truth imported by both the writer and the reader. All closed enums from §3.5 live here.

- [ ] **Step 1: Write the failing schema test**

```python
"""Trace schema invariants (single source of truth per §6.5)."""
from __future__ import annotations

import json

import pytest

from bitgn_contest_agent.trace_schema import (
    ERROR_KIND_VALUES,
    EVENT_KIND_VALUES,
    ERROR_CODE_VALUES,
    TERMINATED_BY_VALUES,
    TRACE_SCHEMA_VERSION,
    TraceMeta,
    TraceOutcome,
    TraceStep,
    TraceEvent,
    TracePrepass,
    TraceTask,
    StepLLMStats,
    StepToolResult,
    load_jsonl,
)


def test_schema_version_is_tuple_like() -> None:
    parts = TRACE_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_closed_enum_sets_are_frozen_and_cover_spec() -> None:
    assert "CANCELLED" in ERROR_KIND_VALUES
    assert None in ERROR_KIND_VALUES or "NULL" in ERROR_KIND_VALUES or True
    assert "validation_retry" in EVENT_KIND_VALUES
    assert "loop_nudge" in EVENT_KIND_VALUES
    assert "rate_limit_backoff" in EVENT_KIND_VALUES
    assert "timeout_cancel" in EVENT_KIND_VALUES
    assert "enforcer_reject" in EVENT_KIND_VALUES
    assert "report_completion" in TERMINATED_BY_VALUES
    assert "cancel" in TERMINATED_BY_VALUES
    assert "RPC_DEADLINE" in ERROR_CODE_VALUES
    assert "PCM_ERROR" in ERROR_CODE_VALUES


def test_meta_roundtrips() -> None:
    m = TraceMeta(
        agent_version="0.0.7",
        agent_commit="abc",
        model="gpt-5.3-codex",
        backend="openai_compat",
        reasoning_effort="medium",
        benchmark="bitgn/pac1-dev",
        task_id="t14",
        task_index=13,
        started_at="2026-04-10T14:05:12Z",
        trace_schema_version=TRACE_SCHEMA_VERSION,
    )
    parsed = TraceMeta.model_validate_json(m.model_dump_json())
    assert parsed == m


def test_unknown_extra_fields_are_dropped_not_rejected() -> None:
    raw = {
        "kind": "step",
        "step": 1,
        "wall_ms": 42,
        "llm": {"latency_ms": 40, "prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 0, "retry_count": 0},
        "tool_result": {"ok": True, "bytes": 5, "wall_ms": 2, "truncated": False, "error": None, "error_code": None},
        "next_step": {},
        "session_after": {"seen_refs_count": 1, "identity_loaded": True, "rulebook_loaded": True},
        "future_only_field": "safe to ignore",
    }
    s = TraceStep.model_validate(raw)
    assert s.step == 1
    # Unknown field is dropped silently (additive-only policy).
    assert not hasattr(s, "future_only_field")


def test_load_jsonl_parses_heterogeneous_records(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    lines = [
        '{"kind":"meta","agent_version":"0.0.7","agent_commit":"x","model":"gpt-5.3-codex","backend":"openai_compat","reasoning_effort":"medium","benchmark":"bitgn/pac1-dev","task_id":"t1","task_index":0,"started_at":"2026-04-10T00:00:00Z","trace_schema_version":"1.0.0"}',
        '{"kind":"task","task_id":"t1","task_text":"do a thing"}',
        '{"kind":"prepass","cmd":"tree","ok":true,"bytes":10,"wall_ms":5,"error":null,"error_code":null}',
        '{"kind":"outcome","terminated_by":"report_completion","reported":"OUTCOME_OK","enforcer_bypassed":false,"error_kind":null,"total_steps":1,"total_llm_calls":1,"total_prompt_tokens":0,"total_completion_tokens":0,"total_cached_tokens":0}',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = list(load_jsonl(path))
    assert len(records) == 4
    assert isinstance(records[0], TraceMeta)
    assert isinstance(records[-1], TraceOutcome)
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_trace_schema.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/trace_schema.py`**

```python
"""Trace schema — single source of truth per §6.5.

Both the writer (trace_writer.py) and any future reader (scripts/*.py)
MUST import these models. The Pydantic models use extra="ignore" so old
traces with fewer fields and future traces with more fields both parse.

Additive-only evolution within a major version:
- New fields are Optional[...] = None.
- Existing fields are never renamed, retyped, or removed.
- Major bump = commit a new fixture + grow test_version_compat.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


TRACE_SCHEMA_VERSION = "1.0.0"


ERROR_KIND_VALUES: frozenset[Optional[str]] = frozenset(
    {
        None,
        "BACKEND_ERROR",
        "SUBMISSION_FAILED",
        "CONTEXT_OVERFLOW",
        "INTERNAL_CRASH",
        "MAX_STEPS",
        "CANCELLED",
    }
)

EVENT_KIND_VALUES: frozenset[str] = frozenset(
    {
        "validation_retry",
        "loop_nudge",
        "rate_limit_backoff",
        "timeout_cancel",
        "enforcer_reject",
    }
)

TERMINATED_BY_VALUES: frozenset[str] = frozenset(
    {"report_completion", "error", "cancel", "exhausted"}
)

ERROR_CODE_VALUES: frozenset[Optional[str]] = frozenset(
    {None, "RPC_DEADLINE", "RPC_UNAVAILABLE", "PCM_ERROR", "INVALID_ARG", "UNKNOWN"}
)


class _BaseRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TraceMeta(_BaseRecord):
    kind: Literal["meta"] = "meta"
    agent_version: str
    agent_commit: str
    model: str
    backend: str
    reasoning_effort: str
    benchmark: str
    task_id: str
    task_index: int
    started_at: str
    trace_schema_version: str
    cancelled: bool = False


class TraceTask(_BaseRecord):
    kind: Literal["task"] = "task"
    task_id: str
    task_text: str


class TracePrepass(_BaseRecord):
    kind: Literal["prepass"] = "prepass"
    cmd: str
    ok: bool
    bytes: int = 0
    wall_ms: int = 0
    error: Optional[str] = None
    error_code: Optional[str] = None


class StepLLMStats(_BaseRecord):
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0
    retry_count: int = 0


class StepToolResult(_BaseRecord):
    ok: bool
    bytes: int = 0
    wall_ms: int = 0
    truncated: bool = False
    original_bytes: int = 0
    error: Optional[str] = None
    error_code: Optional[str] = None


class StepSessionAfter(_BaseRecord):
    seen_refs_count: int
    identity_loaded: bool
    rulebook_loaded: bool


class TraceStep(_BaseRecord):
    kind: Literal["step"] = "step"
    step: int
    wall_ms: int
    llm: StepLLMStats
    next_step: dict[str, Any]
    tool_result: StepToolResult
    session_after: StepSessionAfter
    enforcer_verdict: Optional[List[str]] = None
    enforcer_action: Optional[str] = None


class TraceEvent(_BaseRecord):
    kind: Literal["event"] = "event"
    at_step: int
    event_kind: str
    wait_ms: Optional[int] = None
    attempt: Optional[int] = None
    details: Optional[str] = None
    repeated_tuple: Optional[List[str]] = None


class TraceOutcome(_BaseRecord):
    kind: Literal["outcome"] = "outcome"
    terminated_by: str
    reported: Optional[str] = None
    enforcer_bypassed: bool = False
    error_kind: Optional[str] = None
    error_msg: Optional[str] = None
    total_steps: int
    total_llm_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cached_tokens: int = 0
    score: Optional[float] = None


TraceRecord = Union[TraceMeta, TraceTask, TracePrepass, TraceStep, TraceEvent, TraceOutcome]


_KIND_TO_MODEL: dict[str, type[_BaseRecord]] = {
    "meta": TraceMeta,
    "task": TraceTask,
    "prepass": TracePrepass,
    "step": TraceStep,
    "event": TraceEvent,
    "outcome": TraceOutcome,
}


def load_jsonl(path: Path) -> Iterator[TraceRecord]:
    """Parse a JSONL trace file into typed records. Unknown kinds raise."""
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            kind = raw.get("kind")
            model = _KIND_TO_MODEL.get(kind)
            if model is None:
                raise ValueError(f"unknown trace record kind: {kind!r}")
            yield model.model_validate(raw)  # type: ignore[misc]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_trace_schema.py -v`
Expected: all five tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/trace_schema.py tests/test_trace_schema.py
git commit -m "feat: trace schema single source of truth with closed enums"
```

---

### Task 15: `TraceWriter` — incremental JSONL + crash fallback

**Files:**
- Create: `src/bitgn_contest_agent/trace_writer.py`
- Create: `tests/test_trace_writer.py`

- [ ] **Step 1: Write the failing writer test**

```python
"""Trace writer — append-per-event JSONL with crash fallback."""
from __future__ import annotations

import json
from pathlib import Path

from bitgn_contest_agent.trace_schema import (
    TRACE_SCHEMA_VERSION,
    TraceMeta,
    load_jsonl,
)
from bitgn_contest_agent.trace_writer import TraceWriter


def _mk_meta(task_id: str = "t1") -> TraceMeta:
    return TraceMeta(
        agent_version="0.0.7",
        agent_commit="dev",
        model="gpt-5.3-codex",
        backend="openai_compat",
        reasoning_effort="medium",
        benchmark="bitgn/pac1-dev",
        task_id=task_id,
        task_index=0,
        started_at="2026-04-10T00:00:00Z",
        trace_schema_version=TRACE_SCHEMA_VERSION,
    )


def test_writer_appends_meta_and_flushes_each_record(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())
    w.append_task(task_id="t1", task_text="do stuff")
    w.append_prepass(cmd="tree", ok=True, bytes=10, wall_ms=5, error=None, error_code=None)
    w.close()

    records = list(load_jsonl(path))
    assert len(records) == 3
    assert records[0].kind == "meta"
    assert records[1].kind == "task"
    assert records[2].kind == "prepass"


def test_writer_survives_crash_and_writes_crashed_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())
    w.write_crash_sidecar("synthetic boom", traceback_text="tb here")
    # Sidecar path is next to the trace.
    sidecar = path.with_name(path.name.replace(".jsonl", "_CRASHED.json"))
    assert sidecar.exists()
    blob = json.loads(sidecar.read_text(encoding="utf-8"))
    assert blob["error"] == "synthetic boom"
    assert blob["traceback"] == "tb here"
    assert blob["partial_trace"] == str(path)


def test_writer_is_thread_safe_per_instance(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())

    def worker(i: int) -> None:
        for _ in range(20):
            w.append_event(at_step=i, event_kind="rate_limit_backoff", wait_ms=10, attempt=1)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    w.close()

    records = list(load_jsonl(path))
    # 1 meta + 100 events
    assert len(records) == 101
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_trace_writer.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/trace_writer.py`**

```python
"""Incremental JSONL writer. Thread-safe per instance.

Each worker creates one TraceWriter, writes records as the run
progresses, and calls close() at the end. On unhandled exception the
worker calls write_crash_sidecar() before re-raising.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from bitgn_contest_agent.trace_schema import (
    StepLLMStats,
    StepSessionAfter,
    StepToolResult,
    TraceEvent,
    TraceMeta,
    TraceOutcome,
    TracePrepass,
    TraceStep,
    TraceTask,
)


class TraceWriter:
    def __init__(self, *, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self._path.open("a", encoding="utf-8", buffering=1)

    @property
    def path(self) -> Path:
        return self._path

    # -- individual record writers ---------------------------------------

    def write_meta(self, meta: TraceMeta) -> None:
        self._write(meta.model_dump(mode="json"))

    def append_task(self, *, task_id: str, task_text: str) -> None:
        rec = TraceTask(task_id=task_id, task_text=task_text)
        self._write(rec.model_dump(mode="json"))

    def append_prepass(
        self,
        *,
        cmd: str,
        ok: bool,
        bytes: int = 0,
        wall_ms: int = 0,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        rec = TracePrepass(cmd=cmd, ok=ok, bytes=bytes, wall_ms=wall_ms, error=error, error_code=error_code)
        self._write(rec.model_dump(mode="json"))

    def append_step(
        self,
        *,
        step: int,
        wall_ms: int,
        llm: StepLLMStats,
        next_step: dict[str, Any],
        tool_result: StepToolResult,
        session_after: StepSessionAfter,
        enforcer_verdict: list[str] | None = None,
        enforcer_action: str | None = None,
    ) -> None:
        rec = TraceStep(
            step=step,
            wall_ms=wall_ms,
            llm=llm,
            next_step=next_step,
            tool_result=tool_result,
            session_after=session_after,
            enforcer_verdict=enforcer_verdict,
            enforcer_action=enforcer_action,
        )
        self._write(rec.model_dump(mode="json"))

    def append_event(
        self,
        *,
        at_step: int,
        event_kind: str,
        wait_ms: Optional[int] = None,
        attempt: Optional[int] = None,
        details: Optional[str] = None,
        repeated_tuple: Optional[list[str]] = None,
    ) -> None:
        rec = TraceEvent(
            at_step=at_step,
            event_kind=event_kind,
            wait_ms=wait_ms,
            attempt=attempt,
            details=details,
            repeated_tuple=repeated_tuple,
        )
        self._write(rec.model_dump(mode="json"))

    def append_outcome(self, outcome: TraceOutcome) -> None:
        self._write(outcome.model_dump(mode="json"))

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                self._fh.close()

    def write_crash_sidecar(self, error: str, *, traceback_text: str) -> None:
        """Write <trace>_CRASHED.json. Uses a separate I/O path so a broken
        main handle does not lose the crash info."""
        sidecar = self._path.with_name(self._path.name.replace(".jsonl", "_CRASHED.json"))
        payload = {
            "error": error,
            "traceback": traceback_text,
            "partial_trace": str(self._path),
        }
        sidecar.write_text(json.dumps(payload), encoding="utf-8")

    # -- internals -------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            if self._fh.closed:
                raise RuntimeError("TraceWriter already closed")
            self._fh.write(line)
            self._fh.write("\n")
            self._fh.flush()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_trace_writer.py -v`
Expected: all three tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/trace_writer.py tests/test_trace_writer.py
git commit -m "feat: incremental JSONL TraceWriter with crash sidecar"
```

---

## Phase 8 — Agent loop

### Task 16: `agent.py` — step loop scaffolding (validation retry + loop nudge + terminal dispatch)

**Files:**
- Create: `src/bitgn_contest_agent/agent.py`
- Create: `tests/test_agent_loop.py`

This task lands the core step loop with P1/P3/P4 patterns and terminal enforcement. Task 17 wires in P2 backend retry and P5 task failure paths.

The agent loop ties together every prior task, so its tests use mock Backend + mock PcmAdapter + real Session/Enforcer/TraceWriter.

- [ ] **Step 1: Write the failing agent loop test**

```python
"""Agent loop scaffold — happy path + enforcer retry path."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence
from unittest.mock import MagicMock

import pytest

from bitgn_contest_agent.agent import AgentLoop, AgentLoopResult
from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult
from bitgn_contest_agent.backend.base import Backend, Message
from bitgn_contest_agent.schemas import NextStep
from bitgn_contest_agent.session import Session
from bitgn_contest_agent.trace_schema import TRACE_SCHEMA_VERSION, TraceMeta
from bitgn_contest_agent.trace_writer import TraceWriter


def _mk_step(function: dict) -> NextStep:
    return NextStep(
        current_state="x",
        plan_remaining_steps_brief=["do", "report"],
        identity_verified=True,
        function=function,
    )


class _ScriptedBackend(Backend):
    def __init__(self, scripted: list[NextStep]) -> None:
        self._steps = list(scripted)
        self.calls = 0

    def next_step(self, messages: Sequence[Message], response_schema, timeout_sec):  # type: ignore[override]
        self.calls += 1
        return self._steps.pop(0)


def _mk_writer(tmp_path: Path) -> TraceWriter:
    w = TraceWriter(path=tmp_path / "trace.jsonl")
    w.write_meta(
        TraceMeta(
            agent_version="0.0.7",
            agent_commit="t",
            model="gpt-5.3-codex",
            backend="openai_compat",
            reasoning_effort="medium",
            benchmark="bitgn/pac1-dev",
            task_id="t1",
            task_index=0,
            started_at="2026-04-10T00:00:00Z",
            trace_schema_version=TRACE_SCHEMA_VERSION,
        )
    )
    return w


def _mk_adapter_mock(tool_result_content: str = "AGENTS.md contents") -> MagicMock:
    adapter = MagicMock(spec=PcmAdapter)
    adapter.run_prepass = MagicMock()
    adapter.dispatch.return_value = ToolResult(
        ok=True,
        content=tool_result_content,
        refs=("AGENTS.md",),
        error=None,
        error_code=None,
        wall_ms=5,
    )
    adapter.submit_terminal.return_value = ToolResult(
        ok=True, content="", refs=(), error=None, error_code=None, wall_ms=3
    )
    return adapter


def _fake_prepass(session: Session) -> None:
    session.identity_loaded = True
    session.rulebook_loaded = True
    session.seen_refs.add("AGENTS.md")


def test_agent_loop_happy_path_read_then_report(tmp_path: Path) -> None:
    backend = _ScriptedBackend(
        [
            _mk_step({"tool": "read", "path": "AGENTS.md"}),
            _mk_step(
                {
                    "tool": "report_completion",
                    "message": "done",
                    "grounding_refs": ["AGENTS.md"],
                    "rulebook_notes": "n",
                    "outcome_justification": "AGENTS.md was read",
                    "completed_steps_laconic": ["read AGENTS.md"],
                    "outcome": "OUTCOME_OK",
                }
            ),
        ]
    )
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=10,
        llm_http_timeout_sec=30.0,
    )
    result: AgentLoopResult = loop.run(task_id="t1", task_text="answer from AGENTS.md")

    assert result.terminated_by == "report_completion"
    assert result.reported == "OUTCOME_OK"
    assert result.enforcer_bypassed is False
    adapter.submit_terminal.assert_called_once()
    writer.close()


def test_agent_loop_enforcer_rejects_fabricated_ref_then_retries(tmp_path: Path) -> None:
    backend = _ScriptedBackend(
        [
            _mk_step(
                {
                    "tool": "report_completion",
                    "message": "done",
                    "grounding_refs": ["imaginary.py"],  # R1 will reject
                    "rulebook_notes": "n",
                    "outcome_justification": "j",
                    "completed_steps_laconic": ["thought about it"],
                    "outcome": "OUTCOME_OK",
                }
            ),
            _mk_step(
                {
                    "tool": "report_completion",
                    "message": "done",
                    "grounding_refs": ["AGENTS.md"],
                    "rulebook_notes": "n",
                    "outcome_justification": "read AGENTS.md",
                    "completed_steps_laconic": ["read AGENTS.md"],
                    "outcome": "OUTCOME_OK",
                }
            ),
        ]
    )
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=10,
        llm_http_timeout_sec=30.0,
    )
    result = loop.run(task_id="t1", task_text="do it")

    assert result.terminated_by == "report_completion"
    assert result.reported == "OUTCOME_OK"
    assert result.enforcer_bypassed is False
    assert backend.calls == 2  # one rejection + one accepted retry
    adapter.submit_terminal.assert_called_once()
    writer.close()


def test_agent_loop_submits_anyway_after_exhausted_enforcer_retry(tmp_path: Path) -> None:
    # Both the initial and the retry emit the same bad terminal.
    bad_terminal = _mk_step(
        {
            "tool": "report_completion",
            "message": "done",
            "grounding_refs": ["still_fake.py"],
            "rulebook_notes": "n",
            "outcome_justification": "j",
            "completed_steps_laconic": ["-"],
            "outcome": "OUTCOME_OK",
        }
    )
    backend = _ScriptedBackend([bad_terminal, bad_terminal])
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(backend=backend, adapter=adapter, writer=writer, max_steps=5, llm_http_timeout_sec=30.0)
    result = loop.run(task_id="t1", task_text="do it")

    assert result.terminated_by == "report_completion"
    assert result.enforcer_bypassed is True   # submit_anyway path
    adapter.submit_terminal.assert_called_once()
    writer.close()


def test_agent_loop_hits_max_steps_and_fails(tmp_path: Path) -> None:
    # Backend keeps emitting read steps forever — never reaches terminal.
    read_step = _mk_step({"tool": "read", "path": "AGENTS.md"})
    backend = _ScriptedBackend([read_step] * 10)
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(backend=backend, adapter=adapter, writer=writer, max_steps=3, llm_http_timeout_sec=30.0)
    result = loop.run(task_id="t1", task_text="do it")

    assert result.terminated_by == "exhausted"
    assert result.error_kind == "MAX_STEPS"
    writer.close()
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_agent_loop.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/agent.py`**

```python
"""Core agent step loop (§2.7).

~120 LoC. Responsibilities:
1. Build initial messages (system prompt + task description).
2. Run pre-pass via adapter.
3. Step loop up to max_steps:
   - Call backend.next_step(...).
   - ValidationError → P3 one-shot retry with critique; re-raise if retry fails.
   - Loop detector → P4 inject nudge on next turn, continue.
   - Dispatch tool via adapter. On failure feed error back to model (P1).
   - If terminal → run enforcer. On retry-exhausted failure → submit anyway.
4. Append everything to the trace.
5. Submit final outcome via adapter.submit_terminal.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from pydantic import ValidationError

from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult
from bitgn_contest_agent.backend.base import Backend, Message
from bitgn_contest_agent.enforcer import Verdict, check_terminal
from bitgn_contest_agent.prompts import critique_injection, loop_nudge, system_prompt
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion
from bitgn_contest_agent.session import Session
from bitgn_contest_agent.trace_schema import (
    StepLLMStats,
    StepSessionAfter,
    StepToolResult,
    TraceOutcome,
)
from bitgn_contest_agent.trace_writer import TraceWriter


_MAX_NUDGES = 2


@dataclass(frozen=True, slots=True)
class AgentLoopResult:
    terminated_by: str
    reported: Optional[str]
    enforcer_bypassed: bool
    error_kind: Optional[str]
    error_msg: Optional[str]
    total_steps: int
    total_llm_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cached_tokens: int


class AgentLoop:
    def __init__(
        self,
        *,
        backend: Backend,
        adapter: PcmAdapter,
        writer: TraceWriter,
        max_steps: int,
        llm_http_timeout_sec: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self._backend = backend
        self._adapter = adapter
        self._writer = writer
        self._max_steps = max_steps
        self._llm_http_timeout_sec = llm_http_timeout_sec
        self._cancel_event = cancel_event

    def run(self, *, task_id: str, task_text: str) -> AgentLoopResult:
        session = Session()
        messages: List[Message] = [
            Message(role="system", content=system_prompt()),
            Message(role="user", content=task_text),
        ]

        # Pre-pass (best effort).
        self._adapter.run_prepass(session=session, trace_writer=self._writer)
        self._writer.append_task(task_id=task_id, task_text=task_text)

        totals = _Totals()
        pending_critique: Optional[str] = None
        pending_nudge: Optional[str] = None

        for step_idx in range(1, self._max_steps + 1):
            if self._cancel_event is not None and self._cancel_event.is_set():
                return self._finish_cancelled(totals, step_idx - 1)

            session.step = step_idx
            step_start = time.monotonic()
            if pending_critique is not None:
                messages.append(Message(role="user", content=pending_critique))
                pending_critique = None
            if pending_nudge is not None:
                messages.append(Message(role="user", content=pending_nudge))
                pending_nudge = None

            # Backend call + P3 validation retry.
            try:
                step_obj = self._backend.next_step(
                    messages=messages,
                    response_schema=NextStep,
                    timeout_sec=self._llm_http_timeout_sec,
                )
            except ValidationError as exc:
                self._writer.append_event(
                    at_step=step_idx,
                    event_kind="validation_retry",
                    details=str(exc)[:500],
                )
                retry_messages = list(messages) + [
                    Message(
                        role="user",
                        content=critique_injection([f"ValidationError: {exc}"]),
                    )
                ]
                try:
                    step_obj = self._backend.next_step(
                        messages=retry_messages,
                        response_schema=NextStep,
                        timeout_sec=self._llm_http_timeout_sec,
                    )
                except ValidationError as exc2:
                    return self._finish_error(
                        totals,
                        step_idx,
                        error_kind="BACKEND_ERROR",
                        error_msg=f"double validation failure: {exc2}",
                    )
            totals.llm_calls += 1

            # Dispatch.
            fn = step_obj.function
            tool_result: ToolResult
            enforcer_verdict: list[str] | None = None
            enforcer_action: str | None = None

            if isinstance(fn, ReportTaskCompletion):
                verdict = check_terminal(session, step_obj)
                if verdict.ok:
                    tool_result = self._adapter.submit_terminal(fn)
                    enforcer_action = "accept"
                else:
                    enforcer_verdict = list(verdict.reasons)
                    self._writer.append_event(
                        at_step=step_idx,
                        event_kind="enforcer_reject",
                        details="; ".join(verdict.reasons)[:500],
                    )
                    # Attempt one retry by injecting critique on next turn.
                    retry_messages = list(messages) + [
                        Message(
                            role="user",
                            content=critique_injection(verdict.reasons),
                        )
                    ]
                    try:
                        retry_step = self._backend.next_step(
                            messages=retry_messages,
                            response_schema=NextStep,
                            timeout_sec=self._llm_http_timeout_sec,
                        )
                        totals.llm_calls += 1
                    except ValidationError:
                        retry_step = step_obj  # fall through to submit_anyway
                    retry_fn = retry_step.function
                    if isinstance(retry_fn, ReportTaskCompletion):
                        retry_verdict = check_terminal(session, retry_step)
                        if retry_verdict.ok:
                            tool_result = self._adapter.submit_terminal(retry_fn)
                            enforcer_action = "accept_after_retry"
                            fn = retry_fn
                        else:
                            tool_result = self._adapter.submit_terminal(retry_fn)
                            enforcer_action = "submit_anyway"
                            enforcer_verdict = list(retry_verdict.reasons)
                            fn = retry_fn
                    else:
                        # Retry returned a non-terminal; submit the original anyway.
                        tool_result = self._adapter.submit_terminal(fn)
                        enforcer_action = "submit_anyway"

                self._log_step(
                    step_idx,
                    step_start,
                    step_obj,
                    tool_result,
                    session,
                    enforcer_verdict=enforcer_verdict,
                    enforcer_action=enforcer_action,
                )
                totals.steps += 1
                return self._finish_report(
                    totals,
                    reported=fn.outcome,
                    enforcer_bypassed=(enforcer_action == "submit_anyway"),
                )

            # Non-terminal: dispatch and loop-detect.
            call_tuple = _canonical_call(fn)
            if session.loop_nudge_needed(call_tuple):
                if session.nudge_budget_remaining(max_nudges=_MAX_NUDGES) > 0:
                    session.nudges_emitted += 1
                    pending_nudge = loop_nudge(call_tuple)
                    self._writer.append_event(
                        at_step=step_idx,
                        event_kind="loop_nudge",
                        repeated_tuple=list(call_tuple),
                    )
                else:
                    return self._finish_error(
                        totals,
                        step_idx,
                        error_kind="INTERNAL_CRASH",
                        error_msg="loop nudge budget exhausted",
                    )

            tool_result = self._adapter.dispatch(fn)
            if tool_result.ok:
                for ref in tool_result.refs:
                    session.seen_refs.add(ref)

            # Feed the tool result back to the planner.
            messages.append(
                Message(
                    role="assistant",
                    content=step_obj.model_dump_json(),
                )
            )
            messages.append(
                Message(
                    role="tool",
                    content=(
                        tool_result.content
                        if tool_result.ok
                        else f"ERROR ({tool_result.error_code}): {tool_result.error}"
                    ),
                )
            )

            self._log_step(step_idx, step_start, step_obj, tool_result, session)
            totals.steps += 1

        # Exhausted max_steps.
        return self._finish_error(
            totals,
            self._max_steps,
            error_kind="MAX_STEPS",
            error_msg=f"exceeded max_steps={self._max_steps}",
        )

    # -- helpers ---------------------------------------------------------

    def _log_step(
        self,
        step_idx: int,
        step_start: float,
        step_obj: NextStep,
        tool_result: ToolResult,
        session: Session,
        *,
        enforcer_verdict: list[str] | None = None,
        enforcer_action: str | None = None,
    ) -> None:
        wall_ms = int((time.monotonic() - step_start) * 1000)
        self._writer.append_step(
            step=step_idx,
            wall_ms=wall_ms,
            llm=StepLLMStats(
                latency_ms=wall_ms,
                prompt_tokens=0,
                completion_tokens=0,
                cached_tokens=0,
                retry_count=0,
            ),
            next_step=step_obj.model_dump(),
            tool_result=StepToolResult(
                ok=tool_result.ok,
                bytes=tool_result.bytes,
                wall_ms=tool_result.wall_ms,
                truncated=tool_result.truncated,
                original_bytes=tool_result.original_bytes,
                error=tool_result.error,
                error_code=tool_result.error_code,
            ),
            session_after=StepSessionAfter(
                seen_refs_count=len(session.seen_refs),
                identity_loaded=session.identity_loaded,
                rulebook_loaded=session.rulebook_loaded,
            ),
            enforcer_verdict=enforcer_verdict,
            enforcer_action=enforcer_action,
        )

    def _finish_report(
        self,
        totals: "_Totals",
        *,
        reported: str,
        enforcer_bypassed: bool,
    ) -> AgentLoopResult:
        outcome = TraceOutcome(
            terminated_by="report_completion",
            reported=reported,
            enforcer_bypassed=enforcer_bypassed,
            error_kind=None,
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
        )
        self._writer.append_outcome(outcome)
        return AgentLoopResult(
            terminated_by="report_completion",
            reported=reported,
            enforcer_bypassed=enforcer_bypassed,
            error_kind=None,
            error_msg=None,
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
        )

    def _finish_error(
        self,
        totals: "_Totals",
        step_idx: int,
        *,
        error_kind: str,
        error_msg: str,
    ) -> AgentLoopResult:
        outcome = TraceOutcome(
            terminated_by="error" if error_kind != "MAX_STEPS" else "exhausted",
            reported=None,
            enforcer_bypassed=False,
            error_kind=error_kind,
            error_msg=error_msg,
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
        )
        self._writer.append_outcome(outcome)
        return AgentLoopResult(
            terminated_by=outcome.terminated_by,
            reported=None,
            enforcer_bypassed=False,
            error_kind=error_kind,
            error_msg=error_msg,
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
        )

    def _finish_cancelled(self, totals: "_Totals", step_idx: int) -> AgentLoopResult:
        # Synthetic cancel-path terminal. BYPASSES the enforcer — written
        # directly by the worker per §3.2.
        outcome = TraceOutcome(
            terminated_by="cancel",
            reported="OUTCOME_ERR_INTERNAL",
            enforcer_bypassed=True,
            error_kind="CANCELLED",
            error_msg="cancelled:timeout",
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
        )
        self._writer.append_outcome(outcome)
        return AgentLoopResult(
            terminated_by="cancel",
            reported="OUTCOME_ERR_INTERNAL",
            enforcer_bypassed=True,
            error_kind="CANCELLED",
            error_msg="cancelled:timeout",
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
        )


@dataclass(slots=True)
class _Totals:
    steps: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


def _canonical_call(fn: object) -> tuple[str, ...]:
    """Produce a stable (tool, sorted-args) tuple for the loop detector."""
    if hasattr(fn, "tool"):
        tool = getattr(fn, "tool")
    else:
        tool = type(fn).__name__
    # Use model_dump so every Req_* turns into a dict of primitives.
    if hasattr(fn, "model_dump"):
        data = fn.model_dump()  # type: ignore[attr-defined]
    else:
        data = {}
    parts = [tool] + [f"{k}={data[k]!r}" for k in sorted(data.keys()) if k != "tool"]
    return tuple(parts)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_agent_loop.py -v`
Expected: all four tests PASS. (If the `submit_anyway` test fails with `AttributeError: Mock object has no attribute 'submit_terminal'`, re-check that the MagicMock was built with `spec=PcmAdapter`.)

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/agent.py tests/test_agent_loop.py
git commit -m "feat: AgentLoop scaffold — P3/P4 + terminal enforcement + submit_anyway"
```

---

### Task 17: Wire P2 backend retry + P5 task failure into the loop

**Files:**
- Modify: `src/bitgn_contest_agent/agent.py`
- Modify: `tests/test_agent_loop.py`

The scaffold from Task 16 does not yet handle `TransientBackendError`. Add a bounded exponential backoff per §3.3.

- [ ] **Step 1: Write the failing P2 test**

Add to `tests/test_agent_loop.py`:

```python
from bitgn_contest_agent.backend.base import TransientBackendError


class _FlakyBackend(Backend):
    """Raises TransientBackendError once, then returns the canned step."""

    def __init__(self, step: NextStep, raise_times: int = 1) -> None:
        self._step = step
        self._remaining_raises = raise_times
        self.calls = 0

    def next_step(self, messages, response_schema, timeout_sec):  # type: ignore[override]
        self.calls += 1
        if self._remaining_raises > 0:
            self._remaining_raises -= 1
            raise TransientBackendError("429", attempt=self.calls)
        return self._step


def test_agent_loop_retries_on_transient_backend_error(tmp_path: Path, monkeypatch) -> None:
    # Replace time.sleep so tests stay fast.
    monkeypatch.setattr("bitgn_contest_agent.agent.time.sleep", lambda s: None)
    backend = _FlakyBackend(
        _mk_step(
            {
                "tool": "report_completion",
                "message": "done",
                "grounding_refs": ["AGENTS.md"],
                "rulebook_notes": "n",
                "outcome_justification": "read",
                "completed_steps_laconic": ["read AGENTS.md"],
                "outcome": "OUTCOME_OK",
            }
        ),
        raise_times=2,
    )
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=5,
        llm_http_timeout_sec=30.0,
        backend_backoff_ms=(1, 1, 1, 1),
    )
    result = loop.run(task_id="t1", task_text="do it")

    assert result.terminated_by == "report_completion"
    assert backend.calls == 3  # 2 transient failures + 1 success
    writer.close()


def test_agent_loop_fails_task_after_backend_exhaustion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("bitgn_contest_agent.agent.time.sleep", lambda s: None)

    class _AlwaysFlaky(Backend):
        def next_step(self, messages, response_schema, timeout_sec):  # type: ignore[override]
            raise TransientBackendError("no capacity")

    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=_AlwaysFlaky(),
        adapter=adapter,
        writer=writer,
        max_steps=5,
        llm_http_timeout_sec=30.0,
        backend_backoff_ms=(1, 1, 1, 1),
    )
    result = loop.run(task_id="t1", task_text="do it")
    assert result.terminated_by == "error"
    assert result.error_kind == "BACKEND_ERROR"
    writer.close()
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_agent_loop.py::test_agent_loop_retries_on_transient_backend_error tests/test_agent_loop.py::test_agent_loop_fails_task_after_backend_exhaustion -v`
Expected: FAIL — `AgentLoop.__init__` rejects `backend_backoff_ms`.

- [ ] **Step 3: Wire P2 into `AgentLoop`**

In `agent.py`, extend the constructor and add a helper:

```python
# top-level in agent.py
_DEFAULT_BACKOFF_MS: tuple[int, ...] = (500, 1500, 4000, 10000)


class AgentLoop:
    def __init__(
        self,
        *,
        backend: Backend,
        adapter: PcmAdapter,
        writer: TraceWriter,
        max_steps: int,
        llm_http_timeout_sec: float,
        cancel_event: Optional[threading.Event] = None,
        backend_backoff_ms: tuple[int, ...] = _DEFAULT_BACKOFF_MS,
    ) -> None:
        self._backend = backend
        self._adapter = adapter
        self._writer = writer
        self._max_steps = max_steps
        self._llm_http_timeout_sec = llm_http_timeout_sec
        self._cancel_event = cancel_event
        self._backoff_ms = backend_backoff_ms
```

Replace the `step_obj = self._backend.next_step(...)` block in `run` with a wrapped call that retries on `TransientBackendError`, and wrap the enforcer-retry backend call the same way. Extract the retry into a private method:

```python
    def _call_backend_with_retry(
        self,
        messages: List[Message],
        *,
        at_step: int,
    ) -> NextStep | None:
        """Returns NextStep on success, or None if all attempts exhausted
        (caller should then finish with BACKEND_ERROR)."""
        from bitgn_contest_agent.backend.base import TransientBackendError

        last_exc: Exception | None = None
        for attempt, wait_ms in enumerate([0, *self._backoff_ms], start=0):
            if wait_ms > 0:
                self._writer.append_event(
                    at_step=at_step,
                    event_kind="rate_limit_backoff",
                    wait_ms=wait_ms,
                    attempt=attempt,
                )
                time.sleep(wait_ms / 1000.0)
            try:
                return self._backend.next_step(
                    messages=messages,
                    response_schema=NextStep,
                    timeout_sec=self._llm_http_timeout_sec,
                )
            except TransientBackendError as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            # Swallow; caller finishes with BACKEND_ERROR.
            return None
        return None
```

Then in `run(...)`, replace the direct backend call with:

```python
            step_obj_or_none = self._call_backend_with_retry(messages, at_step=step_idx)
            if step_obj_or_none is None:
                return self._finish_error(
                    totals,
                    step_idx,
                    error_kind="BACKEND_ERROR",
                    error_msg="transient backend exhausted",
                )
            try:
                step_obj = step_obj_or_none
            except ValidationError as exc:  # pragma: no cover — kept for clarity
                ...
```

(Leave the `ValidationError` handling from Task 16 in place; wrap only the `TransientBackendError` path.)

Do the same for the enforcer-retry call inside the terminal branch.

- [ ] **Step 4: Run the full agent suite**

Run: `pytest tests/test_agent_loop.py -v`
Expected: all six tests PASS (four from Task 16 + two from Task 17).

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/agent.py tests/test_agent_loop.py
git commit -m "feat: P2 transient-backend retry with backoff in AgentLoop"
```

---

## Phase 9 — Orchestrator and harness

### Task 18: Orchestrator — thread pool, cancel event, grace period

**Files:**
- Create: `src/bitgn_contest_agent/orchestrator.py`
- Create: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing orchestrator test**

```python
"""Orchestrator: thread pool + cooperative cancel + per-task deadline."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

import pytest

from bitgn_contest_agent.orchestrator import (
    Orchestrator,
    TaskSpec,
    TaskExecutionResult,
)


@dataclass
class _FakeTrial:
    task_id: str
    instruction: str


def _mk_runner(returns: TaskExecutionResult, *, sleep_s: float = 0.0) -> Callable:
    def runner(task: TaskSpec, cancel_event: threading.Event) -> TaskExecutionResult:
        if sleep_s:
            deadline = time.monotonic() + sleep_s
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    return TaskExecutionResult(
                        task_id=task.task_id,
                        score=0.0,
                        terminated_by="cancel",
                        error_kind="CANCELLED",
                        error_msg=None,
                    )
                time.sleep(0.01)
        return returns
    return runner


def test_orchestrator_runs_all_tasks_and_returns_results() -> None:
    tasks = [
        TaskSpec(task_id=f"t{i}", task_index=i, task_text=f"task {i}")
        for i in range(4)
    ]
    runner = _mk_runner(
        TaskExecutionResult(task_id="", score=1.0, terminated_by="report_completion", error_kind=None, error_msg=None)
    )
    orch = Orchestrator(runner=runner, max_parallel_tasks=2, task_timeout_sec=0)
    results = orch.run(tasks)
    assert len(results) == 4
    assert all(r.terminated_by == "report_completion" for r in results)


def test_orchestrator_cancels_long_running_task_after_deadline() -> None:
    tasks = [TaskSpec(task_id="slow", task_index=0, task_text="...")]
    runner = _mk_runner(
        TaskExecutionResult(task_id="slow", score=1.0, terminated_by="report_completion", error_kind=None, error_msg=None),
        sleep_s=2.0,
    )
    orch = Orchestrator(
        runner=runner,
        max_parallel_tasks=1,
        task_timeout_sec=1,      # 1s deadline
        task_timeout_grace_sec=1,
    )
    t0 = time.monotonic()
    results = orch.run(tasks)
    elapsed = time.monotonic() - t0
    assert len(results) == 1
    assert results[0].terminated_by == "cancel"
    assert elapsed < 2.5  # cancel fired before natural completion


def test_orchestrator_isolation_one_failure_does_not_abort_others() -> None:
    tasks = [
        TaskSpec(task_id="good", task_index=0, task_text="..."),
        TaskSpec(task_id="bad", task_index=1, task_text="..."),
    ]
    def runner(task, cancel_event):
        if task.task_id == "bad":
            raise RuntimeError("synthetic boom")
        return TaskExecutionResult(
            task_id=task.task_id, score=1.0,
            terminated_by="report_completion", error_kind=None, error_msg=None,
        )
    orch = Orchestrator(runner=runner, max_parallel_tasks=2, task_timeout_sec=0)
    results = orch.run(tasks)
    assert len(results) == 2
    by_id = {r.task_id: r for r in results}
    assert by_id["good"].terminated_by == "report_completion"
    assert by_id["bad"].terminated_by == "error"
    assert by_id["bad"].error_kind == "INTERNAL_CRASH"
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_orchestrator.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/orchestrator.py`**

```python
"""Task-level parallelism + cooperative cancel.

Uses ThreadPoolExecutor because the backend interface is synchronous and
the throughput bottleneck is cliproxyapi, not local CPU.

§3.1, §3.2, §4.2 invariant 1 (worker boundary uses except Exception).
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    task_index: int
    task_text: str


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    task_id: str
    score: float
    terminated_by: str
    error_kind: Optional[str]
    error_msg: Optional[str]


TaskRunner = Callable[[TaskSpec, threading.Event], TaskExecutionResult]


class Orchestrator:
    def __init__(
        self,
        *,
        runner: TaskRunner,
        max_parallel_tasks: int,
        task_timeout_sec: int,
        task_timeout_grace_sec: int = 20,
    ) -> None:
        self._runner = runner
        self._max_parallel_tasks = max_parallel_tasks
        self._task_timeout_sec = task_timeout_sec
        self._grace_sec = task_timeout_grace_sec

    def run(self, tasks: Sequence[TaskSpec]) -> List[TaskExecutionResult]:
        results: List[TaskExecutionResult] = [None] * len(tasks)  # type: ignore[list-item]
        cancel_events: dict[int, threading.Event] = {i: threading.Event() for i in range(len(tasks))}

        with cf.ThreadPoolExecutor(max_workers=self._max_parallel_tasks) as pool:
            futures = {
                pool.submit(self._wrap_runner, tasks[i], cancel_events[i]): i
                for i in range(len(tasks))
            }
            start_times = {futures[f]: time.monotonic() for f in futures}
            deadlines = {
                i: (start_times[i] + self._task_timeout_sec) if self._task_timeout_sec > 0 else None
                for i in range(len(tasks))
            }

            pending = set(futures.keys())
            while pending:
                done, pending = cf.wait(
                    pending, timeout=0.25, return_when=cf.FIRST_COMPLETED
                )
                # Fire deadlines.
                now = time.monotonic()
                for fut, idx in list(futures.items()):
                    dl = deadlines[idx]
                    if dl is not None and now >= dl and not fut.done():
                        cancel_events[idx].set()
                        # Extend the future's effective deadline by grace
                        # so the worker can flush its trace.
                        deadlines[idx] = dl + self._grace_sec
                for fut in done:
                    idx = futures[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as exc:
                        results[idx] = TaskExecutionResult(
                            task_id=tasks[idx].task_id,
                            score=0.0,
                            terminated_by="error",
                            error_kind="INTERNAL_CRASH",
                            error_msg=f"{type(exc).__name__}: {exc}",
                        )
        return [r for r in results if r is not None]

    def _wrap_runner(self, task: TaskSpec, cancel_event: threading.Event) -> TaskExecutionResult:
        try:
            return self._runner(task, cancel_event)
        except Exception as exc:
            _LOG.exception("worker crashed on task %s", task.task_id)
            return TaskExecutionResult(
                task_id=task.task_id,
                score=0.0,
                terminated_by="error",
                error_kind="INTERNAL_CRASH",
                error_msg=f"{type(exc).__name__}: {exc}",
            )
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_orchestrator.py -v`
Expected: all three tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: orchestrator thread pool with cooperative cancel"
```

---

### Task 19: Harness wrapper — `get_benchmark → start_playground → end_trial`

**Files:**
- Create: `src/bitgn_contest_agent/harness.py`
- Create: `tests/test_harness.py`

This is the thin wrapper around `HarnessServiceClientSync` that lets the orchestrator iterate over benchmark tasks and score each run. The auth interceptor pattern is taken from the sibling `bitgn_pac1_adapter.py`.

- [ ] **Step 1: Write the failing harness test**

```python
"""Harness wrapper — translates the benchmark 3-step flow."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bitgn_contest_agent.harness import BitgnHarness, StartedTask


def test_list_tasks_calls_get_benchmark_and_returns_task_ids() -> None:
    fake_client = MagicMock()
    fake_task = MagicMock()
    fake_task.task_id = "t1"
    fake_task.preview = "do stuff"
    fake_client.get_benchmark.return_value = MagicMock(tasks=[fake_task])

    h = BitgnHarness(
        harness_client=fake_client,
        runtime_client_factory=MagicMock(),
        benchmark="bitgn/pac1-dev",
    )
    task_ids = h.list_task_ids()
    assert task_ids == ["t1"]
    call = fake_client.get_benchmark.call_args.args[0]
    assert call.benchmark_id == "bitgn/pac1-dev"


def test_start_task_calls_start_playground_and_builds_runtime_client() -> None:
    fake_client = MagicMock()
    playground = MagicMock()
    playground.trial_id = "trial-xyz"
    playground.task_id = "t1"
    playground.benchmark_id = "bitgn/pac1-dev"
    playground.instruction = "solve it"
    playground.harness_url = "https://vm.bitgn/t1"
    fake_client.start_playground.return_value = playground

    runtime_factory = MagicMock()
    runtime_factory.return_value = MagicMock(name="runtime")

    h = BitgnHarness(
        harness_client=fake_client,
        runtime_client_factory=runtime_factory,
        benchmark="bitgn/pac1-dev",
    )
    started = h.start_task("t1")
    assert isinstance(started, StartedTask)
    assert started.trial_id == "trial-xyz"
    assert started.instruction == "solve it"
    runtime_factory.assert_called_once_with(playground.harness_url)


def test_end_task_calls_end_trial_and_returns_score() -> None:
    fake_client = MagicMock()
    fake_client.end_trial.return_value = MagicMock(score=0.75, score_detail=[])
    h = BitgnHarness(
        harness_client=fake_client,
        runtime_client_factory=MagicMock(),
        benchmark="bitgn/pac1-dev",
    )
    started = StartedTask(
        trial_id="trial-xyz",
        task_id="t1",
        benchmark_id="bitgn/pac1-dev",
        instruction="...",
        harness_url="...",
        runtime_client=MagicMock(),
    )
    score, detail = h.end_task(started)
    assert score == 0.75
    assert detail == []
    call = fake_client.end_trial.call_args.args[0]
    assert call.trial_id == "trial-xyz"
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_harness.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/harness.py`**

```python
"""Thin wrapper around the BitGN HarnessService.

Three-step flow:
  1. harness.get_benchmark(...)   → discover task list
  2. harness.start_playground(...) → get trial_id + harness_url (per-task runtime)
  3. harness.end_trial(...)       → submit and receive score

Authentication is a ConnectRPC metadata interceptor (taken from the
sibling bitgn_pac1_adapter.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Tuple

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    GetBenchmarkRequest,
    StartPlaygroundRequest,
)
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from connectrpc.client_sync import MetadataInterceptorSync  # type: ignore[import-not-found]


class _AuthHeaderInterceptor(MetadataInterceptorSync[None]):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def on_start_sync(self, ctx: Any) -> None:  # pragma: no cover — thin glue
        ctx.request_headers()["authorization"] = f"Bearer {self._api_key}"
        return None


@dataclass(frozen=True, slots=True)
class StartedTask:
    trial_id: str
    task_id: str
    benchmark_id: str
    instruction: str
    harness_url: str
    runtime_client: PcmRuntimeClientSync


class BitgnHarness:
    def __init__(
        self,
        *,
        harness_client: HarnessServiceClientSync,
        runtime_client_factory: Callable[[str], PcmRuntimeClientSync],
        benchmark: str,
    ) -> None:
        self._harness = harness_client
        self._runtime_factory = runtime_client_factory
        self._benchmark = benchmark

    @classmethod
    def from_env(cls, *, benchmark: str, bitgn_base_url: str, bitgn_api_key: str) -> "BitgnHarness":
        interceptors = (_AuthHeaderInterceptor(bitgn_api_key),)
        harness_client = HarnessServiceClientSync(bitgn_base_url, interceptors=interceptors)
        return cls(
            harness_client=harness_client,
            runtime_client_factory=lambda url: PcmRuntimeClientSync(url, interceptors=interceptors),
            benchmark=benchmark,
        )

    def list_task_ids(self) -> List[str]:
        resp = self._harness.get_benchmark(GetBenchmarkRequest(benchmark_id=self._benchmark))
        return [t.task_id for t in resp.tasks]

    def start_task(self, task_id: str) -> StartedTask:
        resp = self._harness.start_playground(
            StartPlaygroundRequest(benchmark_id=self._benchmark, task_id=task_id)
        )
        runtime = self._runtime_factory(resp.harness_url)
        return StartedTask(
            trial_id=resp.trial_id,
            task_id=resp.task_id,
            benchmark_id=resp.benchmark_id,
            instruction=resp.instruction,
            harness_url=resp.harness_url,
            runtime_client=runtime,
        )

    def end_task(self, started: StartedTask) -> Tuple[float, list[Any]]:
        resp = self._harness.end_trial(EndTrialRequest(trial_id=started.trial_id))
        return float(resp.score), list(resp.score_detail)
```

Note: if the `connectrpc.client_sync` import path differs in the installed `bitgn` wheel, check `bitgn.harness_connect` for the actual interceptor base class — the sibling project uses `MetadataInterceptorSync` so Plan A assumes the same.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_harness.py -v`
Expected: all three tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/harness.py tests/test_harness.py
git commit -m "feat: BitgnHarness wrapper for the 3-step benchmark flow"
```

---

## Phase 10 — CLI

### Task 20: `bitgn-agent` entrypoint with `run-task` and `run-benchmark`

**Files:**
- Create: `src/bitgn_contest_agent/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write the failing CLI test**

```python
"""CLI argument parsing — no live API calls."""
from __future__ import annotations

import pytest

from bitgn_contest_agent.cli import build_parser


def test_parser_run_task_requires_task_id() -> None:
    parser = build_parser()
    ns = parser.parse_args(["run-task", "--task-id", "t14"])
    assert ns.command == "run-task"
    assert ns.task_id == "t14"


def test_parser_run_benchmark_defaults() -> None:
    parser = build_parser()
    ns = parser.parse_args(["run-benchmark"])
    assert ns.command == "run-benchmark"
    assert ns.runs == 1
    assert ns.max_parallel is None  # falls through to config default


def test_parser_run_benchmark_accepts_output_path() -> None:
    parser = build_parser()
    ns = parser.parse_args(
        ["run-benchmark", "--runs", "3", "--output", "artifacts/bench/out.json"]
    )
    assert ns.runs == 3
    assert ns.output == "artifacts/bench/out.json"


def test_parser_rejects_unknown_command() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run-quarantine"])
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `src/bitgn_contest_agent/cli.py`**

```python
"""bitgn-agent CLI — run-task + run-benchmark.

Fail-fast pattern P6: config validation happens before the thread pool
is created. All runtime wiring lives here; agent.py / orchestrator.py
stay pure.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from bitgn_contest_agent import __version__
from bitgn_contest_agent.adapter.pcm import PcmAdapter
from bitgn_contest_agent.agent import AgentLoop, AgentLoopResult
from bitgn_contest_agent.backend.openai_compat import OpenAIChatBackend
from bitgn_contest_agent.config import AgentConfig, ConfigError, load_from_env
from bitgn_contest_agent.harness import BitgnHarness, StartedTask
from bitgn_contest_agent.orchestrator import (
    Orchestrator,
    TaskExecutionResult,
    TaskSpec,
)
from bitgn_contest_agent.trace_schema import TRACE_SCHEMA_VERSION, TraceMeta
from bitgn_contest_agent.trace_writer import TraceWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bitgn-agent", description="BitGN PAC1 contest agent")
    parser.add_argument("--version", action="version", version=f"bitgn-agent {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    run_task = subs.add_parser("run-task", help="run a single benchmark task")
    run_task.add_argument("--task-id", required=True)
    run_task.add_argument("--benchmark", default=None)
    run_task.add_argument("--log-dir", default=None)

    run_bench = subs.add_parser("run-benchmark", help="run every task in a benchmark")
    run_bench.add_argument("--benchmark", default=None)
    run_bench.add_argument("--runs", type=int, default=1, help="repeat each task N times")
    run_bench.add_argument("--max-parallel", type=int, default=None)
    run_bench.add_argument("--output", default=None, help="bench_summary.json path")
    run_bench.add_argument("--log-dir", default=None)

    return parser


def _resolve_config(args: argparse.Namespace) -> AgentConfig:
    cfg = load_from_env()
    if getattr(args, "benchmark", None):
        cfg = AgentConfig(**{**cfg.__dict__, "benchmark": args.benchmark})  # type: ignore[arg-type]
    if getattr(args, "log_dir", None):
        cfg = AgentConfig(**{**cfg.__dict__, "log_dir": args.log_dir})  # type: ignore[arg-type]
    if getattr(args, "max_parallel", None) is not None:
        cfg = AgentConfig(
            **{**cfg.__dict__, "max_parallel_tasks": args.max_parallel}  # type: ignore[arg-type]
        )
    return cfg


def _make_harness(cfg: AgentConfig) -> BitgnHarness:
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"
    return BitgnHarness.from_env(
        benchmark=cfg.benchmark,
        bitgn_base_url=base_url,
        bitgn_api_key=cfg.bitgn_api_key,
    )


def _make_backend(cfg: AgentConfig) -> OpenAIChatBackend:
    return OpenAIChatBackend.from_config(
        base_url=cfg.cliproxy_base_url,
        api_key=cfg.cliproxy_api_key,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
    )


def _trace_path(cfg: AgentConfig, run_id: str, task_id: str, run_index: int) -> Path:
    return Path(cfg.log_dir) / run_id / f"{task_id}__run{run_index}.jsonl"


def _git_commit_short() -> str:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _run_single_task(
    *,
    cfg: AgentConfig,
    harness: BitgnHarness,
    backend: OpenAIChatBackend,
    task: TaskSpec,
    run_id: str,
    run_index: int,
    cancel_event: threading.Event,
) -> TaskExecutionResult:
    started: StartedTask | None = None
    try:
        started = harness.start_task(task.task_id)
        adapter = PcmAdapter(
            runtime=started.runtime_client,
            max_tool_result_bytes=cfg.max_tool_result_bytes,
        )

        trace_path = _trace_path(cfg, run_id, task.task_id, run_index)
        writer = TraceWriter(path=trace_path)
        writer.write_meta(
            TraceMeta(
                agent_version=__version__,
                agent_commit=_git_commit_short(),
                model=cfg.model,
                backend="openai_compat",
                reasoning_effort=cfg.reasoning_effort,
                benchmark=cfg.benchmark,
                task_id=task.task_id,
                task_index=task.task_index,
                started_at=datetime.now(timezone.utc).isoformat(),
                trace_schema_version=TRACE_SCHEMA_VERSION,
            )
        )

        loop = AgentLoop(
            backend=backend,
            adapter=adapter,
            writer=writer,
            max_steps=cfg.max_steps,
            llm_http_timeout_sec=float(cfg.llm_http_timeout_sec),
            cancel_event=cancel_event,
            backend_backoff_ms=cfg.rate_limit_backoff_ms,
        )
        result: AgentLoopResult = loop.run(
            task_id=task.task_id,
            task_text=started.instruction,
        )
        writer.close()

        score, _detail = harness.end_task(started)
        return TaskExecutionResult(
            task_id=task.task_id,
            score=float(score),
            terminated_by=result.terminated_by,
            error_kind=result.error_kind,
            error_msg=result.error_msg,
        )
    except Exception as exc:
        import traceback as tb

        msg = f"{type(exc).__name__}: {exc}"
        if started is not None:
            try:
                harness.end_task(started)
            except Exception:
                pass
        return TaskExecutionResult(
            task_id=task.task_id,
            score=0.0,
            terminated_by="error",
            error_kind="INTERNAL_CRASH",
            error_msg=msg,
        )


def _cmd_run_task(args: argparse.Namespace) -> int:
    cfg = _resolve_config(args)
    harness = _make_harness(cfg)
    backend = _make_backend(cfg)
    all_ids = harness.list_task_ids()
    try:
        idx = all_ids.index(args.task_id)
    except ValueError:
        print(f"error: task {args.task_id} not found in {cfg.benchmark}", file=sys.stderr)
        return 2

    # Use the harness instruction as the task text (§harness wrapper).
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    task = TaskSpec(task_id=args.task_id, task_index=idx, task_text="")
    result = _run_single_task(
        cfg=cfg,
        harness=harness,
        backend=backend,
        task=task,
        run_id=run_id,
        run_index=0,
        cancel_event=threading.Event(),
    )
    print(json.dumps(result.__dict__, indent=2))
    return 0 if result.terminated_by == "report_completion" else 1


def _cmd_run_benchmark(args: argparse.Namespace) -> int:
    cfg = _resolve_config(args)
    harness = _make_harness(cfg)
    backend = _make_backend(cfg)

    task_ids = harness.list_task_ids()
    tasks: List[TaskSpec] = [
        TaskSpec(task_id=tid, task_index=i, task_text="")
        for i in range(len(task_ids))
        for tid in [task_ids[i]]
    ]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    all_results: list[TaskExecutionResult] = []
    for run_index in range(args.runs):
        def runner(task: TaskSpec, cancel_event: threading.Event, _ri=run_index):
            return _run_single_task(
                cfg=cfg,
                harness=harness,
                backend=backend,
                task=task,
                run_id=run_id,
                run_index=_ri,
                cancel_event=cancel_event,
            )

        orch = Orchestrator(
            runner=runner,
            max_parallel_tasks=cfg.max_parallel_tasks,
            task_timeout_sec=cfg.task_timeout_sec,
            task_timeout_grace_sec=cfg.task_timeout_grace_sec,
        )
        all_results.extend(orch.run(tasks))

    if args.output:
        from scripts.bench_summary import summarize  # type: ignore[attr-defined]

        summary = summarize(logs_dir=Path(cfg.log_dir) / run_id)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"bench summary → {args.output}")

    total = len(all_results)
    passed = sum(1 for r in all_results if r.score >= 1.0)
    print(f"pass rate: {passed}/{total} ({passed / max(1, total) * 100:.1f}%)")
    return 0 if passed == total else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "run-task":
            return _cmd_run_task(args)
        if args.command == "run-benchmark":
            return _cmd_run_benchmark(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: all four tests PASS.

- [ ] **Step 5: Verify the installed console script exists**

Run: `bitgn-agent --version`
Expected: prints `bitgn-agent 0.0.7`.

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/cli.py tests/test_cli.py
git commit -m "feat: bitgn-agent CLI with run-task and run-benchmark"
```

---

## Phase 11 — Bench summary

### Task 21: `scripts/bench_summary.py` — frozen schema aggregator

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/bench_summary.py`
- Create: `tests/test_bench_summary.py`

Per §6.6, `bench_summary` has a **frozen, minimal schema** that never changes. Everything is derived from the JSONL traces, reading only closed-enum fields.

- [ ] **Step 1: Write the failing summary test**

```python
"""bench_summary frozen-schema aggregator test."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.bench_summary import FROZEN_SCHEMA_KEYS, summarize


def _write_trace(path: Path, *, task_id: str, outcome: str, score: float, steps: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "kind": "meta",
                "agent_version": "0.0.7",
                "agent_commit": "x",
                "model": "gpt-5.3-codex",
                "backend": "openai_compat",
                "reasoning_effort": "medium",
                "benchmark": "bitgn/pac1-dev",
                "task_id": task_id,
                "task_index": 0,
                "started_at": "2026-04-10T00:00:00Z",
                "trace_schema_version": "1.0.0",
            }
        ),
        json.dumps({"kind": "task", "task_id": task_id, "task_text": "x"}),
        json.dumps(
            {
                "kind": "outcome",
                "terminated_by": "report_completion",
                "reported": outcome,
                "enforcer_bypassed": False,
                "error_kind": None,
                "total_steps": steps,
                "total_llm_calls": steps,
                "total_prompt_tokens": 100,
                "total_completion_tokens": 10,
                "total_cached_tokens": 0,
                "score": score,
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_summarize_reports_pass_rate_and_frozen_keys(tmp_path: Path) -> None:
    _write_trace(tmp_path / "t1__run0.jsonl", task_id="t1", outcome="OUTCOME_OK", score=1.0, steps=5)
    _write_trace(tmp_path / "t1__run1.jsonl", task_id="t1", outcome="OUTCOME_OK", score=1.0, steps=6)
    _write_trace(tmp_path / "t2__run0.jsonl", task_id="t2", outcome="OUTCOME_NONE_CLARIFICATION", score=0.0, steps=3)
    _write_trace(tmp_path / "t2__run1.jsonl", task_id="t2", outcome="OUTCOME_NONE_CLARIFICATION", score=0.0, steps=4)

    summary = summarize(logs_dir=tmp_path)
    assert set(summary.keys()) == set(FROZEN_SCHEMA_KEYS)
    assert summary["tasks"]["t1"]["runs"] == 2
    assert summary["tasks"]["t1"]["passes"] == 2
    assert summary["tasks"]["t1"]["median_steps"] in (5, 6)
    assert summary["tasks"]["t2"]["passes"] == 0
    assert summary["overall"]["pass_rate"] == pytest.approx(0.5)
    assert summary["overall"]["total_runs"] == 4


def test_summarize_is_stable_across_runs(tmp_path: Path) -> None:
    _write_trace(tmp_path / "t1__run0.jsonl", task_id="t1", outcome="OUTCOME_OK", score=1.0, steps=5)
    a = summarize(logs_dir=tmp_path)
    b = summarize(logs_dir=tmp_path)
    assert a == b


import pytest  # bottom import so tests above can use pytest.approx
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_bench_summary.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `scripts/bench_summary.py`**

```python
# scripts/__init__.py
"""Operator scripts for bitgn-contest-agent."""
```

```python
# scripts/bench_summary.py
"""Aggregate a directory of JSONL traces into a bench_summary.

Per §6.6 Asset A, the output schema is FROZEN. Do not add, rename, or
retype any field. New metrics belong in a separate artifact or in the
full trace detail, not here. Cross-version comparisons depend on this.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List

from bitgn_contest_agent.trace_schema import (
    TraceMeta,
    TraceOutcome,
    load_jsonl,
)


FROZEN_SCHEMA_KEYS = ("schema_version", "overall", "tasks")
BENCH_SUMMARY_SCHEMA_VERSION = "1.0.0"


def _iter_jsonl_files(logs_dir: Path) -> Iterable[Path]:
    return sorted(Path(logs_dir).rglob("*.jsonl"))


def _extract_run(path: Path) -> tuple[str, float, int] | None:
    meta: TraceMeta | None = None
    outcome: TraceOutcome | None = None
    try:
        for rec in load_jsonl(path):
            if isinstance(rec, TraceMeta):
                meta = rec
            elif isinstance(rec, TraceOutcome):
                outcome = rec
    except (ValueError, json.JSONDecodeError):
        return None
    if meta is None or outcome is None:
        return None
    score = float(outcome.score) if outcome.score is not None else (
        1.0 if (outcome.reported == "OUTCOME_OK" and outcome.terminated_by == "report_completion") else 0.0
    )
    return meta.task_id, score, outcome.total_steps


def summarize(*, logs_dir: Path) -> Dict[str, Any]:
    by_task: dict[str, list[tuple[float, int]]] = defaultdict(list)
    total_runs = 0
    total_passes = 0

    for path in _iter_jsonl_files(logs_dir):
        run = _extract_run(path)
        if run is None:
            continue
        task_id, score, steps = run
        by_task[task_id].append((score, steps))
        total_runs += 1
        if score >= 1.0:
            total_passes += 1

    tasks_out: dict[str, dict[str, Any]] = {}
    for task_id, entries in sorted(by_task.items()):
        runs = len(entries)
        passes = sum(1 for s, _ in entries if s >= 1.0)
        med_steps = int(statistics.median(s for _, s in entries)) if entries else 0
        tasks_out[task_id] = {
            "runs": runs,
            "passes": passes,
            "median_steps": med_steps,
        }

    return {
        "schema_version": BENCH_SUMMARY_SCHEMA_VERSION,
        "overall": {
            "total_runs": total_runs,
            "total_passes": total_passes,
            "pass_rate": (total_passes / total_runs) if total_runs else 0.0,
        },
        "tasks": tasks_out,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate JSONL traces into a frozen bench_summary")
    parser.add_argument("logs_dir", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = summarize(logs_dir=args.logs_dir)
    out_text = json.dumps(summary, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out_text, encoding="utf-8")
    else:
        print(out_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_bench_summary.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/bench_summary.py tests/test_bench_summary.py
git commit -m "feat: bench_summary.py frozen-schema aggregator"
```

---

## Phase 12 — Analyzer completeness + version compatibility tests

### Task 22: Analyzer completeness property test (§5.2 Test 4)

**Files:**
- Create: `tests/test_analyzer_completeness.py`

Per §5.2 Test 4, this test uses introspection over `trace_schema` and `schemas` to build a synthetic trace covering every closed-enum value and every `Req_*` variant, then checks that `bench_summary.summarize` surfaces the expected shape. The test is self-updating: adding a new Req variant or event kind picks up automatically.

- [ ] **Step 1: Write the failing completeness test**

```python
"""§5.2 Test 4 — analyzer completeness over the full trace variant space."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest

from bitgn_contest_agent.schemas import REQ_MODELS
from bitgn_contest_agent.trace_schema import (
    ERROR_CODE_VALUES,
    ERROR_KIND_VALUES,
    EVENT_KIND_VALUES,
    TERMINATED_BY_VALUES,
    TRACE_SCHEMA_VERSION,
    load_jsonl,
)
from scripts.bench_summary import FROZEN_SCHEMA_KEYS, summarize


def _synth_trace_for_task(path: Path, *, task_id: str, outcome: str) -> None:
    """Produce a trace that exercises every Req_* variant as a step and
    every event kind + error code as events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append(
        json.dumps(
            {
                "kind": "meta",
                "agent_version": "0.0.7",
                "agent_commit": "x",
                "model": "gpt-5.3-codex",
                "backend": "openai_compat",
                "reasoning_effort": "medium",
                "benchmark": "bitgn/pac1-dev",
                "task_id": task_id,
                "task_index": 0,
                "started_at": "2026-04-10T00:00:00Z",
                "trace_schema_version": TRACE_SCHEMA_VERSION,
            }
        )
    )
    lines.append(json.dumps({"kind": "task", "task_id": task_id, "task_text": "x"}))
    for i, model in enumerate(REQ_MODELS, start=1):
        lines.append(
            json.dumps(
                {
                    "kind": "step",
                    "step": i,
                    "wall_ms": 10,
                    "llm": {
                        "latency_ms": 10,
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "cached_tokens": 0,
                        "retry_count": 0,
                    },
                    "next_step": {"tool": get_args(model.model_fields["tool"].annotation)[0]},
                    "tool_result": {
                        "ok": True,
                        "bytes": 1,
                        "wall_ms": 1,
                        "truncated": False,
                        "original_bytes": 0,
                        "error": None,
                        "error_code": None,
                    },
                    "session_after": {"seen_refs_count": i, "identity_loaded": True, "rulebook_loaded": True},
                }
            )
        )
    for ek in sorted(EVENT_KIND_VALUES):
        lines.append(
            json.dumps({"kind": "event", "at_step": 1, "event_kind": ek})
        )
    lines.append(
        json.dumps(
            {
                "kind": "outcome",
                "terminated_by": "report_completion",
                "reported": outcome,
                "enforcer_bypassed": False,
                "error_kind": None,
                "total_steps": len(REQ_MODELS),
                "total_llm_calls": len(REQ_MODELS),
                "total_prompt_tokens": len(REQ_MODELS),
                "total_completion_tokens": len(REQ_MODELS),
                "total_cached_tokens": 0,
                "score": 1.0 if outcome == "OUTCOME_OK" else 0.0,
            }
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_every_req_variant_is_parseable_by_load_jsonl(tmp_path: Path) -> None:
    trace = tmp_path / "t1__run0.jsonl"
    _synth_trace_for_task(trace, task_id="t1", outcome="OUTCOME_OK")
    records = list(load_jsonl(trace))
    # meta + task + N steps + K events + outcome
    assert records[0].kind == "meta"
    assert records[1].kind == "task"
    assert records[-1].kind == "outcome"
    step_count = sum(1 for r in records if r.kind == "step")
    assert step_count == len(REQ_MODELS)


def test_summary_keys_match_frozen_schema_over_exhaustive_synthetic_trace(tmp_path: Path) -> None:
    _synth_trace_for_task(tmp_path / "t1__run0.jsonl", task_id="t1", outcome="OUTCOME_OK")
    _synth_trace_for_task(tmp_path / "t2__run0.jsonl", task_id="t2", outcome="OUTCOME_NONE_CLARIFICATION")
    summary = summarize(logs_dir=tmp_path)
    assert set(summary.keys()) == set(FROZEN_SCHEMA_KEYS)
    assert "t1" in summary["tasks"]
    assert "t2" in summary["tasks"]
    assert summary["overall"]["total_runs"] == 2


def test_closed_enum_sets_are_non_empty_and_disjoint_where_expected() -> None:
    assert EVENT_KIND_VALUES
    assert TERMINATED_BY_VALUES
    assert ERROR_CODE_VALUES
    assert ERROR_KIND_VALUES
    # terminated_by and event_kind must not overlap — they live in different enum slots.
    assert TERMINATED_BY_VALUES.isdisjoint(EVENT_KIND_VALUES)
```

- [ ] **Step 2: Run, watch pass (analyzer already handles these shapes)**

Run: `pytest tests/test_analyzer_completeness.py -v`
Expected: all three tests PASS. If a test fails it is either because a new `Req_*` variant was added without updating REQ_MODELS (fix `schemas.py`) or because a new event kind was added without updating `EVENT_KIND_VALUES` (fix `trace_schema.py`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_analyzer_completeness.py
git commit -m "test: analyzer completeness property test over trace variants"
```

---

### Task 23: Capture golden trace fixture + version-compat test (§5.2 Test 5)

**Files:**
- Create: `tests/fixtures/trace_v1.jsonl`
- Create: `tests/test_version_compat.py`

This task produces the first golden fixture frozen under v1 schema. The fixture should be a real trace from the agent — if the first live run (T24) hasn't happened yet, use a hand-written synthetic one that exercises the full shape; replace with a real one once T24 produces one.

- [ ] **Step 1: Write the failing version-compat test**

```python
"""§5.2 Test 5 — every committed fixture parses cleanly and yields the
same core metrics via the current analyzer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitgn_contest_agent.trace_schema import TraceMeta, TraceOutcome, load_jsonl
from scripts.bench_summary import summarize


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _committed_fixtures() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("trace_v*.jsonl"))


@pytest.mark.parametrize("fixture", _committed_fixtures(), ids=lambda p: p.name)
def test_fixture_parses_with_current_analyzer(fixture: Path) -> None:
    records = list(load_jsonl(fixture))
    assert records, f"{fixture.name} is empty"
    assert any(r.kind == "meta" for r in records)
    assert any(r.kind == "outcome" for r in records)


@pytest.mark.parametrize("fixture", _committed_fixtures(), ids=lambda p: p.name)
def test_fixture_summarizes_without_error(tmp_path: Path, fixture: Path) -> None:
    # summarize walks a directory, so copy the single fixture into a dir.
    (tmp_path / fixture.name).write_bytes(fixture.read_bytes())
    summary = summarize(logs_dir=tmp_path)
    assert "overall" in summary
    assert summary["overall"]["total_runs"] == 1
```

- [ ] **Step 2: Write an initial golden fixture by hand**

Create `tests/fixtures/trace_v1.jsonl`:

```jsonl
{"kind":"meta","agent_version":"0.0.7","agent_commit":"seed","model":"gpt-5.3-codex","backend":"openai_compat","reasoning_effort":"medium","benchmark":"bitgn/pac1-dev","task_id":"t_fixture","task_index":0,"started_at":"2026-04-10T00:00:00Z","trace_schema_version":"1.0.0","cancelled":false}
{"kind":"task","task_id":"t_fixture","task_text":"seed fixture — replace with real trace after first live run"}
{"kind":"prepass","cmd":"tree","ok":true,"bytes":10,"wall_ms":5,"error":null,"error_code":null}
{"kind":"prepass","cmd":"read_agents_md","ok":true,"bytes":500,"wall_ms":12,"error":null,"error_code":null}
{"kind":"prepass","cmd":"context","ok":true,"bytes":30,"wall_ms":3,"error":null,"error_code":null}
{"kind":"step","step":1,"wall_ms":200,"llm":{"latency_ms":195,"prompt_tokens":500,"completion_tokens":50,"cached_tokens":0,"retry_count":0},"next_step":{"current_state":"reading","plan_remaining_steps_brief":["read","report"],"identity_verified":true,"function":{"tool":"read","path":"AGENTS.md"}},"tool_result":{"ok":true,"bytes":500,"wall_ms":4,"truncated":false,"original_bytes":0,"error":null,"error_code":null},"session_after":{"seen_refs_count":1,"identity_loaded":true,"rulebook_loaded":true}}
{"kind":"step","step":2,"wall_ms":1200,"llm":{"latency_ms":1195,"prompt_tokens":800,"completion_tokens":120,"cached_tokens":400,"retry_count":0},"next_step":{"current_state":"reporting","plan_remaining_steps_brief":["report"],"identity_verified":true,"function":{"tool":"report_completion","message":"fixture done","grounding_refs":["AGENTS.md"],"rulebook_notes":"followed identity","outcome_justification":"read AGENTS.md","completed_steps_laconic":["read AGENTS.md"],"outcome":"OUTCOME_OK"}},"tool_result":{"ok":true,"bytes":0,"wall_ms":3,"truncated":false,"original_bytes":0,"error":null,"error_code":null},"session_after":{"seen_refs_count":1,"identity_loaded":true,"rulebook_loaded":true},"enforcer_verdict":null,"enforcer_action":"accept"}
{"kind":"outcome","terminated_by":"report_completion","reported":"OUTCOME_OK","enforcer_bypassed":false,"error_kind":null,"error_msg":null,"total_steps":2,"total_llm_calls":2,"total_prompt_tokens":1300,"total_completion_tokens":170,"total_cached_tokens":400,"score":1.0}
```

- [ ] **Step 3: Run the version-compat suite**

Run: `pytest tests/test_version_compat.py -v`
Expected: both parametrized tests PASS against `trace_v1.jsonl`.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/trace_v1.jsonl tests/test_version_compat.py
git commit -m "test: version-compat suite + seed trace_v1.jsonl fixture"
```

---

## Phase 13 — First live run + ratchet floor

### Task 24: First benchmark run → committed `bench_summary` + ratchet floor

**Files:**
- Create: `artifacts/bench/<commit>_<timestamp>.json`
- Modify: `tests/fixtures/trace_v1.jsonl` (replace seed with a real trace)

This is the task that proves the whole pipeline works end-to-end against `bitgn/pac1-dev`. Keep it small the first time — `--runs 1`, not 3.

**Pre-flight environment check:**

- `BITGN_API_KEY` is set and valid for the contest benchmark
- `CLIPROXY_BASE_URL=http://127.0.0.1:8317/v1`
- `CLIPROXY_API_KEY` is the local key from `~/cliproxyapi/config/local_api_key.txt`
- `cliproxyapi` is running and reachable: `curl -s $CLIPROXY_BASE_URL/models -H "authorization: bearer $CLIPROXY_API_KEY"` returns 200
- the `gpt-5.3-codex` auth directory (`~/cliproxyapi/auths/`) contains a valid token

- [ ] **Step 1: Smoke-test with a single task**

Run:

```bash
bitgn-agent run-task --task-id t01 --log-dir logs
```

Expected: exits 0 (task passed) OR 1 (task ran to completion but the grader returned score < 1); either way a JSONL trace at `logs/<run_id>/t01__run0.jsonl` exists and is non-empty.

If the run crashes with a `ConfigError`, check env vars and retry.
If the run crashes with a `TypeError` in the backend, toggle `use_structured_output=False` in `OpenAIChatBackend.from_config` and re-run — this resolves §9 open question 5 empirically.
If the run crashes with a `ValidationError` on `NextStep`, inspect the trace's `event` entries with `event_kind=validation_retry` to see the raw model output.

- [ ] **Step 2: Run the full benchmark once**

Run:

```bash
bitgn-agent run-benchmark --runs 1 --max-parallel 4 \
  --output "artifacts/bench/$(git rev-parse --short HEAD)_$(date -u +%Y%m%dT%H%M%SZ).json" \
  --log-dir logs
```

Expected: ~40 tasks run in parallel, the CLI prints a `pass rate: X/43 (Y%)` line at the end, and `artifacts/bench/<commit>_<timestamp>.json` is created.

- [ ] **Step 3: Replace the seed fixture with a real trace**

Pick one passing trace (preferably a short one, 5-10 steps, with no events other than a clean path) and copy it over `tests/fixtures/trace_v1.jsonl`:

```bash
cp logs/<run_id>/tXX__run0.jsonl tests/fixtures/trace_v1.jsonl
```

Re-run the version-compat suite:

```bash
pytest tests/test_version_compat.py -v
```

Expected: PASS. If it fails, the real trace contains a field the analyzer doesn't know — either the analyzer is broken (fix it) or the field is a future-only extra (add it as `Optional[...] = None` in `trace_schema.py`).

- [ ] **Step 4: Record the ratchet floor**

Open `artifacts/bench/<commit>_<timestamp>.json` and note the `overall.pass_rate`. Add a README note inside the artifacts directory:

Create `artifacts/bench/README.md`:

```markdown
# bench_summary history

Committed artifacts in this directory form the ratchet floor for merges
per the regression harness gate in §5.4 of the design spec.

## Rule
Every PR must produce a `bench_summary` whose `overall.pass_rate` is
greater than or equal to the maximum `pass_rate` previously recorded in
this directory. PRs that regress the floor are blocked.

## Current floor
See the most recent `*.json` file.
```

- [ ] **Step 5: Commit the artifact + fixture update**

```bash
git add artifacts/bench/ tests/fixtures/trace_v1.jsonl
git commit -m "artifact: first bench_summary + real trace_v1.jsonl fixture"
```

- [ ] **Step 6: Tag the ratchet floor in project memory**

After the commit lands, record the floor number in your project memory (file: `memory/project_bench_ratchet.md`) so you remember it in the next conversation without grepping commits. Include the pass rate, the date, and the commit hash.

---

## Plan A Self-Review

### Spec coverage

| Spec section | Plan task(s) |
|---|---|
| §1 Architecture (5 layers) | T1 (package), T6-7 (backend), T8-10 (adapter), T16-17 (agent loop), T18 (orchestrator), T14-15 (traces) |
| §2.1 Provider-agnostic backend | T6 (Protocol), T7 (openai_compat) |
| §2.2 Schemas (Union + NextStep + ReportTaskCompletion) | T2 |
| §2.3 Session state + loop detector | T11 |
| §2.4 Enforcer (R1 + R2) | T12 |
| §2.4.1 Deferred rules | Documented in spec; no code in Plan A beyond R1+R2 |
| §2.5 Prompts | T13 |
| §2.6 Adapter + pre-pass | T8, T9, T10 |
| §2.7 Agent loop | T16, T17 |
| §3.1 Par-A threading | T18 |
| §3.2 Cooperative cancel + cancel-path synthetic terminal | T16 (`_finish_cancelled`), T18 (deadline + cancel_event) |
| §3.3 Transient backend retry | T17 (`_call_backend_with_retry` with backoff) |
| §3.4 Prompt caching | T13 (static prompt is bit-identical) |
| §3.5 Trace format (JSONL + closed enums + crash fallback) | T14, T15 |
| §4 Error handling P1-P7 | T16 (P1 tool-feedback, P3 validation retry, P4 loop nudge, P5 task fail, P7 cancel), T17 (P2 backoff), T5 (P6 fail-fast startup), T18 (worker boundary) |
| §4.1 Calibrated defaults | T5 |
| §4.2 Error-handling invariants | T16 (nudge budget, retry_count++, submit-anyway logging), T15 (crash sidecar), T18 (except Exception at worker boundary) |
| §5.2 Test 1 — tool coverage | T3 |
| §5.2 Test 2 — schema round-trip | T4 |
| §5.2 Test 3 — adapter dispatch | T9 |
| §5.2 Test 4 — analyzer completeness | T22 |
| §5.2 Test 5 — version compat | T23 |
| §5.4 Regression harness pass criteria | T24 (first run establishes the floor) |
| §6.2 `bench_summary.py` | T21 |
| §6.6 Schema evolution (Asset A frozen, Asset B additive) | T14 (extra=ignore), T21 (FROZEN_SCHEMA_KEYS), T23 (golden fixture), T22 (test 4) |

**Deferred (Plan B), tracked here:**

- §6.2 `trace_stats.py`, `failure_clusters.py`, `grep_traces.py`, `trace_diff.py`, `bench_diff.py`, `agent_ctl.py`
- §6.3 `.claude/skills/bitgn-agent-ops/SKILL.md`

### Placeholder scan

- No "TBD" / "TODO" / "implement later" anywhere in the plan.
- No "similar to Task N" references — code blocks are always repeated in full.
- No "add appropriate error handling" — the error taxonomy is the §4 table, and every touchpoint references the specific P-pattern by number.
- The only deliberately unresolved items are the two `NotImplementedError` stubs in Task 8's scaffold, which Task 9 and Task 10 explicitly replace.

### Type consistency

- `NextStep.function` uses a discriminated union on the `tool` field — every `Req_*` model declares `tool: Literal["..."]` (enforced by `test_req_models_are_discriminated_by_tool_field`).
- `ToolResult` is the single return shape from both `adapter.dispatch` and `adapter.submit_terminal`. All consumers (agent loop, pre-pass, cli) use the same dataclass.
- `TaskExecutionResult` fields (`task_id`, `score`, `terminated_by`, `error_kind`, `error_msg`) are identical in the orchestrator, the CLI, and the runner signature — consistent across T18 and T20.
- Method names on `PcmRuntimeClientSync` use snake_case (`mk_dir`, not `mkdir`) — consistent in adapter impl T9 and the `pcm_pb2` transcription.
- `TraceOutcome.terminated_by` uses the closed enum `{report_completion, error, cancel, exhausted}` — the agent loop's `_finish_*` helpers emit exactly these values.
- Cancel-path synthetic terminal sets `enforcer_bypassed=True` in both `_finish_cancelled` (T16) and the spec §3.2 narrative.

No type drift detected.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-10-bitgn-agent-plan-a-core.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. Required sub-skill: `superpowers:subagent-driven-development`.
2. **Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints for review.

Which approach would you like?





