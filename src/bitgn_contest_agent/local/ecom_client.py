"""Filesystem-backed ECOM mock — serves workspace snapshots as if they
were a live BitGN ECOM sandbox. Mirrors the public ECOM RPC surface:

    tree, list, read, search, find, stat, exec, context,
    write, delete, answer

This enables offline replay of tasks against local workspace snapshots
without connecting to the BitGN server, and is the fixture for unit
tests of the agent loop end-to-end (prepass + steps + termination).

Provenance: shape adapted from the PAC1 LocalPcmClient. Wire-level
differences from PCM that matter to call sites:

  - Tree response: each node has `kind` (NodeKind enum), not `is_dir`.
    The recursive node type is `Entry`, not `TreeEntry`.
  - List response: entries are flat `Entry` records with `name`, `path`,
    `kind`, `content_type` (no nested children).
  - Read response: gains `path`, `content_type`, `sha256`, `truncated`
    fields alongside `content`. Optional line slicing
    (start_line / end_line, 1-based, 0 = unbounded).
  - Find response: payload is `paths: list[str]` (was `items` on PCM).
  - Search response: each match is `path / line / line_text`.
  - Stat: not present on PCM. Returns kind/content_type/writable plus
    a write_schema / description for any file the runtime documents.
  - Exec: not present on PCM. The local mock implements `/bin/sql`
    against any *.db / *.sqlite file in the workspace via Python's
    sqlite3; other paths return exit_code=127 + a stderr explaining
    that local exec only supports SQL.

The mock is intentionally NOT proto-typed — it returns lightweight
duck-typed dataclasses with the same attribute surface the agent
adapter consumes. That keeps the local path free of buf.build wheel
hassles and matches the PAC1 lineage's design.
"""
from __future__ import annotations

import calendar
import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


# ---- NodeKind: mirrors ecom_pb2.NodeKind values exactly ----

NODE_KIND_UNSPECIFIED = 0
NODE_KIND_FILE = 1
NODE_KIND_DIR = 2


# ---- Lightweight response wrappers (duck-type compatible with proto) ----


@dataclass
class _Entry:
    name: str
    kind: int
    content_type: str = ""
    children: list["_Entry"] = field(default_factory=list)
    path: str = ""


@dataclass
class _SearchMatch:
    path: str
    line: int
    line_text: str


@dataclass
class _TreeResponse:
    root: _Entry
    truncated: bool = False


@dataclass
class _ListResponse:
    path: str
    entries: list[_Entry]


@dataclass
class _ReadResponse:
    path: str
    content_type: str
    content: str
    sha256: str
    truncated: bool = False


@dataclass
class _FindResponse:
    paths: list[str]
    truncated: bool = False


@dataclass
class _SearchResponse:
    matches: list[_SearchMatch]
    truncated: bool = False


@dataclass
class _StatResponse:
    path: str
    kind: int
    content_type: str = ""
    writable: bool = True
    write_schema_content_type: str = ""
    write_schema: str = ""
    description: str = ""


@dataclass
class _ExecResponse:
    exit_code: int
    content_type: str = ""
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False


@dataclass
class _ContextResponse:
    unix_time: int
    time: str


@dataclass
class _WriteResponse:
    path: str
    audit_path: str = ""
    action_id: str = ""
    action_status: int = 0


@dataclass
class _DeleteResponse:
    """ECOM DeleteResponse is empty on the wire — kept as a typed marker
    so the adapter's `_finish` path has something to JSON-format."""


@dataclass
class _AnswerResponse:
    """ECOM AnswerResponse is empty on the wire."""


# ---- Helpers ----


_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".csv", ".tsv",
    ".py", ".sh", ".sql", ".html", ".xml",
}

_SQL_BIN_PATHS = frozenset({"/bin/sql", "bin/sql"})


