"""Filesystem-backed ECOM mock — serves workspace snapshots as if they
were a live BitGN ECOM sandbox. Mirrors the public ECOM RPC surface
as of the 2026-05-15 API freeze:

    tree, list, read, search, find, stat, exec,
    write, delete, answer

The `context()` RPC was retired at the freeze; actor identity is now
exposed via `exec(/bin/id)` and the trial clock via `exec(/bin/date)`.

This enables offline replay of tasks against local workspace snapshots
without connecting to the BitGN server, and is the fixture for unit
tests of the agent loop end-to-end (prepass + steps + termination).

Wire-level alignment with PROD
==============================

The mock returns real ``ecom_pb2.*Response`` proto messages, not
duck-typed dataclasses, so the adapter's ``_response_to_text`` formats
them via ``MessageToJson`` exactly the way PROD responses are formatted.
That keeps the user-message strings the LLM sees identical between
local replay and live trials.

Mismatches we explicitly mirror (caught via wire-level probe against
bitgn/ecom1-dev on 2026-05-11):

  - tree.root.name == "/" for the root path; otherwise the directory name
  - NodeKind serialized as the enum name string (NODE_KIND_DIR, …) by
    MessageToJson — automatic when we return real protos
  - content_type uses canonical MIME types: text/markdown for .md/.MD,
    application/json for .json, text/csv / text/plain for SQL output,
    application/octet-stream otherwise
  - read.number=True prefixes each line with `<N spaces>N\\t` like the
    `cat -n` format the PROD runtime emits
  - stat omits `writable` and `content_type` when not applicable
    (proto default-omit handles this automatically)
  - find.paths and search.matches: proto repeated fields, MessageToJson
    omits empty ones so an empty result serializes to ``{}``
  - /bin/sql output is CSV (comma-separated, header row, text/csv);
    `.schema` returns text/plain with raw DDL

Heuristic toggles (for offline replay flexibility):
  ECOM_LOCAL_CASE_INSENSITIVE_SEARCH=1  — search ignores case
  ECOM_LOCAL_CASE_INSENSITIVE_FIND=1    — find ignores case
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from bitgn.vm.ecom import ecom_pb2


# ---- NodeKind: re-export proto enum values so call sites have stable names ----

NODE_KIND_UNSPECIFIED = ecom_pb2.NodeKind.NODE_KIND_UNSPECIFIED
NODE_KIND_FILE = ecom_pb2.NodeKind.NODE_KIND_FILE
NODE_KIND_DIR = ecom_pb2.NodeKind.NODE_KIND_DIR


# ---- Helpers ----

_CONTENT_TYPE_BY_EXT = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/plain",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".xml": "application/xml",
    ".sql": "application/sql",
    ".py": "text/x-python",
    ".sh": "application/x-sh",
}

_SQLITE_EXTS = {".db", ".sqlite", ".sqlite3"}

_SQL_BIN_PATHS = frozenset({"/bin/sql", "bin/sql"})
_ID_BIN_PATHS = frozenset({"/bin/id", "bin/id"})
_DATE_BIN_PATHS = frozenset({"/bin/date", "bin/date"})
_CHECKOUT_BIN_PATHS = frozenset({"/bin/checkout", "bin/checkout"})
_DISCOUNT_BIN_PATHS = frozenset({"/bin/discount", "bin/discount"})
_PAYMENTS_BIN_PATHS = frozenset({"/bin/payments", "bin/payments"})
_JQ_BIN_PATHS = frozenset({"/bin/jq", "bin/jq"})

# PROD's /bin/jq is a custom "PowerTools E-Commerce OS jq" (probed live
# 2026-05-30, artifacts/prod_explore/jq_probe*.json), NOT real jq. It
# prepends this banner line to stdout on EVERY invocation (even errors).
_JQ_BANNER = "PowerTools E-Commerce OS jq\n"
_JQ_USAGE = "Usage: jq [-r|--raw-output] <filter> [path|-]\n"
# Single path token: `.field`, `[<int>]`, or `[]` (iterate-all).
_JQ_TOKEN_RE = re.compile(r"\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\]|\[\]")

# /bin/* entries are zero-byte stubs in the real ECOM runtime —
# `read /bin/checkout` returns {path, content_type, sha256} with
# NO `content` field. The agent uses `--help` arg via exec, not read.
# Local mock mirrors this so `_response_to_text` produces the same
# JSON shape the LLM sees on PROD. Confirmed across 62 trials
# (scans run1 + run2, 2026-05-15): every /bin/* file has sha256=
# e3b0c442... (empty string) and content_type=text/plain.
_BIN_STUB_PATHS = frozenset({
    "/bin/checkout", "/bin/date", "/bin/discount", "/bin/id",
    "/bin/payments", "/bin/sql", "/bin/jq",
})
_EMPTY_FILE_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


def _jq_format_value(value: Any, raw: bool) -> str:
    """One output value as PowerTools-jq prints it: raw (-r) emits bare
    strings; everything else (and all non-strings) is compact JSON with
    object keys sorted, mirroring jq's default ordering."""
    if raw and isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _eval_jq_filter(doc: Any, filt: str) -> tuple[Optional[list], Optional[str]]:
    """Evaluate a PowerTools-OS /bin/jq filter against ``doc``.

    Returns ``(outputs, None)`` on success (``outputs`` is the list of
    emitted values — more than one when the filter iterates an array via
    ``[]``), or ``(None, error_message)`` on a hard failure (exit 1).

    Faithful to the restricted grammar probed on PROD 2026-05-30
    (artifacts/prod_explore/jq_probe2/3/4_*.json):

      SUPPORTED (path-extraction only):
        .            -> whole doc
        .a / .a.b.c  -> nested object field (missing key -> null)
        .a[i]        -> array index (out of range -> null)
        .a[]         -> iterate all elements (one output per element)
        .a[].b       -> field of every element
        keys         -> sorted top-level keys

      SILENTLY WRONG on PROD -> returns a single ``null`` (exit 0):
        pipes ``| length|type|tostring|...``, ``//`` default,
        comparisons ``> < == !=``.  Replicated so local A/B catches a
        prompt that wrongly relies on them (they look like they work).

      HARD ERROR (exit 1) on PROD:
        select(), has(), [..] collect, arithmetic + - *, string interp.
    """
    f = filt.strip()
    if f == ".":
        return [doc], None
    if f == "keys":
        if isinstance(doc, dict):
            return [sorted(doc.keys())], None
        return None, f'jq: cannot compute keys of {type(doc).__name__}'

    # PROD hard-error bucket (checked BEFORE the silent-null bucket: a
    # pipe whose left side iterates an array, or any function-call /
    # collect / interpolation / arithmetic, errors out rather than
    # silently nulling).
    pipe_left = f.split("|", 1)[0] if "|" in f else ""
    if (
        "(" in f                       # select(, has(, any function call
        or f.startswith("[")           # [..] collect
        or "\\(" in f                  # string interpolation
        or re.search(r"[+*]", f)       # arithmetic + *
        or re.search(r"\s-\s", f)      # arithmetic -
        or ("|" in f and "[]" in pipe_left)   # iterate-then-pipe
    ):
        return None, f'jq: unsupported filter "{filt}"'

    # PROD silent-null bucket: scalar pipe to a function (`.x | length`),
    # `//` default, comparisons — these parse but yield null on the
    # custom engine. Replicate (single null, exit 0) so they are not
    # mistaken for working extractions during local validation.
    if ("|" in f) or ("//" in f) or re.search(r"[<>]=?|==|!=", f):
        return [None], None

    # Pure path expression. Tokenise and walk.
    if not f.startswith("."):
        return None, f'jq: unsupported filter "{filt}"'
    pos = 0
    tokens: list[str] = []
    while pos < len(f):
        m = _JQ_TOKEN_RE.match(f, pos)
        if not m:
            return None, f'jq: unsupported path segment "{f[pos:]}"'
        tokens.append(m.group())
        pos = m.end()

    current: list[Any] = [doc]
    for tok in tokens:
        nxt: list[Any] = []
        if tok.startswith("."):
            key = tok[1:]
            for v in current:
                if isinstance(v, dict):
                    nxt.append(v.get(key))
                else:
                    return None, (
                        f'jq: cannot index non-object with field "{key}"'
                    )
        elif tok == "[]":
            for v in current:
                if isinstance(v, list):
                    nxt.extend(v)
                else:
                    return None, "jq: cannot iterate over non-array"
        else:  # [<int>]
            idx = int(tok[1:-1])
            for v in current:
                if isinstance(v, list):
                    nxt.append(v[idx] if 0 <= idx < len(v) else None)
                else:
                    return None, "jq: cannot index non-array"
        current = nxt
    return current, None


