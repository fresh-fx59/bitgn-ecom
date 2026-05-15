"""TracingEcomClient — a proxy around EcomRuntimeClientSync that writes
one `ecom_op` trace record per runtime call.

Naming note: kept as `TracingEcomClient` / `ecom_op` for log-format
continuity with the PAC1 lineage (so existing dashboards and
`jq` queries keep working). The wrapped runtime is ECOM.

Motivation: the BitGN dashboard's "steps" metric counts runtime
ops (list/read/tree/find/search/stat/exec/write/...), not LLM
iterations or high-level tool calls. Until we logged this layer,
reconciling the dashboard against a local trace required shuttling
screenshots or pastebins. With this wrapper, the local JSONL trace
contains the same ops in the same order, so
`jq 'select(.kind=="ecom_op")' trace.jsonl` gives you the dashboard
view verbatim.

Wrapping the runtime (not the adapter) is load-bearing: anything that
takes the runtime directly (e.g. helper modules, fan-out scripts)
makes raw `client.list()` / `client.read()` calls that bypass
EcomAdapter.dispatch. Tracing at the adapter would miss those.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from bitgn.vm.ecom import ecom_pb2

try:
    from google.protobuf.json_format import MessageToJson
except ImportError:
    MessageToJson = None  # type: ignore

# Gate for search() case-fold retry: a single lowercase alphanumeric
# token (letters, digits, `_`, `-`). Patterns with regex metacharacters,
# whitespace, or mixed case are left alone — we only retry proper-noun-
# shaped LLM queries fed in lowercase against title-cased workspace
# content (e.g. "badger" vs "Badger").
_LOWER_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


# Phase attribution for ecom_op records. The agent loop sets this around
# each logical phase (prepass, step:N) so every op the underlying
# EcomRuntimeClientSync sees inherits the label — including ops made by
# preflight_* tools that call the runtime directly. "routed_preflight"
# is a historical label present in older log files but no longer emitted.
_ecom_op_origin: ContextVar[Optional[str]] = ContextVar(
    "ecom_op_origin", default=None,
)


@contextmanager
def ecom_origin(label: str) -> Iterator[None]:
    """Attribute all ecom_op records emitted in this block to `label`.

    Nests cleanly via contextvars — resetting on exit restores whatever
    the outer scope had set. Thread-safe because ContextVar is per-task
    in asyncio and copied into new threads at fork time (not relevant
    here since the agent is synchronous, but stated for the record).
    """
    token = _ecom_op_origin.set(label)
    try:
        yield
    finally:
        _ecom_op_origin.reset(token)


def set_ecom_origin(label: str) -> None:
    """Set the origin label for subsequent ecom_op emissions until the
    next call (or the end of the current Context). Use this when the
    code structure doesn't cleanly fit a `with` block — e.g. inside a
    big agent-loop iteration where re-indenting the body would churn
    300 lines. Each iteration overwrites before any op fires, so
    attribution is still precise per-step. The final value leaks to
    whatever runs after, which is fine as long as no ECOM ops fire
    post-loop."""
    _ecom_op_origin.set(label)


def origin_bucket(origin: Optional[str]) -> str:
    """Collapse fine-grained origin labels into summary buckets.

    `step:1`, `step:2`, ..., `step:N` all map to "step" so cross-task
    aggregates compare apples to apples — otherwise a 15-step task has
    15 origin keys and a 3-step task has 3, making
    `tasks[*].ecom_ops_by_origin` awkward to roll up.

    `None` maps to "other" so traces from before attribution landed
    (or off-path code that forgets to set origin) still account for
    their ops rather than vanishing from the bucket breakdown.

    This function is the canonical bucketing rule — both bench_summary
    and failure_report import it so their origin categories always
    agree.
    """
    if origin is None:
        return "other"
    if origin.startswith("step:"):
        return "step"
    return origin


def _response_bytes(resp: Any) -> int:
    """Wire-byte size of a proto response. Matches how the dashboard
    would measure payload size. Returns 0 on non-proto objects."""
    try:
        return int(resp.ByteSize())
    except Exception:
        return 0


_RAW_DUMP_LOCK = threading.Lock()
_RAW_DUMP_PATH: Optional[Path] = None


def _raw_dump_path() -> Optional[Path]:
    """Resolve the per-process raw-response dump file lazily.

    Returns None when capture is disabled. When enabled, all calls in
    this process append to the same file so cross-thread interleaving is
    serialized (file open is JSON-line safe under the lock).
    """
    global _RAW_DUMP_PATH
    if not os.environ.get("BITGN_TRACE_RAW_RESPONSES"):
        return None
    if _RAW_DUMP_PATH is not None:
        return _RAW_DUMP_PATH
    base = os.environ.get("BITGN_TRACE_RAW_DIR", "logs/raw_responses")
    Path(base).mkdir(parents=True, exist_ok=True)
    _RAW_DUMP_PATH = Path(base) / f"ecom_responses.{os.getpid()}.jsonl"
    return _RAW_DUMP_PATH


def _proto_to_dict(msg: Any) -> Any:
    """Best-effort serialization that works for protobuf and the local
    duck-typed response wrappers. Returns a JSON-safe dict, or None when
    the object can't be serialized."""
    if MessageToJson is not None:
        try:
            return json.loads(
                MessageToJson(msg, preserving_proto_field_name=True)
            )
        except Exception:
            pass
    try:
        return {k: getattr(msg, k) for k in getattr(msg, "__dataclass_fields__", {})}
    except Exception:
        return None


