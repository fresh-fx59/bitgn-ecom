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