def _guess_content_type(path: Path) -> str:
    """Best-effort content_type. The real ECOM runtime returns a much
    richer taxonomy, but for the local mock a tiny heuristic is enough
    to keep agent code that branches on `content_type` happy."""
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        return f"text/{suffix.lstrip('.')}" if suffix else "text/plain"
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        return "application/x-sqlite3"
    return "application/octet-stream"


def _slice_lines(content: str, start_line: int, end_line: int) -> tuple[str, bool]:
    """Apply ECOM's read.start_line / read.end_line slicing. Both bounds
    are 1-based inclusive; 0 means unbounded. Returns (sliced, truncated)
    where truncated is True iff slicing actually dropped content.
    """
    if start_line <= 0 and end_line <= 0:
        return content, False
    lines = content.splitlines(keepends=True)
    n = len(lines)
    lo = max(start_line - 1, 0) if start_line > 0 else 0
    hi = min(end_line, n) if end_line > 0 else n
    if lo == 0 and hi == n:
        return content, False
    return "".join(lines[lo:hi]), True


# ---- The client ----


class LocalEcomClient:
    """Drop-in replacement for `EcomRuntimeClientSync` that reads from a
    local directory instead of making gRPC calls.

    Write/delete operations mutate the local snapshot, so a verifier
    can inspect the final state. Every call appends to `ops_log` for
    test assertions and trace comparison.

    Parameters
    ----------
    workspace_root:
        Filesystem root that emulates the ECOM workspace. The runtime's
        view of "/" maps to this directory.
    context_date:
        Optional ISO8601 string (e.g. ``"2026-05-08T12:00:00Z"``) used
        as the value `context()` returns. Defaults to ``datetime.now(UTC)``.
    sql_db_paths:
        Optional iterable of catalogue SQLite databases (relative to
        workspace_root) that `/bin/sql` should attach. If empty,
        defaults to every ``*.db`` / ``*.sqlite`` / ``*.sqlite3`` file
        found in the workspace at construction time.

    Environment overrides:
        ECOM_LOCAL_CASE_INSENSITIVE_SEARCH=1  — make `search` ignore case
        ECOM_LOCAL_CASE_INSENSITIVE_FIND=1    — make `find` ignore case
    """

    def __init__(
        self,
        workspace_root: str | Path,
        context_date: str | None = None,
        sql_db_paths: Optional[Iterable[str | Path]] = None,
    ) -> None:
        self._root = Path(workspace_root).resolve()
        if not self._root.exists():
            raise FileNotFoundError(f"Workspace root not found: {self._root}")
        self._context_date = context_date or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._sql_dbs: list[Path] = self._resolve_sql_dbs(sql_db_paths)

        self.ops_log: list[dict[str, Any]] = []
        self.reads: set[str] = set()
        self.writes: dict[str, str] = {}
        self.deletes: set[str] = set()

    # ---- public introspection ----

    @property
    def workspace_root(self) -> Path:
        return self._root

    def get_workspace_hash(self) -> str:
        """SHA-256 hash of all file contents — for snapshot diffing
        between runs."""
        h = hashlib.sha256()
        for filepath in sorted(self._root.rglob("*")):
            if filepath.is_dir():
                continue
            rel = str(filepath.relative_to(self._root))
            content = filepath.read_bytes()
            h.update(f"{rel}:{len(content)}:".encode())
            h.update(content)
        return h.hexdigest()

    # ---- ECOM RPC surface ----

    def tree(self, req: Any) -> _TreeResponse:
        root_path = getattr(req, "root", "") or "/"
        level = int(getattr(req, "level", 0) or 0)
        resolved = self._resolve(root_path)

        def _walk(p: Path, depth: int) -> _Entry:
            if p.is_dir():
                children: list[_Entry] = []
                # level=0 means unbounded (matches ECOM TreeRequest spec)
                if level == 0 or depth < level:
                    for child in sorted(p.iterdir()):
                        children.append(_walk(child, depth + 1))
                return _Entry(
                    name=p.name if p != self._root else "",
                    kind=NODE_KIND_DIR,
                    content_type="",
                    children=children,
                )
            return _Entry(
                name=p.name,
                kind=NODE_KIND_FILE,
                content_type=_guess_content_type(p),
            )

        entry = _walk(resolved, depth=0)
        self.ops_log.append({"op": "tree", "root": root_path, "level": level})
        return _TreeResponse(root=entry, truncated=False)

    def list(self, req: Any) -> _ListResponse:
        path = getattr(req, "path", "") or "/"
        resolved = self._resolve(path)
        if not resolved.exists() or not resolved.is_dir():
            raise FileNotFoundError(f"Directory not found: {path}")
        entries: list[_Entry] = []
        for child in sorted(resolved.iterdir()):
            kind = NODE_KIND_DIR if child.is_dir() else NODE_KIND_FILE
            entries.append(
                _Entry(
                    name=child.name,
                    path="/" + str(child.relative_to(self._root)),
                    kind=kind,
                    content_type=_guess_content_type(child) if not child.is_dir() else "",
                )
            )
        self.ops_log.append({"op": "list", "path": path, "n": len(entries)})
        return _ListResponse(path=path, entries=entries)

    def read(self, req: Any) -> _ReadResponse:
        path = getattr(req, "path", "")
        start_line = int(getattr(req, "start_line", 0) or 0)
        end_line = int(getattr(req, "end_line", 0) or 0)
        resolved = self._resolve(path)
        if not resolved.exists() or resolved.is_dir():
            raise FileNotFoundError(f"File not found: {path}")
        raw = resolved.read_text(encoding="utf-8", errors="replace")
        sliced, truncated = _slice_lines(raw, start_line, end_line)
        sha = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        self.reads.add(path.lstrip("/"))
        self.ops_log.append({
            "op": "read", "path": path,
            "bytes": len(sliced), "truncated": truncated,
        })
        return _ReadResponse(
            path=path,
            content_type=_guess_content_type(resolved),
            content=sliced,
            sha256=sha,
            truncated=truncated,
        )

    def search(self, req: Any) -> _SearchResponse:
        root = getattr(req, "root", "") or "/"
        pattern = getattr(req, "pattern", "")
        limit = int(getattr(req, "limit", 10) or 10)
        resolved = self._resolve(root)

        flags = re.IGNORECASE if os.environ.get("ECOM_LOCAL_CASE_INSENSITIVE_SEARCH") else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            self.ops_log.append({
                "op": "search", "root": root, "pattern": pattern,
                "matches": 0, "error": "invalid_regex",
            })
            return _SearchResponse(matches=[], truncated=False)

        matches: list[_SearchMatch] = []
        truncated = False
        for filepath in sorted(resolved.rglob("*")):
            if filepath.is_dir():
                continue
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(_SearchMatch(
                        path="/" + str(filepath.relative_to(self._root)),
                        line=i,
                        line_text=line[:200],
                    ))
                    if len(matches) >= limit:
                        truncated = True
                        break
            if len(matches) >= limit:
                break

        self.ops_log.append({
            "op": "search", "root": root, "pattern": pattern,
            "matches": len(matches), "truncated": truncated,
        })
        return _SearchResponse(matches=matches, truncated=truncated)

    def find(self, req: Any) -> _FindResponse:
        """Substring match on file names. PROD ECOM find is case-sensitive
        substring; the local mock matches PROD by default. Set
        ``ECOM_LOCAL_CASE_INSENSITIVE_FIND=1`` for lenient replay.

        ``kind`` (NodeKind enum) filters: NODE_KIND_FILE → files only,
        NODE_KIND_DIR → dirs only, anything else → both.
        """
        root = getattr(req, "root", "") or "/"
        name = getattr(req, "name", "")
        kind = int(getattr(req, "kind", NODE_KIND_UNSPECIFIED) or 0)
        limit = int(getattr(req, "limit", 10) or 10)
        resolved = self._resolve(root)

        ci = bool(os.environ.get("ECOM_LOCAL_CASE_INSENSITIVE_FIND"))
        needle = name.lower() if ci else name

        results: list[str] = []
        truncated = False
        for filepath in sorted(resolved.rglob("*")):
            if kind == NODE_KIND_FILE and filepath.is_dir():
                continue
            if kind == NODE_KIND_DIR and not filepath.is_dir():
                continue
            haystack = filepath.name.lower() if ci else filepath.name
            if needle in haystack:
                results.append("/" + str(filepath.relative_to(self._root)))
                if len(results) >= limit:
                    truncated = True
                    break

        self.ops_log.append({
            "op": "find", "root": root, "name": name,
            "kind": kind, "results": len(results),
        })
        return _FindResponse(paths=results, truncated=truncated)

    def stat(self, req: Any) -> _StatResponse:
        path = getattr(req, "path", "")
        resolved = self._resolve(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Path not found: {path}")
        kind = NODE_KIND_DIR if resolved.is_dir() else NODE_KIND_FILE
        ct = _guess_content_type(resolved) if kind == NODE_KIND_FILE else ""
        self.ops_log.append({"op": "stat", "path": path, "kind": kind})
        return _StatResponse(
            path=path,
            kind=kind,
            content_type=ct,
            writable=os.access(resolved, os.W_OK) if resolved.exists() else False,
        )

    def exec(self, req: Any) -> _ExecResponse:
        """Local exec — supports /bin/sql against attached SQLite DBs.

        For any other ``path``, returns exit_code=127 with a stderr
        explaining the limitation. The PROD ECOM runtime exposes a
        richer /bin inventory (workspace-specific scripts and CLI
        wrappers); reproducing those locally is out of scope — task
        snapshots that exercise non-SQL exec calls won't replay
        deterministically against the mock.
        """
        path = getattr(req, "path", "")
        args = list(getattr(req, "args", []) or [])
        stdin = getattr(req, "stdin", "") or ""
        if path in _SQL_BIN_PATHS:
            return self._exec_sql(args=args, stdin=stdin)

        self.ops_log.append({
            "op": "exec", "path": path, "args": args, "exit_code": 127,
        })
        return _ExecResponse(
            exit_code=127,
            content_type="text/plain",
            stdout="",
            stderr=(
                f"local mock: exec {path!r} not supported. "
                "Only /bin/sql is implemented. Provide a SQLite "
                "catalogue in the workspace to query via SQL, or run "
                "this task against a real ECOM VM."
            ),
        )

    def _exec_sql(self, *, args: list[str], stdin: str) -> _ExecResponse:
        """Run the stdin SQL body against the workspace's SQLite
        catalogues. Multiple databases are attached as
        ``ATTACH DATABASE ... AS db<N>`` so a query can join across them
        without the agent needing to know which file holds which table.

        Returns plain-text stdout (column-aligned rows) — that is the
        format the PROD /bin/sql shipped with sample-agents/ecom-py
        emits, and the agent's prompt assumes."""
        if not self._sql_dbs:
            return _ExecResponse(
                exit_code=2,
                content_type="text/plain",
                stderr=(
                    "local /bin/sql: no SQLite catalogues found in this "
                    "workspace. Drop a *.db or *.sqlite file under the "
                    "root, or pass `sql_db_paths=...` to LocalEcomClient."
                ),
            )

        if not stdin.strip():
            return _ExecResponse(
                exit_code=2,
                content_type="text/plain",
                stderr="local /bin/sql: empty SQL body on stdin",
            )

        # Open the first DB as the main connection; attach the rest
        # under stable aliases (db1, db2, ...) so cross-DB joins are
        # possible without the agent needing to know the file mapping.
        primary = self._sql_dbs[0]
        try:
            conn = sqlite3.connect(str(primary))
            for i, db in enumerate(self._sql_dbs[1:], start=1):
                conn.execute(
                    "ATTACH DATABASE ? AS ?", (str(db), f"db{i}")
                )
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(stdin)
            rows = cursor.fetchall()
            cols = (
                [c[0] for c in cursor.description]
                if cursor.description else []
            )
            stdout = _format_rows(cols, rows)
            self.ops_log.append({
                "op": "exec", "path": "/bin/sql", "args": args,
                "rows": len(rows), "exit_code": 0,
            })
            return _ExecResponse(
                exit_code=0,
                content_type="text/tab-separated-values",
                stdout=stdout,
            )
        except sqlite3.Error as exc:
            self.ops_log.append({
                "op": "exec", "path": "/bin/sql", "args": args,
                "exit_code": 1, "error": str(exc),
            })
            return _ExecResponse(
                exit_code=1,
                content_type="text/plain",
                stderr=f"sqlite3 error: {exc}",
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def context(self, req: Any = None) -> _ContextResponse:
        time_str = self._context_date
        try:
            dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")
            unix_time = int(calendar.timegm(dt.timetuple()))
        except (ValueError, TypeError):
            unix_time = 0
        self.ops_log.append({"op": "context"})
        return _ContextResponse(unix_time=unix_time, time=time_str)

    def write(self, req: Any) -> _WriteResponse:
        path = getattr(req, "path", "")
        content = getattr(req, "content", "")
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        clean = path.lstrip("/")
        self.writes[clean] = content
        self.ops_log.append({
            "op": "write", "path": path, "bytes": len(content),
        })
        return _WriteResponse(path=path)

    def delete(self, req: Any) -> _DeleteResponse:
        path = getattr(req, "path", "")
        resolved = self._resolve(path)
        if resolved.is_dir():
            raise IsADirectoryError(f"Cannot delete directory: {path}")
        if resolved.exists():
            resolved.unlink()
        self.deletes.add(path.lstrip("/"))
        self.ops_log.append({"op": "delete", "path": path})
        return _DeleteResponse()

    def answer(self, req: Any) -> _AnswerResponse:
        self.ops_log.append({
            "op": "answer",
            "message": getattr(req, "message", ""),
            "outcome": int(getattr(req, "outcome", 0) or 0),
            "refs": list(getattr(req, "refs", []) or []),
        })
        return _AnswerResponse()

    # ---- internals ----

    def _resolve(self, path: str) -> Path:
        clean = (path or "/").lstrip("/")
        resolved = (self._root / clean).resolve() if clean else self._root.resolve()
        if not str(resolved).startswith(str(self._root.resolve())):
            raise ValueError(f"Path escapes workspace: {path}")
        return resolved

    def _resolve_sql_dbs(
        self, supplied: Optional[Iterable[str | Path]],
    ) -> list[Path]:
        if supplied is not None:
            return [self._resolve(str(p)) for p in supplied]
        # Auto-discover any SQLite-shaped file at workspace root or one
        # level deep. Deeper trees are not auto-attached to keep startup
        # predictable; pass `sql_db_paths=...` for non-default layouts.
        out: list[Path] = []
        for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
            out.extend(sorted(self._root.glob(pattern)))
            out.extend(sorted(self._root.glob(f"*/{pattern}")))
        return out


def _format_rows(cols: list[str], rows: list[Any]) -> str:
    """Tab-separated rows with a header — matches the wire shape the
    PROD /bin/sql is documented to return in the ECOM sample.

    Empty result sets return just the header line so downstream parsers
    can still distinguish "no rows" from "no schema"."""
    if not cols:
        return ""
    out = ["\t".join(cols)]
    for row in rows:
        if hasattr(row, "keys"):
            values = [_format_cell(row[c]) for c in cols]
        else:
            values = [_format_cell(v) for v in row]
        out.append("\t".join(values))
    return "\n".join(out) + "\n"


def _format_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)
