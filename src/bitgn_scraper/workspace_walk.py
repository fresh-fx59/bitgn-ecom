"""Walk a live PCM workspace and produce FileRecord rows.

Used by Phase 1 (initial scrape) and Phase 3 (integrity check).
A file that fails to read is recorded with byte_size=0, sha256='READ_ERROR'
so the scrape doesn't abort on a single bad file.

`walk_and_dump_workspace` adds content-to-disk capture in the same pass
without changing the FileRecord shape; it's used by the full PROD
scrape so the local harness can replay trials offline.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from bitgn_scraper.fingerprint import FileRecord


def walk_workspace(pcm: Any) -> list[FileRecord]:
    """Return a FileRecord per file in the workspace rooted at /."""
    from bitgn.vm.pcm_pb2 import TreeRequest

    tree_resp = pcm.tree(TreeRequest(root="/"))

    records: list[FileRecord] = []
    _collect(pcm, tree_resp.root, "", records, dump_root=None)
    return records


def walk_and_dump_workspace(pcm: Any, dump_root: Path) -> list[FileRecord]:
    """Walk + save every file's content under `dump_root`.

    `dump_root/<rel_path>` mirrors the workspace tree. Existing files
    are overwritten. Returns the same FileRecord rows as
    `walk_workspace`.
    """
    from bitgn.vm.pcm_pb2 import TreeRequest

    dump_root.mkdir(parents=True, exist_ok=True)
    tree_resp = pcm.tree(TreeRequest(root="/"))

    records: list[FileRecord] = []
    _collect(pcm, tree_resp.root, "", records, dump_root=dump_root)
    return records


def _collect(
    pcm: Any,
    entry: Any,
    prefix: str,
    out: list[FileRecord],
    *,
    dump_root: Path | None,
) -> None:
    """Recursive helper. Mutates `out`."""
    from bitgn.vm.pcm_pb2 import ReadRequest
    from connectrpc.errors import ConnectError

    name = entry.name or ""
    path = prefix + ("/" + name if name and name != "/" else "")
    if entry.is_dir:
        for child in entry.children:
            _collect(pcm, child, path, out, dump_root=dump_root)
        return

    file_path = path or "/"
    try:
        resp = pcm.read(ReadRequest(path=file_path))
        content_bytes = resp.content.encode("utf-8")
        out.append(FileRecord(
            path=file_path,
            sha256=hashlib.sha256(content_bytes).hexdigest(),
            byte_size=len(content_bytes),
        ))
        if dump_root is not None:
            rel = file_path.lstrip("/")
            target = dump_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content_bytes)
    except ConnectError:
        out.append(FileRecord(
            path=file_path,
            sha256="READ_ERROR",
            byte_size=0,
        ))
