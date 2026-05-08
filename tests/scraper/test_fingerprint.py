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
