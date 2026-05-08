"""Verify PcmAdapter.run_prepass emits a semantic-index bootstrap
message after the schema bootstrap, when schema roots are present."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult
from bitgn_contest_agent.trace_writer import TraceWriter


def test_run_prepass_appends_semantic_index_bootstrap(tmp_path, monkeypatch):
    runtime = MagicMock()
    runtime.tree.return_value = MagicMock(root=MagicMock(name="", is_dir=True, children=[]))
    runtime.read.return_value = MagicMock(content="")
    runtime.context.return_value = MagicMock()
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=65536)

    canned_schema = json.dumps({
        "summary": "ok",
        "data": {
            "inbox_root": None,
            "entities_root": "10_entities",
            "finance_roots": [],
            "projects_root": "40_projects",
            "outbox_root": None,
            "rulebook_root": None,
            "workflows_root": None,
            "schemas_root": None,
            "errors": [],
        },
    })
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.schema.run_preflight_schema",
        lambda client, ctx: ToolResult(
            ok=True, content=canned_schema, refs=(),
            error=None, error_code=None, wall_ms=1,
        ),
    )
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.semantic_index.run_preflight_semantic_index",
        lambda client, schema: ToolResult(
            ok=True,
            content="WORKSPACE SEMANTIC INDEX …\nCAST:\n- entity.nina  alias=nina",
            refs=(), error=None, error_code=None, wall_ms=1,
        ),
    )

    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path=path)
    session = MagicMock()
    session.identity_loaded = False
    session.rulebook_loaded = False
    session.seen_refs = set()

    prepass = adapter.run_prepass(session=session, trace_writer=writer)
    writer.close()

    # Bootstrap messages now include PRE-PASS tree/AGENTS/context blocks
    # in addition to schema and semantic index. The exact count depends on
    # which pre-pass calls returned non-empty content; what matters is that
    # schema appears before semantic index, both are present, and any
    # PRE-PASS blocks come before the schema block.
    schema_idx = next(
        i for i, c in enumerate(prepass.bootstrap_content)
        if "WORKSPACE SCHEMA" in c
    )
    si_idx = next(
        i for i, c in enumerate(prepass.bootstrap_content)
        if "WORKSPACE SEMANTIC INDEX" in c
    )
    assert si_idx > schema_idx
    for c in prepass.bootstrap_content[:schema_idx]:
        assert c.startswith("PRE-PASS ")

    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    cmds = [r.get("cmd") for r in records]
    assert "preflight_semantic_index" in cmds


def test_run_prepass_suppresses_semantic_index_when_empty(tmp_path, monkeypatch):
    runtime = MagicMock()
    runtime.tree.return_value = MagicMock(root=MagicMock(name="", is_dir=True, children=[]))
    runtime.read.return_value = MagicMock(content="")
    runtime.context.return_value = MagicMock()
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=65536)

    canned_schema = json.dumps({
        "summary": "ok",
        "data": {
            "inbox_root": None,
            "entities_root": None,
            "finance_roots": [],
            "projects_root": None,
            "outbox_root": None,
            "rulebook_root": None,
            "workflows_root": None,
            "schemas_root": None,
            "errors": [],
        },
    })
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.schema.run_preflight_schema",
        lambda client, ctx: ToolResult(
            ok=True, content=canned_schema, refs=(),
            error=None, error_code=None, wall_ms=1,
        ),
    )
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.semantic_index.run_preflight_semantic_index",
        lambda client, schema: ToolResult(
            ok=True, content="", refs=(), error=None,
            error_code=None, wall_ms=1,
        ),
    )

    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path=path)
    session = MagicMock()
    session.identity_loaded = False
    session.rulebook_loaded = False
    session.seen_refs = set()

    prepass = adapter.run_prepass(session=session, trace_writer=writer)
    writer.close()

    # Empty semantic-index content → no bootstrap entry for it.
    # Schema bootstrap still present; PRE-PASS blocks may also be present.
    assert any("WORKSPACE SCHEMA" in c for c in prepass.bootstrap_content)
    assert not any(
        "WORKSPACE SEMANTIC INDEX" in c for c in prepass.bootstrap_content
    )