def _content_type_for(path: Path) -> str:
    """Best-effort MIME type matching PROD's taxonomy. Markdown is
    ``text/markdown`` (.md AND .MD); JSON files always ``application/
    json``; SQLite files (the catalogue) ``application/x-sqlite3``.
    Fallback ``application/octet-stream`` mirrors PROD."""
    suffix = path.suffix.lower()
    if suffix in _SQLITE_EXTS:
        return "application/x-sqlite3"
    return _CONTENT_TYPE_BY_EXT.get(suffix, "application/octet-stream")


def _is_text_like(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in {
        "application/json", "application/yaml", "application/toml",
        "application/xml", "application/sql",
    }


def _slice_lines(content: str, start_line: int, end_line: int) -> tuple[str, bool]:
    """Apply ECOM read.start_line / read.end_line slicing. Both bounds
    are 1-based inclusive; 0 means unbounded. Returns (sliced,
    truncated). ``truncated`` is True iff slicing dropped content."""
    if start_line <= 0 and end_line <= 0:
        return content, False
    lines = content.splitlines(keepends=True)
    n = len(lines)
    lo = max(start_line - 1, 0) if start_line > 0 else 0
    hi = min(end_line, n) if end_line > 0 else n
    if lo == 0 and hi == n:
        return content, False
    return "".join(lines[lo:hi]), True


def _prepend_line_numbers(content: str, start_index: int = 1) -> str:
    """Prefix each line with `<right-justified N><tab>` — matches the
    `cat -n` shape PROD emits when read.number=True. start_index lets
    sliced reads number from their slice's start_line."""
    lines = content.splitlines(keepends=True)
    if not lines:
        return content
    width = max(6, len(str(start_index + len(lines) - 1)))
    out = []
    for i, line in enumerate(lines):
        out.append(f"{(start_index + i):>{width}}\t{line}")
    return "".join(out)


def _format_sql_csv(cols: list[str], rows: list[Any]) -> str:
    """CSV-formatted SQL result rows with a header — what PROD's
    /bin/sql returns (text/csv). The PROD probe captured ``"n\\n10\\n"``
    for a single-column count; Python's csv module uses ``\\r\\n``
    line endings by default so we override to ``\\n`` to match."""
    if not cols:
        return ""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(cols)
    for row in rows:
        if hasattr(row, "keys"):
            writer.writerow([_format_cell(row[c]) for c in cols])
        else:
            writer.writerow([_format_cell(v) for v in row])
    return buf.getvalue()


def _format_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


# ---- The client ----


@dataclass(frozen=True)
class _CatalogueAccess:
    """Resolved /bin/sql configuration."""
    primary: Optional[Path]
    attached: tuple[Path, ...]


class LocalEcomClient:
    """Drop-in replacement for ``EcomRuntimeClientSync`` that reads
    from a local directory instead of making gRPC calls.

    Write / delete operations mutate the local snapshot so a verifier
    can inspect the final state. Every call appends to ``ops_log`` for
    test assertions and trace comparison.

    Parameters
    ----------
    workspace_root:
        Filesystem root that emulates the ECOM workspace. The runtime's
        view of "/" maps to this directory.
    context_date:
        Optional ISO8601 string surfaced by ``exec(/bin/date)`` so the
        agent can anchor relative-date arithmetic to a deterministic
        trial clock. Defaults to ``datetime.now(UTC)``. Replaces the
        retired ``context()`` RPC.
    actor_id:
        Identity descriptor surfaced by ``exec(/bin/id)`` on the
        ``user:`` line. PROD default across 42/62 scanned trials is
        ``"anonymous"``; remaining trials use ``cust_NNN`` / ``emp_NNN``
        ids per trial seed.
    roles:
        Comma-separated roles surfaced by ``exec(/bin/id)`` on the
        ``roles:`` line. PROD default is ``"GUEST"`` for anonymous;
        customers get ``"customer"``; employees get the long form
        ``"employee, store_manager, discount_manager, inventory_viewer,
        fulfillment_viewer, customer_service"``.
    sql_db_paths:
        Optional iterable of catalogue SQLite databases (relative to
        workspace_root) that ``/bin/sql`` should attach. If None,
        defaults to every ``*.db`` / ``*.sqlite`` / ``*.sqlite3`` file
        found at the workspace root or one level below.

    Environment overrides:
        ECOM_LOCAL_CASE_INSENSITIVE_SEARCH=1  — search ignores case
        ECOM_LOCAL_CASE_INSENSITIVE_FIND=1    — find ignores case
    """

    def __init__(
        self,
        workspace_root: str | Path,
        context_date: str | None = None,
        sql_db_paths: Optional[Iterable[str | Path]] = None,
        actor_id: str = "anonymous",
        roles: str = "GUEST",
    ) -> None:
        self._root = Path(workspace_root).resolve()
        if not self._root.exists():
            raise FileNotFoundError(f"Workspace root not found: {self._root}")
        self._context_date = context_date or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._actor_id = actor_id
        self._roles = roles
        sql_dbs = self._resolve_sql_dbs(sql_db_paths)
        self._sql_dbs = _CatalogueAccess(
            primary=sql_dbs[0] if sql_dbs else None,
            attached=tuple(sql_dbs[1:]),
        )

        self.ops_log: list[dict[str, Any]] = []
        self.reads: set[str] = set()
        self.writes: dict[str, str] = {}
        self.deletes: set[str] = set()

    # ---- public introspection ----

    @property
    def workspace_root(self) -> Path:
        return self._root

    def get_workspace_hash(self) -> str:
        """SHA-256 hash of all file contents — for snapshot diffing."""
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

    def tree(self, req: Any) -> "ecom_pb2.TreeResponse":
        root_path = getattr(req, "root", "") or "/"
        level = int(getattr(req, "level", 0) or 0)
        resolved = self._resolve(root_path)
        # PROD returns an RPC error (classified as UNKNOWN) for tree
        # against a missing path. Without this raise, local quietly
        # returned a FILE entry — masking the v0.1.93 addenda_completer
        # bug where /docs/<dir> dirs that don't exist in a draw look
        # the same locally as dirs that exist with content. Aligning
        # behavior closes that test gap.
        if not resolved.exists():
            raise FileNotFoundError(f"Path not found: {root_path}")

        def _walk(p: Path, depth: int) -> "ecom_pb2.TreeResponse.Entry":
            if p.is_dir():
                # PROD names the absolute root "/" and other dirs by basename.
                if p == self._root:
                    name = "/"
                else:
                    name = p.name
                entry = ecom_pb2.TreeResponse.Entry(
                    name=name, kind=ecom_pb2.NodeKind.NODE_KIND_DIR,
                )
                if level == 0 or depth < level:
                    for child in sorted(p.iterdir()):
                        entry.children.append(_walk(child, depth + 1))
                return entry
            return ecom_pb2.TreeResponse.Entry(
                name=p.name,
                kind=ecom_pb2.NodeKind.NODE_KIND_FILE,
                content_type=_content_type_for(p),
            )

        root_entry = _walk(resolved, depth=0)
        self.ops_log.append({"op": "tree", "root": root_path, "level": level})
        return ecom_pb2.TreeResponse(root=root_entry)

    def list(self, req: Any) -> "ecom_pb2.ListResponse":
        path = getattr(req, "path", "") or "/"
        resolved = self._resolve(path)
        if not resolved.exists() or not resolved.is_dir():
            raise FileNotFoundError(f"Directory not found: {path}")
        # PROD quirk (confirmed across all 31 trials of the 2026-05-15
        # scan): `list` echoes the request path *lowercased*. Other
        # RPCs (read, stat) preserve case; only `list` normalises.
        # Mirror this so the LLM sees the same `path` string locally.
        resp = ecom_pb2.ListResponse(path=path.lower())
        for child in sorted(resolved.iterdir()):
            if child.is_dir():
                resp.entries.append(ecom_pb2.ListResponse.Entry(
                    name=child.name,
                    path="/" + str(child.relative_to(self._root)),
                    kind=ecom_pb2.NodeKind.NODE_KIND_DIR,
                ))
            else:
                resp.entries.append(ecom_pb2.ListResponse.Entry(
                    name=child.name,
                    path="/" + str(child.relative_to(self._root)),
                    kind=ecom_pb2.NodeKind.NODE_KIND_FILE,
                    content_type=_content_type_for(child),
                ))
        self.ops_log.append({
            "op": "list", "path": path, "n": len(resp.entries),
        })
        return resp

    def read(self, req: Any) -> "ecom_pb2.ReadResponse":
        path = getattr(req, "path", "")
        start_line = int(getattr(req, "start_line", 0) or 0)
        end_line = int(getattr(req, "end_line", 0) or 0)
        number = bool(getattr(req, "number", False))
        resolved = self._resolve(path)
        if not resolved.exists() or resolved.is_dir():
            # Catalogue product files (/proc/catalog/.../<SKU>.json) are
            # not materialised on disk in db-backed snapshots — there can
            # be thousands. Synthesize them from the catalogue.db so the
            # agent reads/cites them exactly as on PROD (where they are
            # real files whose record_path lives in product_variants).
            synth = None
            if path.startswith("/proc/catalog/") and path.endswith(".json"):
                synth = self._synth_catalog_read(path)
            elif path.startswith("/proc/payments/") and path.endswith(".json"):
                synth = self._synth_row_read(
                    path, "payment_transactions", "payment_id")
            elif path.startswith("/proc/baskets/") and path.endswith(".json"):
                synth = self._synth_row_read(
                    path, "shopping_baskets", "basket_id")
            # BITGN_LOCAL_PROD_PATHS=1: also answer the PROD-namespaced
            # /proc paths (per prod AGENTS.MD) so prod fixes can be
            # A/B'd faithfully against dev snapshots. The snapshot db
            # stores record_path as the flat dev path, so the
            # record_path lookup in _synth_row_read misses and the
            # trailing-id-token fallback resolves the row — making this
            # mode path-agnostic (works regardless of nesting).
            elif (
                os.environ.get("BITGN_LOCAL_PROD_PATHS") == "1"
                and path.endswith(".json")
            ):
                if path.startswith("/proc/payment-ledger/"):
                    synth = self._synth_row_read(
                        path, "payment_transactions", "payment_id")
                elif path.startswith("/proc/carts/"):
                    synth = self._synth_row_read(
                        path, "shopping_baskets", "basket_id")
                elif path.startswith("/proc/locations/"):
                    synth = self._synth_row_read(
                        path, "stores", "store_id")
                elif path.startswith("/proc/staff/"):
                    synth = self._synth_row_read(
                        path, "employee_accounts", "employee_id")
                elif path.startswith("/proc/return-workflows/"):
                    synth = self._synth_row_read(
                        path, "return_requests", "return_id")
            if synth is not None:
                self.reads.add(path.lstrip("/"))
                self.ops_log.append({
                    "op": "read", "path": path, "bytes": len(synth),
                    "truncated": False, "synth_row": True,
                })
                return ecom_pb2.ReadResponse(
                    path=path, content_type="application/json",
                    content=synth,
                    sha256=hashlib.sha256(synth.encode("utf-8")).hexdigest(),
                )
            raise FileNotFoundError(f"File not found: {path}")

        # /bin/* are zero-byte executable stubs on PROD. `read` returns
        # {path, content_type, sha256} with NO `content` field and
        # sha256 = e3b0c442… (empty string SHA). Mirror that regardless
        # of what the local filesystem holds, so snapshot rebuilds that
        # copy non-empty stub scripts still produce PROD-shaped reads.
        if path in _BIN_STUB_PATHS:
            self.reads.add(path.lstrip("/"))
            self.ops_log.append({
                "op": "read", "path": path, "bytes": 0,
                "truncated": False, "bin_stub": True,
            })
            return ecom_pb2.ReadResponse(
                path=path,
                content_type="text/plain",
                sha256=_EMPTY_FILE_SHA256,
            )

        raw = resolved.read_text(encoding="utf-8", errors="replace")
        sliced, truncated = _slice_lines(raw, start_line, end_line)
        if number:
            sliced = _prepend_line_numbers(
                sliced, start_index=max(start_line, 1),
            )
        sha = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        self.reads.add(path.lstrip("/"))
        self.ops_log.append({
            "op": "read", "path": path,
            "bytes": len(sliced), "truncated": truncated,
        })
        resp = ecom_pb2.ReadResponse(
            path=path,
            content_type=_content_type_for(resolved),
            content=sliced,
            sha256=sha,
        )
        if truncated:
            resp.truncated = True
        return resp

    def _synth_catalog_read(self, path: str) -> Optional[str]:
        """Build a PROD-shaped catalogue product JSON from the db by
        record_path (falls back to matching the trailing <SKU> token).
        Returns the JSON string, or None if no matching variant exists.
        """
        if not self._sql_dbs.primary:
            return None
        sku_token = path.rsplit("/", 1)[-1][:-len(".json")]
        conn = None
        try:
            conn = sqlite3.connect(str(self._sql_dbs.primary))
            conn.row_factory = sqlite3.Row
            # tables may differ across worlds; guard each access
            cols = {r[1] for r in conn.execute("PRAGMA table_info(product_variants)")}
            if not cols:
                return None
            row = conn.execute(
                "SELECT * FROM product_variants WHERE record_path=? LIMIT 1", (path,)
            ).fetchone()
            if row is None and "product_sku" in cols:
                row = conn.execute(
                    "SELECT * FROM product_variants WHERE product_sku=? LIMIT 1",
                    (sku_token,),
                ).fetchone()
            if row is None:
                return None
            d = dict(row)
            props = {}
            try:
                for pr in conn.execute(
                    "SELECT property_key, property_value_text, property_value_number "
                    "FROM product_variant_properties WHERE product_sku=?",
                    (d.get("product_sku"),),
                ):
                    v = pr["property_value_text"]
                    if v in (None, ""):
                        v = pr["property_value_number"]
                    props[pr["property_key"]] = v
            except Exception:
                pass
            # merge any inline properties column (JSON) if present
            if not props and d.get("properties"):
                try:
                    props = json.loads(d["properties"])
                except Exception:
                    pass
            out = {
                "sku": d.get("product_sku"),
                "category_id": d.get("product_category_id"),
                "kind_id": d.get("product_kind_id"),
                "family_id": d.get("product_family_id"),
                "brand": d.get("brand"),
                "series": d.get("series"),
                "model": d.get("model"),
                "name": d.get("product_name"),
                "price_cents": d.get("price_cents"),
                "price_currency": d.get("price_currency"),
                "properties": props,
            }
            return json.dumps(out)
        except Exception:
            return None
        finally:
            if conn is not None:
                conn.close()

    def _synth_row_read(self, path: str, table: str, key_col: str) -> Optional[str]:
        """Synthesize a /proc/<ns>/<id>.json read from a db row (by
        record_path, then by the trailing id token against key_col).
        Used for payments/baskets which aren't materialised as files in
        db-backed snapshots. Returns the row as JSON, or None."""
        if not self._sql_dbs.primary:
            return None
        token = path.rsplit("/", 1)[-1][:-len(".json")]
        conn = None
        try:
            conn = sqlite3.connect(str(self._sql_dbs.primary))
            conn.row_factory = sqlite3.Row
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not cols:
                return None
            row = None
            if "record_path" in cols:
                row = conn.execute(
                    f"SELECT * FROM {table} WHERE record_path=? LIMIT 1", (path,)
                ).fetchone()
            if row is None:
                row = conn.execute(
                    f"SELECT * FROM {table} WHERE {key_col}=? LIMIT 1", (token,)
                ).fetchone()
            if row is None:
                return None
            d = {k: row[k] for k in row.keys()}
            # Reflect a discount applied this session via /bin/discount
            # so the agent's verify-after-write re-read of the basket
            # sees the mutation (PROD mutates; the local db is read-only).
            if table == "shopping_baskets":
                applied = getattr(self, "_applied_discounts", {}).get(
                    d.get("basket_id") or token
                )
                if applied:
                    d.update(applied)
            return json.dumps(d)
        except Exception:
            return None
        finally:
            if conn is not None:
                conn.close()

    def search(self, req: Any) -> "ecom_pb2.SearchResponse":
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
            return ecom_pb2.SearchResponse()

        resp = ecom_pb2.SearchResponse()
        truncated = False
        for filepath in sorted(resolved.rglob("*")):
            if filepath.is_dir():
                continue
            ct = _content_type_for(filepath)
            if not _is_text_like(ct):
                continue
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    resp.matches.append(ecom_pb2.SearchResponse.Match(
                        path="/" + str(filepath.relative_to(self._root)),
                        line=i,
                        line_text=line[:200],
                    ))
                    if len(resp.matches) >= limit:
                        truncated = True
                        break
            if len(resp.matches) >= limit:
                break

        if truncated:
            resp.truncated = True
        self.ops_log.append({
            "op": "search", "root": root, "pattern": pattern,
            "matches": len(resp.matches), "truncated": truncated,
        })
        return resp

    def find(self, req: Any) -> "ecom_pb2.FindResponse":
        """Substring match on file names. PROD ECOM find is case-
        sensitive substring; the local mock matches PROD by default.
        ``ECOM_LOCAL_CASE_INSENSITIVE_FIND=1`` enables lenient replay.

        ``kind`` (NodeKind enum) filters:
          NODE_KIND_FILE → files only
          NODE_KIND_DIR  → dirs only
          anything else  → both
        """
        root = getattr(req, "root", "") or "/"
        name = getattr(req, "name", "")
        kind = int(getattr(req, "kind", NODE_KIND_UNSPECIFIED) or 0)
        limit = int(getattr(req, "limit", 10) or 10)
        resolved = self._resolve(root)

        ci = bool(os.environ.get("ECOM_LOCAL_CASE_INSENSITIVE_FIND"))
        needle = name.lower() if ci else name

        resp = ecom_pb2.FindResponse()
        truncated = False
        for filepath in sorted(resolved.rglob("*")):
            if kind == NODE_KIND_FILE and filepath.is_dir():
                continue
            if kind == NODE_KIND_DIR and not filepath.is_dir():
                continue
            haystack = filepath.name.lower() if ci else filepath.name
            if needle in haystack:
                resp.paths.append("/" + str(filepath.relative_to(self._root)))
                if len(resp.paths) >= limit:
                    truncated = True
                    break

        if truncated:
            resp.truncated = True
        self.ops_log.append({
            "op": "find", "root": root, "name": name,
            "kind": kind, "results": len(resp.paths),
        })
        return resp

    def stat(self, req: Any) -> "ecom_pb2.StatResponse":
        """Stat mirrors PROD's omit-empty behavior: directories carry
        only ``path`` + ``kind``, files add ``content_type``. The
        ``writable`` field is left unset so MessageToJson omits it —
        matching the PROD probe shape."""
        path = getattr(req, "path", "")
        resolved = self._resolve(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Path not found: {path}")
        if resolved.is_dir():
            resp = ecom_pb2.StatResponse(
                path=path, kind=ecom_pb2.NodeKind.NODE_KIND_DIR,
            )
        else:
            resp = ecom_pb2.StatResponse(
                path=path,
                kind=ecom_pb2.NodeKind.NODE_KIND_FILE,
                content_type=_content_type_for(resolved),
            )
        self.ops_log.append({"op": "stat", "path": path, "kind": resp.kind})
        return resp

    def exec(self, req: Any) -> "ecom_pb2.ExecResponse":
        """Local exec — mirrors the post-freeze ECOM /bin inventory.

        Stdout shapes / exit codes confirmed across 62 scanner trials
        (2026-05-15, scans run1+run2):

          /bin/sql                — SELECT returns CSV on stdout
                                    (header row + data rows). Errors
                                    return {exit_code=1, stderr}.
                                    Dot-commands (.schema/.tables) are
                                    NOT supported on PROD — they
                                    surface as SQL syntax errors. Use
                                    `SELECT … FROM sqlite_schema …`
                                    instead.
          /bin/id                 — `"user: <id>\\nroles: <role>\\n"`
                                    (two newline-terminated lines).
          /bin/date               — ISO8601 UTC stamp, newline-term.
          /bin/checkout (no args) — exit 1, stderr =
                                    "checkout: expected exactly one
                                    basket id\\n".
          /bin/checkout <basket>  — locally: no-op success (empty
                                    response) since cart mutation is
                                    snapshot-specific. PROD performs
                                    the real checkout.
          /bin/discount (no args) — exit 1, stderr =
                                    "discount: expected basket id,
                                    percent, reason code, and issuer
                                    id\\n".
          /bin/payments (no args) — exit 1, stderr =
                                    "payments: expected subcommand\\n".
          /bin/payments recover-3ds <id>
                                  — stdout =
                                    "3ds_recovery_started <id>\\n"
                                    (confirmed in baseline dump).

        Any unknown /bin/* path returns exit_code=127 with stderr
        explaining the local limitation.
        """
        path = getattr(req, "path", "")
        args = list(getattr(req, "args", []) or [])
        stdin = getattr(req, "stdin", "") or ""
        if path in _SQL_BIN_PATHS:
            return self._exec_sql(args=args, stdin=stdin)
        if path in _ID_BIN_PATHS:
            return self._exec_id()
        if path in _DATE_BIN_PATHS:
            return self._exec_date()
        if path in _CHECKOUT_BIN_PATHS:
            return self._exec_checkout(args=args, stdin=stdin)
        if path in _DISCOUNT_BIN_PATHS:
            return self._exec_discount(args=args, stdin=stdin)
        if path in _PAYMENTS_BIN_PATHS:
            return self._exec_payments(args=args, stdin=stdin)
        if path in _JQ_BIN_PATHS:
            return self._exec_jq(args=args, stdin=stdin)

        self.ops_log.append({
            "op": "exec", "path": path, "args": args, "exit_code": 127,
        })
        return ecom_pb2.ExecResponse(
            exit_code=127,
            stderr=(
                f"local mock: exec {path!r} not supported. "
                "Implemented bins: /bin/sql, /bin/id, /bin/date, "
                "/bin/checkout, /bin/discount, /bin/payments, /bin/jq. "
                "Run this task against a real ECOM VM for unmodelled "
                "binaries."
            ),
        )

    def _exec_id(self) -> "ecom_pb2.ExecResponse":
        """Mirror PROD `/bin/id`: two newline-terminated lines —
        `user: <actor>\\nroles: <roles>\\n`. PROD default across the
        2026-05-15 scan (42/62 trials) was actor=anonymous, role=GUEST;
        the remaining trials surfaced customer/employee identities
        derived from the trial seed."""
        stdout = f"user: {self._actor_id}\nroles: {self._roles}\n"
        self.ops_log.append({"op": "exec", "path": "/bin/id", "exit_code": 0})
        return ecom_pb2.ExecResponse(stdout=stdout)

    def _exec_date(self) -> "ecom_pb2.ExecResponse":
        """Mirror PROD `/bin/date`: ISO8601 UTC stamp on stdout with a
        trailing newline. Trial clock is deterministic per snapshot via
        the constructor's ``context_date``."""
        stdout = f"{self._context_date}\n"
        self.ops_log.append({"op": "exec", "path": "/bin/date", "exit_code": 0})
        return ecom_pb2.ExecResponse(stdout=stdout)

    def _exec_checkout(
        self, *, args: list[str], stdin: str,
    ) -> "ecom_pb2.ExecResponse":
        """`/bin/checkout` — PROD-aligned argument parsing.

        - No args            → exit 1, PROD's verbatim stderr.
        - One arg (basket id) → empty success ({} on the wire). PROD
                               actually mutates basket state; the local
                               mock can't faithfully model that without
                               per-snapshot cart logic, so we return
                               success-shaped empty response and let
                               the snapshot's grading rules (e.g.
                               forbidden_refs / mutation_count probes)
                               catch wrong behaviour.
        """
        self.ops_log.append({
            "op": "exec", "path": "/bin/checkout", "args": args,
            "exit_code": 1 if not args else 0,
        })
        if not args:
            return ecom_pb2.ExecResponse(
                exit_code=1,
                stderr="checkout: expected exactly one basket id\n",
            )
        # PROD on success: empty response (no stdout/stderr/exit_code).
        # `ExecResponse()` serialises to `{}` exactly like PROD.
        return ecom_pb2.ExecResponse()

    def _exec_discount(
        self, *, args: list[str], stdin: str,
    ) -> "ecom_pb2.ExecResponse":
        """`/bin/discount` — PROD-aligned. Requires basket id +
        percent + reason code + issuer id. With fewer than 4 args
        PROD emits the verbatim stderr below; with a full arg set
        PROD mutates discount state which we cannot model locally —
        return empty success so the agent's *decision* (whether to
        run discount under /docs/discounts.md) is what the local A/B
        measures, not the binary's side-effect."""
        self.ops_log.append({
            "op": "exec", "path": "/bin/discount", "args": args,
            "exit_code": 1 if len(args) < 4 else 0,
        })
        if len(args) < 4:
            return ecom_pb2.ExecResponse(
                exit_code=1,
                stderr=(
                    "discount: expected basket id, percent, reason "
                    "code, and issuer id\n"
                ),
            )
        # Record the applied discount so a subsequent basket re-read
        # reflects it — PROD mutates discount state; without modelling
        # this the agent's verify-after-write re-read sees an empty
        # discount and falsely refuses NONE_UNSUPPORTED (local t26
        # apply-case). args = [basket_id, percent, reason_code, issuer].
        basket_id, percent, reason, issuer = args[0], args[1], args[2], args[3]
        if not hasattr(self, "_applied_discounts"):
            self._applied_discounts = {}
        self._applied_discounts[basket_id] = {
            "discount_percent": percent,
            "discount_reason_code": reason,
            "discount_issuer_employee_id": issuer,
        }
        return ecom_pb2.ExecResponse(
            stdout=f"discount_applied {basket_id} {percent}\n"
        )

    def _exec_payments(
        self, *, args: list[str], stdin: str,
    ) -> "ecom_pb2.ExecResponse":
        """`/bin/payments` — PROD-aligned subcommand router.

        - No args                       → exit 1, PROD's stderr.
        - recover-3ds <payment_id>     → stdout =
                                          "3ds_recovery_started <id>\\n"
                                          (verbatim PROD shape, baseline
                                          dump 2026-05-15 captured this
                                          exactly for two calls).
        - other subcommands             → empty success (no PROD data
                                          captured for those yet; expand
                                          when scanner exercises them).
        """
        self.ops_log.append({
            "op": "exec", "path": "/bin/payments", "args": args,
        })
        if not args:
            return ecom_pb2.ExecResponse(
                exit_code=1,
                stderr="payments: expected subcommand\n",
            )
        if args[0] == "recover-3ds" and len(args) >= 2:
            payment_id = args[1]
            return ecom_pb2.ExecResponse(
                stdout=f"3ds_recovery_started {payment_id}\n",
            )
        return ecom_pb2.ExecResponse()

    def _exec_jq(self, *, args: list[str], stdin: str) -> "ecom_pb2.ExecResponse":
        """`/bin/jq` — the custom "PowerTools E-Commerce OS jq".

        PROD contract (probed live 2026-05-30, see _eval_jq_filter and
        artifacts/prod_explore/jq_probe*.json):

          - Invocation: ``jq [-r|--raw-output] <filter> [path|-]``.
            JSON read from the path arg (``-`` or absent => stdin).
          - stdout ALWAYS begins with the banner line, then the result.
          - ONLY -r / --raw-output supported; any other flag is rejected.
          - Restricted path-extraction grammar (no pipes/functions/ops).

        This local mock mirrors that exactly so prompt guidance that uses
        /bin/jq can be A/B-validated faithfully before a PROD run. It is
        deliberately *not* a passthrough to the host's real jq, which is
        far more capable and would mask filters that fail on PROD.
        """
        self.ops_log.append({"op": "exec", "path": "/bin/jq", "args": args})

        raw = False
        positionals: list[str] = []
        for a in args:
            if a in ("-r", "--raw-output"):
                raw = True
            elif a == "-":
                positionals.append(a)
            elif a.startswith("-") and a != "-":
                return ecom_pb2.ExecResponse(
                    exit_code=1, stdout=_JQ_BANNER,
                    stderr=f"flag provided but not defined: {a}\n{_JQ_USAGE}",
                )
            else:
                positionals.append(a)

        if not positionals:
            return ecom_pb2.ExecResponse(
                exit_code=1, stdout=_JQ_BANNER, stderr="jq: expected filter\n",
            )
        filt = positionals[0]
        path_arg = positionals[1] if len(positionals) > 1 else None

        if path_arg and path_arg != "-":
            try:
                content = getattr(
                    self.read(ecom_pb2.ReadRequest(path=path_arg)), "content", ""
                ) or ""
            except Exception:
                return ecom_pb2.ExecResponse(
                    exit_code=1, stdout=_JQ_BANNER,
                    stderr=f"jq: error: could not open {path_arg}\n",
                )
        else:
            content = stdin

        try:
            doc = json.loads(content)
        except (ValueError, TypeError):
            return ecom_pb2.ExecResponse(
                exit_code=1, stdout=_JQ_BANNER,
                stderr="jq: error: input is not valid JSON\n",
            )

        outputs, err = _eval_jq_filter(doc, filt)
        if err is not None:
            return ecom_pb2.ExecResponse(
                exit_code=1, stdout=_JQ_BANNER, stderr=err + "\n",
            )
        body = "".join(_jq_format_value(o, raw) + "\n" for o in (outputs or []))
        return ecom_pb2.ExecResponse(stdout=_JQ_BANNER + body)

    def _exec_sql(self, *, args: list[str], stdin: str) -> "ecom_pb2.ExecResponse":
        """Run the stdin SQL body against the workspace's SQLite
        catalogues. Supports ``.schema`` for DDL inspection and
        arbitrary SELECT/UPDATE/INSERT via sqlite3.

        Post-freeze ``ExecResponse`` no longer carries a content_type
        field (reserved in the proto). The CSV-vs-error distinction
        now lives only in shape:

          success  → {stdout: "col1,col2\\nval,val\\n..."}
          failure  → {exit_code: 1, stderr: "<sqlite error>\\n"}

        Multiple catalogue files are attached as ``db1``/``db2``/...
        so a query can join across them without the agent needing to
        know which file holds which table.

        Dot-commands (``.schema``, ``.tables``, etc.) are NOT
        supported by PROD — they surface as SQL syntax errors,
        matching real sqlite CLI behaviour when dot-commands are
        fed via stdin. Agents are expected to use SQL queries
        against the ``sqlite_schema`` virtual table instead:
            SELECT name, sql FROM sqlite_schema WHERE type='table';

        BITGN_LOCAL_SQL_UNAVAILABLE=1: emulate PROD's no-/bin/sql state
        (data lives in filesystem, not a SQL cluster). Returns exit_code=1
        with a PROD-shaped "sql backend unavailable" error so the agent's
        filesystem-fallback paths are exercised locally. Default (flag
        unset) = normal SQLite-backed behaviour.
        """
        if os.environ.get("BITGN_LOCAL_SQL_UNAVAILABLE") == "1":
            self.ops_log.append({
                "op": "exec", "path": "/bin/sql", "args": args,
                "exit_code": 1, "rejected": "sql_unavailable",
            })
            return ecom_pb2.ExecResponse(
                exit_code=1,
                stderr=(
                    "sql backend unavailable: no SQL cluster reachable "
                    "in this environment; use filesystem paths under "
                    "/proc/ instead\n"
                ),
            )

        body = stdin.strip()
        # PROD /bin/sql rejects sqlite dot-commands the same way the
        # raw sqlite engine does when fed `.schema` as if it were SQL.
        # Confirmed across 62 scanner trials (2026-05-15) — both
        # `.schema` and `.tables` returned exit_code=1, stderr =
        # "SQL logic error: near \".\": syntax error (1)\n".
        # This rejection fires BEFORE the catalogue presence check so
        # the wire shape matches PROD even on snapshots with no DB.
        if body.startswith("."):
            self.ops_log.append({
                "op": "exec", "path": "/bin/sql", "args": args,
                "exit_code": 1, "rejected": "dot_command",
            })
            return ecom_pb2.ExecResponse(
                exit_code=1,
                stderr='SQL logic error: near ".": syntax error (1)\n',
            )

        if not self._sql_dbs.primary:
            return ecom_pb2.ExecResponse(
                exit_code=2,
                stderr=(
                    "local /bin/sql: no SQLite catalogues found in this "
                    "workspace. Drop a *.db or *.sqlite file under the "
                    "root, or pass `sql_db_paths=...` to LocalEcomClient."
                ),
            )

        if not body:
            return ecom_pb2.ExecResponse(
                exit_code=2,
                stderr="local /bin/sql: empty SQL body on stdin",
            )

        conn = None
        try:
            conn = sqlite3.connect(str(self._sql_dbs.primary))
            for i, db in enumerate(self._sql_dbs.attached, start=1):
                conn.execute(
                    "ATTACH DATABASE ? AS ?", (str(db), f"db{i}")
                )
            conn.row_factory = sqlite3.Row

            cursor = conn.execute(body)
            rows = cursor.fetchall()
            cols = (
                [c[0] for c in cursor.description]
                if cursor.description else []
            )
            stdout = _format_sql_csv(cols, rows)
            self.ops_log.append({
                "op": "exec", "path": "/bin/sql", "args": args,
                "rows": len(rows), "exit_code": 0,
            })
            return ecom_pb2.ExecResponse(stdout=stdout)
        except sqlite3.Error as exc:
            self.ops_log.append({
                "op": "exec", "path": "/bin/sql", "args": args,
                "exit_code": 1, "error": str(exc),
            })
            # PROD's stderr format for sqlite errors is
            # "SQL logic error: <message> (<code>)\n" — matching the
            # raw sqlite shell output. Python's sqlite3.Error message
            # uses "<message>" without the framing; we wrap it so the
            # local stderr text matches PROD's shape closely.
            msg = str(exc)
            if not msg.startswith("SQL logic error"):
                msg = f"SQL logic error: {msg} (1)"
            if not msg.endswith("\n"):
                msg += "\n"
            return ecom_pb2.ExecResponse(exit_code=1, stderr=msg)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def write(self, req: Any) -> "ecom_pb2.WriteResponse":
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
        return ecom_pb2.WriteResponse(path=path)

    def delete(self, req: Any) -> "ecom_pb2.DeleteResponse":
        path = getattr(req, "path", "")
        resolved = self._resolve(path)
        if resolved.is_dir():
            raise IsADirectoryError(f"Cannot delete directory: {path}")
        if resolved.exists():
            resolved.unlink()
        self.deletes.add(path.lstrip("/"))
        self.ops_log.append({"op": "delete", "path": path})
        return ecom_pb2.DeleteResponse()

    def answer(self, req: Any) -> "ecom_pb2.AnswerResponse":
        self.ops_log.append({
            "op": "answer",
            "message": getattr(req, "message", ""),
            "outcome": int(getattr(req, "outcome", 0) or 0),
            "refs": list(getattr(req, "refs", []) or []),
        })
        return ecom_pb2.AnswerResponse()

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
        out: list[Path] = []
        for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
            out.extend(sorted(self._root.glob(pattern)))
            out.extend(sorted(self._root.glob(f"*/{pattern}")))
        return out
