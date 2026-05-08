"""Workspace tree walker tests using a fake PCM client."""
from __future__ import annotations

from dataclasses import dataclass, field

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from bitgn_scraper.workspace_walk import walk_and_dump_workspace, walk_workspace


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
            raise ConnectError(Code.NOT_FOUND, path)
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


def test_walk_and_dump_writes_files_to_disk(tmp_path) -> None:
    tree = _FakeEntry(name="/", is_dir=True, children=[
        _FakeEntry(name="a.md", is_dir=False),
        _FakeEntry(name="sub", is_dir=True, children=[
            _FakeEntry(name="b.md", is_dir=False),
        ]),
    ])
    files = {"/a.md": "alpha", "/sub/b.md": "bravo"}
    pcm = _FakePcm(tree, files)

    dump_root = tmp_path / "dump"
    records = walk_and_dump_workspace(pcm, dump_root)

    assert (dump_root / "a.md").read_text() == "alpha"
    assert (dump_root / "sub" / "b.md").read_text() == "bravo"
    assert sorted(r.path for r in records) == ["/a.md", "/sub/b.md"]


def test_walk_workspace_skips_unreadable_with_marker() -> None:
    tree = _FakeEntry(name="/", is_dir=True, children=[
        _FakeEntry(name="a.md", is_dir=False),
        _FakeEntry(name="bad.bin", is_dir=False),
    ])
    files = {"/a.md": "alpha"}  # /bad.bin missing → ConnectError on read
    pcm = _FakePcm(tree, files)

    records = walk_workspace(pcm)
    bad = next(r for r in records if r.path == "/bad.bin")
    assert bad.byte_size == 0
    assert bad.sha256 == "READ_ERROR"