def _dump_raw(op: str, req: Any, resp: Any, *, ok: bool, wall_ms: int,
              error_code: Optional[str]) -> None:
    """Append a single raw request/response record to the per-process
    dump file. Best-effort: any exception is swallowed so capture
    failures never mask a real runtime error.
    """
    path = _raw_dump_path()
    if path is None:
        return
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "ok": ok,
            "wall_ms": wall_ms,
            "error_code": error_code,
            "request": _proto_to_dict(req) if req is not None else None,
            "response": _proto_to_dict(resp) if resp is not None else None,
            "origin": _ecom_op_origin.get(),
        }
        line = json.dumps(record, default=str) + "\n"
        with _RAW_DUMP_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _classify_exception(exc: BaseException) -> str:
    """Same buckets as EcomAdapter._classify_exception — kept local so
    the wrapper has no circular import on the adapter."""
    name = type(exc).__name__
    if "Deadline" in name or "Timeout" in name:
        return "RPC_DEADLINE"
    if "Unavailable" in name or "Connection" in name:
        return "RPC_UNAVAILABLE"
    if "InvalidArgument" in name or isinstance(exc, (TypeError, ValueError)):
        return "INVALID_ARG"
    if "EcomError" in name:
        return "RUNTIME_ERROR"
    return "UNKNOWN"


# Map request proto type → (op label, attribute to extract as `path`).
_REQUEST_PATH_ATTR: dict[type, tuple[str, Optional[str]]] = {
    ecom_pb2.ReadRequest: ("read", "path"),
    ecom_pb2.WriteRequest: ("write", "path"),
    ecom_pb2.DeleteRequest: ("delete", "path"),
    ecom_pb2.ListRequest: ("list", "path"),
    ecom_pb2.TreeRequest: ("tree", "root"),
    ecom_pb2.FindRequest: ("find", "root"),
    ecom_pb2.SearchRequest: ("search", "root"),
    ecom_pb2.StatRequest: ("stat", "path"),
    ecom_pb2.ExecRequest: ("exec", "path"),
    ecom_pb2.AnswerRequest: ("answer", None),
}


