"""Filesystem-backed PCM mock — serves workspace snapshots as if they
were a live BitGN sandbox. Supports the same operations the agent uses:
tree, list, read, search, find, context, write, delete, move, mkdir.

This enables offline replay of PROD tasks against local workspace
snapshots without connecting to the BitGN server.

Usage as a library:
    from scripts.local_pcm import LocalPcmClient
    client = LocalPcmClient("artifacts/ws_snapshots/t001/run_0/workspace")
    tree_resp = client.tree(TreeRequest(root="/"))
    read_resp = client.read(ReadRequest(path="/AGENTS.MD"))
"""
from __future__ import annotations

import calendar
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class TreeEntry:
    name: str
    is_dir: bool
    children: list["TreeEntry"] = field(default_factory=list)


@dataclass
class ListEntry:
    name: str
    is_dir: bool


@dataclass
class SearchMatch:
    path: str
    snippet: str
    line_number: int


class LocalPcmClient:
    """Drop-in replacement for PcmRuntimeClientSync that reads from a
    local directory instead of making gRPC calls.

    Write/delete/move/mkdir operations mutate the local snapshot, so
    the grader (or a local verifier) can inspect the final state.
    """

    def __init__(
        self,
        workspace_root: str | Path,
        context_date: str | None = None,
    ):
        self._root = Path(workspace_root).resolve()
        if not self._root.exists():
            raise FileNotFoundError(f"Workspace root not found: {self._root}")
        # Context date — defaults to today
        self._context_date = context_date or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # Track all operations for grading/verification
        self.ops_log: list[dict[str, Any]] = []
        self.reads: set[str] = set()
        self.writes: dict[str, str] = {}  # path -> content
        self.deletes: set[str] = set()

    def _resolve(self, path: str) -> Path:
        """Resolve a workspace path to a local filesystem path."""
        # Normalize: remove leading / and collapse
        clean = path.lstrip("/")
        resolved = (self._root / clean).resolve()
        # Safety: ensure within workspace root
        if not str(resolved).startswith(str(self._root.resolve())):
            raise ValueError(f"Path escapes workspace: {path}")
        return resolved

    def tree(self, req: Any) -> Any:
        """Emulate tree RPC — returns recursive directory listing."""
        root_path = getattr(req, "root", "/")
        resolved = self._resolve(root_path)

        def _walk(p: Path, depth: int = 0) -> TreeEntry:
            name = p.name or "/"
            if p.is_dir():
                children = []
                if depth < 10:  # prevent infinite recursion
                    for child in sorted(p.iterdir()):
                        children.append(_walk(child, depth + 1))
                return TreeEntry(name=name, is_dir=True, children=children)
            return TreeEntry(name=name, is_dir=False)

        entry = _walk(resolved)
        self.ops_log.append({"op": "tree", "root": root_path})
        return _TreeResponse(root=entry)

    def read(self, req: Any) -> Any:
        """Emulate read RPC — returns file content."""
        path = getattr(req, "path", "")
        resolved = self._resolve(path)
        if not resolved.exists() or resolved.is_dir():
            raise FileNotFoundError(f"File not found: {path}")
        content = resolved.read_text(encoding="utf-8", errors="replace")
        self.reads.add(path.lstrip("/"))
        self.ops_log.append({"op": "read", "path": path, "bytes": len(content)})
        return _ReadResponse(content=content)

    def list(self, req: Any) -> Any:
        """Emulate list RPC — returns directory entries."""
        name = getattr(req, "name", "/")
        resolved = self._resolve(name)
        if not resolved.exists() or not resolved.is_dir():
            raise FileNotFoundError(f"Directory not found: {name}")
        entries = []
        for child in sorted(resolved.iterdir()):
            entries.append(ListEntry(name=child.name, is_dir=child.is_dir()))
        self.ops_log.append({"op": "list", "name": name})
        return _ListResponse(entries=entries)

    def search(self, req: Any) -> Any:
        """Emulate search RPC — regex search across files."""
        root = getattr(req, "root", "/")
        pattern = getattr(req, "pattern", "")
        limit = getattr(req, "limit", 100)
        resolved = self._resolve(root)

        matches: list[SearchMatch] = []
        total = 0
        # PROD PCM search is case-sensitive. Default local matches PROD;
        # set PCM_LOCAL_CASE_INSENSITIVE=1 to override for lenient replay.
        flags = re.IGNORECASE if os.environ.get("PCM_LOCAL_CASE_INSENSITIVE") else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            self.ops_log.append({"op": "search", "root": root, "pattern": pattern, "matches": 0})
            return _SearchResponse(matches=[], total_matches=0)

        for filepath in sorted(resolved.rglob("*")):
            if filepath.is_dir():
                continue
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel_path = "/" + str(filepath.relative_to(self._root))
                    matches.append(SearchMatch(
                        path=rel_path,
                        snippet=line[:200],
                        line_number=i,
                    ))
                    total += 1
                    if total >= limit:
                        break
            if total >= limit:
                break

        self.ops_log.append({"op": "search", "root": root, "pattern": pattern, "matches": total})
        return _SearchResponse(matches=matches, total_matches=total)

    def find(self, req: Any) -> Any:
        """Emulate find RPC — find files by name pattern.

        PROD PCM find is case-sensitive substring (per cf90740 trace
        evidence: 3/3 PROD find calls returned empty 2-byte envelopes
        for non-matching exact-case names). Default local matches PROD;
        set PCM_LOCAL_FIND_CASE_INSENSITIVE=1 to override.
        """
        root = getattr(req, "root", "/")
        name = getattr(req, "name", "")
        limit = getattr(req, "limit", 100)
        resolved = self._resolve(root)

        case_insensitive = bool(os.environ.get("PCM_LOCAL_FIND_CASE_INSENSITIVE"))
        needle = name.lower() if case_insensitive else name

        results: list[str] = []
        for filepath in sorted(resolved.rglob("*")):
            haystack = filepath.name.lower() if case_insensitive else filepath.name
            if needle in haystack:
                rel_path = "/" + str(filepath.relative_to(self._root))
                results.append(rel_path)
                if len(results) >= limit:
                    break

        self.ops_log.append({"op": "find", "root": root, "name": name, "results": len(results)})
        return _FindResponse(items=results)

    def context(self, req: Any = None) -> Any:
        """Emulate context RPC — returns current date.

        PROD ContextResponse has both `time` (ISO8601 string) and
        `unix_time` (int seconds). We mirror that shape exactly.
        """
        time_str = self._context_date
        try:
            dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")
            unix_time = int(calendar.timegm(dt.timetuple()))
        except (ValueError, TypeError):
            unix_time = 0
        self.ops_log.append({"op": "context"})
        return _ContextResponse(time=time_str, unix_time=unix_time)

    def write(self, req: Any) -> Any:
        """Emulate write RPC — writes content to file."""
        path = getattr(req, "path", "")
        content = getattr(req, "content", "")
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        clean_path = path.lstrip("/")
        self.writes[clean_path] = content
        self.ops_log.append({"op": "write", "path": path, "bytes": len(content)})
        return _WriteResponse(ok=True)

    def delete(self, req: Any) -> Any:
        """Emulate delete RPC — removes a file."""
        path = getattr(req, "path", "")
        resolved = self._resolve(path)
        if resolved.exists():
            resolved.unlink()
        self.deletes.add(path.lstrip("/"))
        self.ops_log.append({"op": "delete", "path": path})
        return _DeleteResponse(ok=True)

    def move(self, req: Any) -> Any:
        """Emulate move RPC — renames a file."""
        from_name = getattr(req, "from_name", "")
        to_name = getattr(req, "to_name", "")
        src = self._resolve(from_name)
        dst = self._resolve(to_name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        self.ops_log.append({"op": "move", "from": from_name, "to": to_name})
        return _MoveResponse(ok=True)

    def mkdir(self, req: Any) -> Any:
        """Emulate mkdir RPC — creates a directory."""
        path = getattr(req, "path", "")
        resolved = self._resolve(path)
        resolved.mkdir(parents=True, exist_ok=True)
        self.ops_log.append({"op": "mkdir", "path": path})
        return _MkdirResponse(ok=True)

    def answer(self, req: Any) -> Any:
        """Emulate answer RPC — records the agent's submission."""
        self.ops_log.append({
            "op": "answer",
            "message": getattr(req, "message", ""),
            "outcome": getattr(req, "outcome", ""),
        })
        return _AnswerResponse(ok=True)

    def get_workspace_hash(self) -> str:
        """SHA-256 hash of all file contents — for diff comparison."""
        h = hashlib.sha256()
        for filepath in sorted(self._root.rglob("*")):
            if filepath.is_dir():
                continue
            rel = str(filepath.relative_to(self._root))
            content = filepath.read_bytes()
            h.update(f"{rel}:{len(content)}:".encode())
            h.update(content)
        return h.hexdigest()


# ---- Lightweight response wrappers (duck-type compatible with protobuf) ----

@dataclass
class _TreeResponse:
    # Field name matches protobuf TreeResponse.root so preflight tools
    # that access tree_resp.root work without modification.
    root: TreeEntry

@dataclass
class _ReadResponse:
    content: str

@dataclass
class _ListResponse:
    entries: list[ListEntry]

@dataclass
class _SearchResponse:
    matches: list[SearchMatch]
    total_matches: int

@dataclass
class _FindResponse:
    items: list[str]

@dataclass
class _ContextResponse:
    time: str
    unix_time: int = 0

@dataclass
class _WriteResponse:
    ok: bool

@dataclass
class _DeleteResponse:
    ok: bool

@dataclass
class _MoveResponse:
    ok: bool

@dataclass
class _MkdirResponse:
    ok: bool

@dataclass
class _AnswerResponse:
    ok: bool
