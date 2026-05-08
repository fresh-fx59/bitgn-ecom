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