class TracingEcomClient:
    """Drop-in replacement for `EcomRuntimeClientSync` that records
    every call to a `TraceWriter`. Methods mirror the underlying
    client; unknown attributes are delegated verbatim so future ECOM
    methods work without a wrapper update (they just won't be traced).
    """

    def __init__(self, runtime: Any, *, writer: Any = None) -> None:
        self._runtime = runtime
        self._writer = writer

    def set_writer(self, writer: Any) -> None:
        """Attach a writer after construction. Ops dispatched before
        a writer is attached are silently not traced — the caller is
        responsible for wiring early enough. Used when the writer
        depends on task_id which is only known after start_trial."""
        self._writer = writer

    # -- traced proxies --------------------------------------------------

    def read(self, req: "ecom_pb2.ReadRequest") -> Any:
        return self._traced(req, self._runtime.read)

    def write(self, req: "ecom_pb2.WriteRequest") -> Any:
        return self._traced(req, self._runtime.write)

    def delete(self, req: "ecom_pb2.DeleteRequest") -> Any:
        return self._traced(req, self._runtime.delete)

    def list(self, req: "ecom_pb2.ListRequest") -> Any:
        return self._traced(req, self._runtime.list)

    def tree(self, req: "ecom_pb2.TreeRequest") -> Any:
        return self._traced(req, self._runtime.tree)

    def find(self, req: "ecom_pb2.FindRequest") -> Any:
        return self._traced(req, self._runtime.find)

    def stat(self, req: "ecom_pb2.StatRequest") -> Any:
        return self._traced(req, self._runtime.stat)

    def exec(self, req: "ecom_pb2.ExecRequest") -> Any:
        return self._traced(req, self._runtime.exec)

    def search(self, req: "ecom_pb2.SearchRequest") -> Any:
        resp = self._traced(req, self._runtime.search)
        # PROD search is case-sensitive substring match. Agents often
        # feed entity aliases in lowercase ("badger") while workspace
        # content is title-cased ("Badger"), producing zero-hit false
        # negatives that read as "no evidence exists". If the first pass
        # returned nothing and the pattern is a single lowercase proper-
        # noun-shaped token, retry once with Title case. Both probes
        # appear in the trace, so observability is preserved.
        pattern = getattr(req, "pattern", "") or ""
        if list(getattr(resp, "matches", []) or []):
            return resp
        if not _LOWER_TOKEN_RE.match(pattern):
            return resp
        titled = pattern[:1].upper() + pattern[1:]
        if hasattr(req, "model_copy"):
            # pydantic BaseModel (agent-facing Req_Search)
            retry_req = req.model_copy(update={"pattern": titled})
        elif hasattr(req, "CopyFrom"):
            # protobuf SearchRequest — PROD runtime path
            retry_req = type(req)()
            retry_req.CopyFrom(req)
            retry_req.pattern = titled
        else:
            retry_req = type(req)()
            for attr in ("root", "pattern", "limit"):
                if hasattr(req, attr):
                    setattr(retry_req, attr, getattr(req, attr))
            retry_req.pattern = titled
        retry_resp = self._traced(retry_req, self._runtime.search)
        return retry_resp if list(getattr(retry_resp, "matches", []) or []) else resp

    def answer(self, req: "ecom_pb2.AnswerRequest") -> Any:
        return self._traced(req, self._runtime.answer)

    # -- unknown method passthrough --------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Delegate any attribute we don't explicitly wrap. Do NOT use
        for `_runtime`/`_writer` — those are set in __init__ and hit
        __getattribute__ first."""
        return getattr(self._runtime, name)

    # -- internals -------------------------------------------------------

    def _traced(
        self,
        req: Any,
        method: Any,
        *,
        op: Optional[str] = None,
        path: Optional[str] = None,
    ) -> Any:
        resolved_op, resolved_path = self._resolve(req, op, path)
        start = time.monotonic()
        try:
            resp = method(req)
        except BaseException as exc:
            wall_ms = int((time.monotonic() - start) * 1000)
            ec = _classify_exception(exc)
            self._emit(
                op=resolved_op,
                path=resolved_path,
                bytes_=0,
                wall_ms=wall_ms,
                ok=False,
                error_code=ec,
            )
            _dump_raw(resolved_op, req, None, ok=False,
                      wall_ms=wall_ms, error_code=ec)
            raise
        wall_ms = int((time.monotonic() - start) * 1000)
        self._emit(
            op=resolved_op,
            path=resolved_path,
            bytes_=_response_bytes(resp),
            wall_ms=wall_ms,
            ok=True,
            error_code=None,
        )
        _dump_raw(resolved_op, req, resp, ok=True,
                  wall_ms=wall_ms, error_code=None)
        return resp

    def _resolve(
        self, req: Any, op: Optional[str], path: Optional[str],
    ) -> tuple[str, Optional[str]]:
        if op is not None:
            return op, path
        entry = _REQUEST_PATH_ATTR.get(type(req))
        if entry is None:
            return type(req).__name__, path
        op_label, path_attr = entry
        if path_attr is None:
            return op_label, path
        return op_label, getattr(req, path_attr, None) or None

    def _emit(
        self,
        *,
        op: str,
        path: Optional[str],
        bytes_: int,
        wall_ms: int,
        ok: bool,
        error_code: Optional[str],
    ) -> None:
        w = self._writer
        if w is None:
            return
        try:
            w.append_ecom_op(
                op=op,
                path=path,
                bytes=bytes_,
                wall_ms=wall_ms,
                ok=ok,
                error_code=error_code,
                origin=_ecom_op_origin.get(),
            )
        except Exception:
            # Tracing must never mask a real runtime error. Drop silently
            # if the writer is closed or raises.
            pass
