"""Single-class adapter between Pydantic Req_* models and the official
bitgn PcmRuntimeClientSync. Every other layer is adapter-agnostic.

The adapter is the ONLY place in the project that imports bitgn.vm.pcm_pb2
or bitgn.vm.pcm_connect. Anywhere else that references bitgn is a smell
to be fixed.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Sequence, Tuple

from bitgn.vm import pcm_pb2
from bitgn.vm.pcm_connect import PcmRuntimeClientSync

from bitgn_contest_agent.schemas import (
    NextStep,  # noqa: F401 — used by T10 type hints
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
    Req_PreflightSchema,
    Req_PreflightSemanticIndex,
)

from bitgn_contest_agent.arch_constants import ArchCategory
from bitgn_contest_agent.arch_log import emit_arch
from bitgn_contest_agent.format_validator import validate_yaml_frontmatter

_LOG = logging.getLogger(__name__)

_OPT_A_CASE_INSENSITIVE = os.getenv("BITGN_OPT_A_CASE_INSENSITIVE", "") == "1"
_OPT_A_FIND_CI = os.getenv("BITGN_OPT_A_FIND_CI", "") == "1"

_CI_PREFIX_RE = re.compile(r"^\(\?[a-zA-Z]*i[a-zA-Z]*\)")


def _maybe_rewrite_ci(pattern: str) -> str:
    """Prefix `(?i)` to make the pattern case-insensitive, unless the
    pattern already enables the `i` inline flag at the start. Used by the
    Option-A experiment to absorb LLM forgetfulness around `rg -i`.
    """
    if not pattern:
        return pattern
    if _CI_PREFIX_RE.match(pattern):
        return pattern
    return "(?i)" + pattern


def _find_ci_variants(name: str) -> list[str]:
    """Generate cased variants of a find `name` to fan-out for CI find.

    PROD `find` is case-sensitive substring; this helper produces the
    minimum set of variants that, when unioned, approximate
    case-insensitive matching for ASCII names. Order is stable so the
    "original" variant gets dispatched first.
    """
    if not name:
        return [name]
    seen: dict[str, None] = {}
    for v in (name, name.lower(), name.upper(), name.title(), name.capitalize()):
        if v not in seen:
            seen[v] = None
    return list(seen.keys())


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


@dataclass(frozen=True, slots=True)
class PrepassResult:
    """Return shape of `PcmAdapter.run_prepass`.

    `bootstrap_content` is the list of strings the agent loop appends as
    additional user messages (today: only the WORKSPACE SCHEMA payload).
    `schema` is the typed parse of the `preflight_schema` JSON envelope —
    consumed by the routed-preflight dispatcher to fill root args. On
    any preflight_schema failure the schema is the empty WorkspaceSchema.
    """
    bootstrap_content: list[str]
    schema: "WorkspaceSchema"


def _response_to_text(resp: Any) -> str:
    """Extract a printable representation of any pcm_pb2 response.

    Generated proto messages are not JSON-serializable out of the box, so
    we use the protobuf MessageToJson helper + a plain string fallback.

    Special case: `SearchResponse` gets a `total_matches` field stamped
    at the very top of the JSON body — ahead of the `matches` array —
    so counting tasks survive response truncation. `_finish` may still
    cut the tail of the matches list, but the count is written in the
    first ~30 bytes and cannot be lost.
    """
    try:
        if isinstance(resp, pcm_pb2.SearchResponse):
            return _search_response_to_text(resp)
        from google.protobuf.json_format import MessageToJson

        return MessageToJson(resp, preserving_proto_field_name=True, indent=None)
    except Exception:
        return str(resp)


def _search_response_to_text(resp: "pcm_pb2.SearchResponse") -> str:
    """Serialize a SearchResponse with a truncation-proof total_matches header.

    The canonical `MessageToJson` shape is `{"matches": [...]}` which
    buries the count behind an arbitrarily long array. We invert it:
    `{"total_matches": N, "matches": [...]}`. The count is an exact
    lower-bound (equal to `len(resp.matches)` at the moment the adapter
    received the response) — it is exact when the caller's `limit` was
    not reached and a lower bound when it was. Client code should treat
    `total_matches == limit` as "possibly more; raise limit or subdivide".
    """
    import json as _json

    matches_obj = [
        {"path": m.path, "line": m.line, "line_text": m.line_text}
        for m in resp.matches
    ]
    payload = {"total_matches": len(matches_obj), "matches": matches_obj}
    return _json.dumps(payload, separators=(", ", ": "))


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


class PcmAdapter:
    def __init__(
        self,
        *,
        runtime: PcmRuntimeClientSync,
        max_tool_result_bytes: int,
    ) -> None:
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
                # Pre-write YAML guard — catches malformed frontmatter
                # BEFORE persistence so the agent can fix-and-retry
                # without accumulating a duplicate-write mutation that
                # the grader flags. Post-write FORMAT_VALIDATOR remains
                # as belt-and-suspenders for non-YAML format issues.
                val = validate_yaml_frontmatter(req.content)
                if not val.ok:
                    emit_arch(
                        category=ArchCategory.FORMAT_PRE_WRITE_REJECT,
                        at_step=None,
                        details=f"path={req.path} error={val.error}",
                    )
                    wall_ms = int((time.monotonic() - start) * 1000)
                    return ToolResult(
                        ok=False,
                        content="",
                        refs=(),
                        error=f"YAML frontmatter parse error: {val.error}",
                        error_code="FORMAT_INVALID",
                        wall_ms=wall_ms,
                    )
                resp = self._runtime.write(
                    pcm_pb2.WriteRequest(path=req.path, content=req.content)
                )
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
                name_in = req.name
                variants = _find_ci_variants(name_in) if _OPT_A_FIND_CI else [name_in]
                if _OPT_A_FIND_CI and len(variants) > 1:
                    union: list[str] = []
                    seen_paths: set[str] = set()
                    hits_before = -1
                    for idx, variant in enumerate(variants):
                        try:
                            r = self._runtime.find(
                                pcm_pb2.FindRequest(
                                    root=req.root,
                                    name=variant,
                                    type=_FIND_TYPE_MAP[req.type],
                                    limit=req.limit,
                                )
                            )
                        except Exception:
                            continue
                        items = list(getattr(r, "items", []) or [])
                        if idx == 0:
                            hits_before = len(items)
                        for p in items:
                            if p not in seen_paths:
                                seen_paths.add(p)
                                union.append(p)
                                if len(union) >= req.limit:
                                    break
                        if len(union) >= req.limit:
                            break
                    resp = pcm_pb2.FindResponse(items=union)
                    _LOG.info(
                        "[OPT_A] find rewrite root=%s name=%r variants=%s "
                        "hits_before=%d hits_after=%d",
                        req.root, name_in, variants, hits_before, len(union),
                    )
                else:
                    resp = self._runtime.find(
                        pcm_pb2.FindRequest(
                            root=req.root,
                            name=name_in,
                            type=_FIND_TYPE_MAP[req.type],
                            limit=req.limit,
                        )
                    )
                    if _OPT_A_FIND_CI:
                        items = list(getattr(resp, "items", []) or [])
                        _LOG.info(
                            "[OPT_A] find no-rewrite root=%s name=%r hits=%d",
                            req.root, name_in, len(items),
                        )
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Search):
                pattern_in = req.pattern
                pattern_out = _maybe_rewrite_ci(pattern_in)
                if _OPT_A_CASE_INSENSITIVE and pattern_out != pattern_in:
                    try:
                        resp_orig = self._runtime.search(
                            pcm_pb2.SearchRequest(
                                root=req.root, pattern=pattern_in, limit=req.limit
                            )
                        )
                        hits_before = len(getattr(resp_orig, "matches", []) or [])
                    except Exception:
                        hits_before = -1
                    resp = self._runtime.search(
                        pcm_pb2.SearchRequest(
                            root=req.root, pattern=pattern_out, limit=req.limit
                        )
                    )
                    hits_after = len(getattr(resp, "matches", []) or [])
                    _LOG.info(
                        "[OPT_A] search rewrite root=%s orig=%r new=%r hits_before=%d hits_after=%d",
                        req.root, pattern_in, pattern_out, hits_before, hits_after,
                    )
                else:
                    resp = self._runtime.search(
                        pcm_pb2.SearchRequest(
                            root=req.root, pattern=pattern_in, limit=req.limit
                        )
                    )
                    if _OPT_A_CASE_INSENSITIVE:
                        hits_after = len(getattr(resp, "matches", []) or [])
                        _LOG.info(
                            "[OPT_A] search no-rewrite root=%s pattern=%r hits=%d",
                            req.root, pattern_in, hits_after,
                        )
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Context):
                resp = self._runtime.context(pcm_pb2.ContextRequest())
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_PreflightSchema):
                from bitgn_contest_agent.preflight.schema import run_preflight_schema
                return run_preflight_schema(self._runtime, None)
            if isinstance(req, Req_PreflightSemanticIndex):
                from bitgn_contest_agent.preflight.schema import (
                    parse_schema_content, run_preflight_schema,
                )
                from bitgn_contest_agent.preflight.semantic_index import (
                    run_preflight_semantic_index,
                )
                # The adapter's `dispatch` is stateless — it may be called
                # with no prior schema in hand (e.g. from a unit test). In
                # that case, run schema first so we have the roots.
                schema_result = run_preflight_schema(self._runtime, None)
                schema = parse_schema_content(schema_result.content)
                return run_preflight_semantic_index(self._runtime, schema)
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

    @staticmethod
    def _strip_leading_slashes(text: str) -> str:
        """Strip leading '/' from file paths in answer text."""
        return re.sub(r'(?m)^/', '', text)

    def submit_terminal(self, completion: ReportTaskCompletion) -> ToolResult:
        start = time.monotonic()
        try:
            message = self._strip_leading_slashes(completion.message)
            refs = [r.lstrip("/") for r in completion.grounding_refs]
            resp = self._runtime.answer(
                pcm_pb2.AnswerRequest(
                    message=message,
                    outcome=_OUTCOME_MAP[completion.outcome],
                    refs=refs,
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

    def run_prepass(self, *, session: Any, trace_writer: Any) -> PrepassResult:
        """Best-effort identity bootstrap.

        Attempts tree(/), read(AGENTS.md), context(), preflight_schema().
        Each failure is recorded and proceeds to the next call —
        identity_loaded flips true on ANY success. Per §1 the session
        is task-local, and the trace writer captures every attempt for
        the analyzer.

        Returns a `PrepassResult` carrying:
        - `bootstrap_content`: list of strings the caller injects as
          additional user messages (today: WORKSPACE SCHEMA payload).
        - `schema`: typed parse of the preflight_schema JSON envelope,
          consumed by the routed-preflight dispatcher. Empty
          `WorkspaceSchema` if preflight_schema failed or was empty.
        """
        from bitgn_contest_agent.adapter.pcm_tracing import pcm_origin
        from bitgn_contest_agent.preflight.schema import parse_schema_content

        bootstrap_content: list[str] = []
        schema_content: str | None = None
        pre_cmds = [
            ("tree", Req_Tree(tool="tree", root="/")),
            ("read_agents_md", Req_Read(tool="read", path="AGENTS.md")),
            ("context", Req_Context(tool="context")),
            ("preflight_schema", Req_PreflightSchema(tool="preflight_schema")),
        ]
        # Phase 1 ops are mutually independent (each is its own RPC) so
        # we dispatch them in parallel. ContextVars don't auto-propagate
        # to ThreadPoolExecutor workers; copy_context() per-submit gives
        # each worker the parent's pcm_origin label.
        def _dispatch_with_origin(req: Any) -> ToolResult:
            with pcm_origin("prepass"):
                return self.dispatch(req)

        with ThreadPoolExecutor(max_workers=len(pre_cmds)) as ex:
            futures = [
                ex.submit(contextvars.copy_context().run, _dispatch_with_origin, req)
                for _, req in pre_cmds
            ]
            phase1_results = [f.result() for f in futures]

        # Apply side-effects + write trace in canonical order so log
        # consumers see the same record sequence as the sequential path.
        with pcm_origin("prepass"):
            for (label, _), result in zip(pre_cmds, phase1_results):
                if result.ok:
                    session.identity_loaded = True
                    if label == "read_agents_md":
                        session.rulebook_loaded = True
                    for ref in result.refs:
                        session.seen_refs.add(ref)
                    if label == "tree" and result.content:
                        bootstrap_content.append(
                            "PRE-PASS tree(root=\"/\") — already executed, "
                            "do NOT re-run:\n"
                            f"{result.content}"
                        )
                    if label == "read_agents_md" and result.content:
                        bootstrap_content.append(
                            "PRE-PASS read(path=\"AGENTS.md\") — already "
                            "executed, do NOT re-run. AGENTS.md content "
                            "below is the rulebook:\n"
                            f"{result.content}"
                        )
                    if label == "context" and result.content:
                        bootstrap_content.append(
                            "PRE-PASS context() — already executed, do NOT "
                            "re-run:\n"
                            f"{result.content}"
                        )
                    if label == "preflight_schema" and result.content:
                        bootstrap_content.append(
                            "WORKSPACE SCHEMA (auto-discovered, use these roots "
                            "when a preflight tool asks for inbox_root / "
                            "entities_root / finance_roots / projects_root):\n"
                            f"{result.content}"
                        )
                        schema_content = result.content
                schema_roots = None
                if label == "preflight_schema" and result.ok and result.content:
                    from bitgn_contest_agent.preflight.schema import parse_schema_content
                    parsed = parse_schema_content(result.content)
                    schema_roots = {
                        "projects_root": parsed.projects_root,
                        "finance_roots": list(parsed.finance_roots),
                        "entities_root": parsed.entities_root,
                        "inbox_root": parsed.inbox_root,
                        "outbox_root": parsed.outbox_root,
                    }
                trace_writer.append_prepass(
                    cmd=label,
                    ok=result.ok,
                    bytes=result.bytes,
                    wall_ms=result.wall_ms,
                    error=result.error,
                    error_code=result.error_code,
                    schema_roots=schema_roots,
                )
        # Phase 2: semantic index — depends on schema roots discovered above.
        parsed_schema = parse_schema_content(schema_content)
        if parsed_schema.entities_root or parsed_schema.projects_root:
            with pcm_origin("prepass"):
                from bitgn_contest_agent.preflight.semantic_index import (
                    run_preflight_semantic_index,
                )
                t0 = time.perf_counter()
                try:
                    si_result = run_preflight_semantic_index(
                        self._runtime, parsed_schema,
                    )
                except Exception as exc:
                    si_result = ToolResult(
                        ok=False, content="", refs=tuple(), error=str(exc),
                        error_code="INTERNAL", wall_ms=0,
                    )
                wall_ms = int((time.perf_counter() - t0) * 1000)
                if si_result.ok and si_result.content:
                    bootstrap_content.append(si_result.content)
                trace_writer.append_prepass(
                    cmd="preflight_semantic_index",
                    ok=si_result.ok,
                    bytes=len(si_result.content or ""),
                    wall_ms=wall_ms,
                    error=si_result.error,
                    error_code=si_result.error_code,
                    schema_roots=None,
                )
        return PrepassResult(
            bootstrap_content=bootstrap_content,
            schema=parse_schema_content(schema_content),
        )

    # -- helpers ----------------------------------------------------------

    def _finish(
        self,
        start: float,
        resp: Any,
        *,
        refs: Tuple[str, ...],
    ) -> ToolResult:
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
