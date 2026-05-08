"""Pre-pass best-effort identity bootstrap."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bitgn.vm import pcm_pb2

from bitgn_contest_agent.adapter.pcm import PcmAdapter
from bitgn_contest_agent.session import Session


class _FakeTraceWriter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def append_prepass(self, *, cmd: str, ok: bool, **kwargs: object) -> None:
        self.events.append({"cmd": cmd, "ok": ok, **kwargs})


def test_prepass_runs_tree_read_context_preflight_and_marks_loaded() -> None:
    runtime = MagicMock()
    runtime.tree.return_value = pcm_pb2.TreeResponse()
    runtime.read.return_value = pcm_pb2.ReadResponse(content="rules")
    runtime.context.return_value = pcm_pb2.ContextResponse()

    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=16384)
    session = Session()
    writer = _FakeTraceWriter()
    prepass = adapter.run_prepass(session=session, trace_writer=writer)

    # Four pre-pass calls attempted (tree, read, context, preflight_schema).
    # tree is called twice: once by the adapter's Req_Tree dispatch and
    # once by run_preflight_schema's workspace walk.
    assert runtime.tree.call_count == 2
    assert runtime.read.call_count == 1
    assert runtime.context.call_count == 1

    # On ANY success, identity_loaded flips true.
    assert session.identity_loaded is True
    assert "AGENTS.md" in session.seen_refs
    assert len(writer.events) == 4
    assert all(e["ok"] for e in writer.events)
    assert writer.events[3]["cmd"] == "preflight_schema"

    # Bootstrap now also includes PRE-PASS blocks for tree/AGENTS/context
    # so the LLM can skip the redundant identity bootstrap on step 1.
    # run_preflight_schema wraps internal exceptions in `schema.errors`
    # but still returns ok=True, so there is always schema content.
    assert isinstance(prepass.bootstrap_content, list)
    assert any("WORKSPACE SCHEMA" in c for c in prepass.bootstrap_content)
    schema_idx = next(
        i for i, c in enumerate(prepass.bootstrap_content)
        if "WORKSPACE SCHEMA" in c
    )
    for c in prepass.bootstrap_content[:schema_idx]:
        assert c.startswith("PRE-PASS ")

    # The typed schema is parsed from the preflight_schema envelope. Since
    # the runtime mock returned an empty TreeResponse, no roots get
    # discovered — but the dataclass exists and is consumable.
    from bitgn_contest_agent.preflight.schema import WorkspaceSchema
    assert isinstance(prepass.schema, WorkspaceSchema)


def test_prepass_returns_empty_schema_when_no_roots_discovered() -> None:
    runtime = MagicMock()
    runtime.tree.return_value = pcm_pb2.TreeResponse()
    runtime.read.return_value = pcm_pb2.ReadResponse(content="rules")
    runtime.context.return_value = pcm_pb2.ContextResponse()

    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=16384)
    session = Session()
    writer = _FakeTraceWriter()
    prepass = adapter.run_prepass(session=session, trace_writer=writer)

    from bitgn_contest_agent.preflight.schema import WorkspaceSchema
    # No directories with role signatures => empty roots, but dataclass intact.
    assert prepass.schema.inbox_root is None
    assert prepass.schema.entities_root is None
    assert prepass.schema.finance_roots == []
    assert prepass.schema.projects_root is None
    assert isinstance(prepass.schema, WorkspaceSchema)


def test_prepass_is_best_effort_one_failure_does_not_abort_others() -> None:
    runtime = MagicMock()
    runtime.tree.side_effect = RuntimeError("tree failed")
    runtime.read.return_value = pcm_pb2.ReadResponse(content="rules")
    runtime.context.return_value = pcm_pb2.ContextResponse()

    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=16384)
    session = Session()
    writer = _FakeTraceWriter()
    adapter.run_prepass(session=session, trace_writer=writer)

    # Adapter Req_Tree dispatch raised, so count is 1 (not 2). The
    # preflight_schema's inner client.tree() call is short-circuited by
    # the side_effect=RuntimeError, exits at the first access.
    assert runtime.tree.call_count == 2
    assert runtime.read.call_count == 1
    assert runtime.context.call_count == 1
    assert session.identity_loaded is True  # still true — read + context succeeded
    assert len(writer.events) == 4
    assert writer.events[0]["ok"] is False
    assert writer.events[1]["ok"] is True
    assert writer.events[2]["ok"] is True
    # preflight_schema dispatch always returns ok=True (internal errors
    # are captured in its schema.errors payload, not as a dispatch failure).
    assert writer.events[3]["ok"] is True
    assert writer.events[3]["cmd"] == "preflight_schema"
