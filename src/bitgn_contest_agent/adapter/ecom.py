"""Single-class adapter between Pydantic Req_* models and the official
bitgn EcomRuntimeClientSync. Every other layer is adapter-agnostic.

The adapter is the ONLY place in the project that imports
`bitgn.vm.ecom.ecom_pb2` or `bitgn.vm.ecom.ecom_connect`. Anywhere else
that references the wire-level proto module is a smell to be fixed.

Heuristics carried over from the PAC1 lineage that are domain-agnostic
and remain useful here:
  - BITGN_OPT_A_CASE_INSENSITIVE: prepend `(?i)` to bare search patterns
  - BITGN_OPT_A_FIND_CI:           fan-out find() across name casings
  - _strip_leading_slashes:        normalize answer ref paths
Each costs at most one extra RPC and only fires when the original
returned zero hits or when explicitly enabled.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from bitgn.vm.ecom import ecom_pb2
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync

from bitgn_contest_agent.schemas import (
    NextStep,  # noqa: F401 — used by external type hints
    ReportTaskCompletion,
    Req_Context,
    Req_Delete,
    Req_Exec,
    Req_Find,
    Req_List,
    Req_Read,
    Req_Search,
    Req_Stat,
    Req_Tree,
    Req_Write,
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
    """Return shape of `EcomAdapter.run_prepass`.

    `bootstrap_content` is the list of strings the agent loop appends as
    additional user messages (today: tree(/), AGENTS.MD, context()).
    `schema` is preserved for call-site stability; the ECOM port no
    longer discovers a workspace schema (vault concept), so this is
    always an empty stub.
    """
    bootstrap_content: list[str]
    schema: "WorkspaceSchema"


def _response_to_text(resp: Any) -> str:
    """Extract a printable representation of any ecom_pb2 response.

    Generated proto messages are not JSON-serializable out of the box, so
    we use the protobuf MessageToJson helper + a plain string fallback.

    Special case: `SearchResponse` gets a `total_matches` field stamped
    at the very top of the JSON body — ahead of the `matches` array —
    so counting tasks survive response truncation. `_finish` may still
    cut the tail of the matches list, but the count is written in the
    first ~30 bytes and cannot be lost.
    """
    try:
        if isinstance(resp, ecom_pb2.SearchResponse):
            return _search_response_to_text(resp)
        from google.protobuf.json_format import MessageToJson

        return MessageToJson(resp, preserving_proto_field_name=True, indent=None)
    except Exception:
        return str(resp)


def _search_response_to_text(resp: "ecom_pb2.SearchResponse") -> str:
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


# ECOM catalogue files have a canonical flat location even when
# `products.path` returns a category-nested form. The grader scores
# refs against the flat form (`/proc/catalog/<SKU>.json`); SKUs are
# 3-letter prefix + dash + 8 char id (e.g. PNT-169R7W8O).
_CATALOG_NESTED_RE = re.compile(
    r"^/proc/catalog/(?:[^/]+/)+([A-Z]{3}-[A-Z0-9]{8}\.json)$"
)


def _canonicalize_ref(ref: str) -> str:
    """Normalize a grounding ref to the form the ECOM grader expects.

    Today this strips category-nested catalogue paths back to the flat
    /proc/catalog/<SKU>.json shape. If we discover other namespaces
    that need normalization, add them here so the wire-level fix lives
    in one place."""
    m = _CATALOG_NESTED_RE.match(ref)
    if m:
        return f"/proc/catalog/{m.group(1)}"
    return ref


_FIND_KIND_MAP: Dict[str, int] = {
    "all": ecom_pb2.NodeKind.NODE_KIND_UNSPECIFIED,
    "files": ecom_pb2.NodeKind.NODE_KIND_FILE,
    "dirs": ecom_pb2.NodeKind.NODE_KIND_DIR,
}


_OUTCOME_MAP: Dict[str, int] = {
    "OUTCOME_OK": ecom_pb2.Outcome.OUTCOME_OK,
    "OUTCOME_DENIED_SECURITY": ecom_pb2.Outcome.OUTCOME_DENIED_SECURITY,
    "OUTCOME_NONE_CLARIFICATION": ecom_pb2.Outcome.OUTCOME_NONE_CLARIFICATION,
    "OUTCOME_NONE_UNSUPPORTED": ecom_pb2.Outcome.OUTCOME_NONE_UNSUPPORTED,
    "OUTCOME_ERR_INTERNAL": ecom_pb2.Outcome.OUTCOME_ERR_INTERNAL,
}


def _build_write_request(req: Req_Write) -> "ecom_pb2.WriteRequest":
    """Construct a WriteRequest, mirroring the sample's belt-and-suspenders
    around field-2 naming drift.

    Some published SDK pins briefly named WriteRequest's field 2
    `content_type` before settling on `content`. The sample mirrors the
    body to whichever name the loaded descriptor exposes. We replicate
    that here so a stale wheel doesn't silently drop the body.
    """
    kwargs = {"path": req.path, "content": req.content}
    if "content_type" in ecom_pb2.WriteRequest.DESCRIPTOR.fields_by_name:
        kwargs["content_type"] = req.content
    return ecom_pb2.WriteRequest(**kwargs)


class EcomAdapter:
    def __init__(
        self,
        *,
        runtime: EcomRuntimeClientSync,
        max_tool_result_bytes: int,
    ) -> None:
        self._runtime = runtime
        self._max_bytes = max_tool_result_bytes

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, req: Any) -> ToolResult:
        start = time.monotonic()
        try:
            if isinstance(req, Req_Read):
                resp = self._runtime.read(
                    ecom_pb2.ReadRequest(
                        path=req.path,
                        number=req.number,
                        start_line=req.start_line,
                        end_line=req.end_line,
                    )
                )
                # Register both the literal path read AND its
                # canonical flat form so the validator's R1 grounding-
                # ref check accepts either. The grader will see only
                # the canonical form (submit_terminal canonicalizes
                # outgoing refs); the agent may have read either.
                canon = _canonicalize_ref(req.path)
                read_refs = (req.path,) if canon == req.path else (req.path, canon)
                return self._finish(start, resp, refs=read_refs)
            if isinstance(req, Req_Stat):
                resp = self._runtime.stat(ecom_pb2.StatRequest(path=req.path))
                # Stat counts as evidence too — agent can use it to
                # confirm a path exists before citing.
                canon = _canonicalize_ref(req.path)
                stat_refs = (req.path,) if canon == req.path else (req.path, canon)
                return self._finish(start, resp, refs=stat_refs)
            if isinstance(req, Req_Write):
                # Pre-write YAML guard — catches malformed frontmatter
                # BEFORE persistence so the agent can fix-and-retry
                # without accumulating a duplicate-write mutation that
                # the grader flags. Domain-agnostic: validate_yaml_frontmatter
                # returns ok=True when no frontmatter is present, so
                # ECOM writes that have no YAML are not affected.
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
                resp = self._runtime.write(_build_write_request(req))
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Delete):
                resp = self._runtime.delete(ecom_pb2.DeleteRequest(path=req.path))
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_List):
                resp = self._runtime.list(ecom_pb2.ListRequest(path=req.path))
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Tree):
                resp = self._runtime.tree(
                    ecom_pb2.TreeRequest(root=req.root, level=req.level)
                )
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
                                ecom_pb2.FindRequest(
                                    root=req.root,
                                    name=variant,
                                    kind=_FIND_KIND_MAP[req.kind],
                                    limit=req.limit,
                                )
                            )
                        except Exception:
                            continue
                        items = list(getattr(r, "paths", []) or [])
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
                    resp = ecom_pb2.FindResponse(paths=union)
                    _LOG.info(
                        "[OPT_A] find rewrite root=%s name=%r variants=%s "
                        "hits_before=%d hits_after=%d",
                        req.root, name_in, variants, hits_before, len(union),
                    )
                else:
                    resp = self._runtime.find(
                        ecom_pb2.FindRequest(
                            root=req.root,
                            name=name_in,
                            kind=_FIND_KIND_MAP[req.kind],
                            limit=req.limit,
                        )
                    )
                    if _OPT_A_FIND_CI:
                        hits = list(getattr(resp, "paths", []) or [])
                        _LOG.info(
                            "[OPT_A] find no-rewrite root=%s name=%r hits=%d",
                            req.root, name_in, len(hits),
                        )
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Search):
                pattern_in = req.pattern
                pattern_out = _maybe_rewrite_ci(pattern_in)
                if _OPT_A_CASE_INSENSITIVE and pattern_out != pattern_in:
                    try:
                        resp_orig = self._runtime.search(
                            ecom_pb2.SearchRequest(
                                root=req.root, pattern=pattern_in, limit=req.limit
                            )
                        )
                        hits_before = len(getattr(resp_orig, "matches", []) or [])
                    except Exception:
                        hits_before = -1
                    resp = self._runtime.search(
                        ecom_pb2.SearchRequest(
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
                        ecom_pb2.SearchRequest(
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
            if isinstance(req, Req_Exec):
                resp = self._runtime.exec(
                    ecom_pb2.ExecRequest(
                        path=req.path, args=list(req.args), stdin=req.stdin,
                    )
                )
                return self._finish(start, resp, refs=())
            if isinstance(req, Req_Context):
                resp = self._runtime.context(ecom_pb2.ContextRequest())
                return self._finish(start, resp, refs=())
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

    def submit_terminal(self, completion: ReportTaskCompletion) -> ToolResult:
        start = time.monotonic()
        try:
            # ECOM paths are absolute (e.g. /proc/catalog/<sku>.json) and
            # the grader matches grounding refs verbatim against those
            # absolute paths. The PAC1 lineage stripped the leading "/"
            # because the vault grader expected vault-relative paths;
            # PROD evidence shows that breaks the ECOM match.
            #
            # ECOM canonicalization: the catalogue exposes the SAME SKU
            # file at both a flat path (/proc/catalog/<sku>.json) and
            # a category-nested shadow (/proc/catalog/<cat>/<sub>/<sku>.json
            # — surfaced by the products.path SQL column on some
            # benchmarks). The grader normalizes refs to the flat form;
            # two t02/t03 failures on the 2026-05-11 run scored 0.0
            # with "answer missing required reference '/proc/catalog/
            # <sku>.json'" while the agent cited the nested form it had
            # actually read. We strip intermediate dirs between
            # /proc/catalog/ and the SKU file before sending.
            refs = [_canonicalize_ref(r) for r in completion.grounding_refs]
            resp = self._runtime.answer(
                ecom_pb2.AnswerRequest(
                    message=completion.message,
                    outcome=_OUTCOME_MAP[completion.outcome],
                    refs=refs,
                )
            )
            return self._finish(start, resp, refs=tuple(refs))
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

        Attempts tree(/, level=2), read(/AGENTS.MD), context(). Each
        failure is recorded and proceeds to the next call —
        identity_loaded flips true on ANY success. Per §1 the session
        is task-local, and the trace writer captures every attempt for
        the analyzer.

        ECOM port note: the PAC1 prepass also ran preflight_schema and
        preflight_semantic_index to discover an Obsidian-vault layout
        (inbox/finance/projects roots, entity index). ECOM has no such
        structure; the runtime publishes its file inventory via
        context() and its tool inventory via /AGENTS.MD, so the prepass
        is now a flat fan-out of the three universal bootstrap calls.
        """
        from bitgn_contest_agent.adapter.ecom_tracing import ecom_origin
        from bitgn_contest_agent.preflight.schema import (
            WorkspaceSchema,
            parse_schema_content,
        )

        bootstrap_content: list[str] = []
        # ECOM uses /AGENTS.MD (uppercase, leading slash) — see
        # sample-agents/ecom-py/agent.py:run_agent.
        pre_cmds = [
            ("tree", Req_Tree(tool="tree", root="/", level=2)),
            ("read_agents_md", Req_Read(tool="read", path="/AGENTS.MD")),
            ("context", Req_Context(tool="context")),
        ]

        def _dispatch_with_origin(req: Any) -> ToolResult:
            with ecom_origin("prepass"):
                return self.dispatch(req)

        # Phase 1 ops are mutually independent (each is its own RPC) so
        # we dispatch them in parallel. ContextVars don't auto-propagate
        # to ThreadPoolExecutor workers; copy_context() per-submit gives
        # each worker the parent's ecom_origin label.
        with ThreadPoolExecutor(max_workers=len(pre_cmds)) as ex:
            futures = [
                ex.submit(contextvars.copy_context().run, _dispatch_with_origin, req)
                for _, req in pre_cmds
            ]
            phase1_results = [f.result() for f in futures]

        with ecom_origin("prepass"):
            for (label, _), result in zip(pre_cmds, phase1_results):
                if result.ok:
                    session.identity_loaded = True
                    if label == "read_agents_md":
                        session.rulebook_loaded = True
                    for ref in result.refs:
                        session.seen_refs.add(ref)
                    if label == "tree" and result.content:
                        bootstrap_content.append(
                            "PRE-PASS tree(root=\"/\", level=2) — already executed, "
                            "do NOT re-run:\n"
                            f"{result.content}"
                        )
                    if label == "read_agents_md" and result.content:
                        bootstrap_content.append(
                            "PRE-PASS read(path=\"/AGENTS.MD\") — already "
                            "executed, do NOT re-run. /AGENTS.MD content "
                            "below is the rulebook:\n"
                            f"{result.content}"
                        )
                    if label == "context" and result.content:
                        bootstrap_content.append(
                            "PRE-PASS context() — already executed, do NOT "
                            "re-run:\n"
                            f"{result.content}"
                        )
                trace_writer.append_prepass(
                    cmd=label,
                    ok=result.ok,
                    bytes=result.bytes,
                    wall_ms=result.wall_ms,
                    error=result.error,
                    error_code=result.error_code,
                    schema_roots=None,
                )
        return PrepassResult(
            bootstrap_content=bootstrap_content,
            schema=parse_schema_content(""),  # ECOM: empty stub for shape compat
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
        if "EcomError" in name:
            return "RUNTIME_ERROR"
        return "UNKNOWN"
