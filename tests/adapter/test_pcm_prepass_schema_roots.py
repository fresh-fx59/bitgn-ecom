"""Verify PcmAdapter.run_prepass attaches schema_roots to the
preflight_schema trace record."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from bitgn_contest_agent.adapter.pcm import PcmAdapter
from bitgn_contest_agent.trace_writer import TraceWriter


def _mk_adapter_with_stub_schema(tmp_path: Path):
    """Build a PcmAdapter whose runtime returns a canned schema."""
    runtime = MagicMock()
    # Short-circuit tree/read/context so only preflight_schema path is exercised.
    runtime.tree.return_value = MagicMock(root=MagicMock(name="", is_dir=True, children=[]))
    runtime.read.return_value = MagicMock(content="")
    runtime.context.return_value = MagicMock()
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=65536)
    return adapter


def test_run_prepass_attaches_schema_roots(tmp_path, monkeypatch):
    adapter = _mk_adapter_with_stub_schema(tmp_path)

    # Stub run_preflight_schema to return a known ToolResult.
    from bitgn_contest_agent.adapter.pcm import ToolResult
    canned_envelope = json.dumps({
        "summary": "ok",
        "data": {
            "inbox_root": "00_inbox",
            "entities_root": "20_entities",
            "finance_roots": ["50_finance/invoices"],
            "projects_root": "40_projects",
            "outbox_root": "60_outbox/outbox",
            "rulebook_root": None,
            "workflows_root": None,
            "schemas_root": None,
            "errors": [],
        },
    })
    monkeypatch.setattr(
        "bitgn_contest_agent.preflight.schema.run_preflight_schema",
        lambda client, ctx: ToolResult(
            ok=True, content=canned_envelope, refs=(), error=None,
            error_code=None, wall_ms=1,
        ),
    )

    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path=path)
    session = MagicMock()
    session.identity_loaded = False
    session.rulebook_loaded = False
    session.seen_refs = set()

    adapter.run_prepass(session=session, trace_writer=writer)
    writer.close()

    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    schema_recs = [r for r in records if r.get("cmd") == "preflight_schema"]
    assert schema_recs, "preflight_schema trace record missing"
    sr = schema_recs[0].get("schema_roots")
    assert sr is not None
    assert sr["projects_root"] == "40_projects"
    assert sr["finance_roots"] == ["50_finance/invoices"]
