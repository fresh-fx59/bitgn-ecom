"""Tests for parse_schema_content — reconstructs WorkspaceSchema from the
preflight_schema response content string."""
from __future__ import annotations

from bitgn_contest_agent.preflight.schema import (
    WorkspaceSchema,
    parse_schema_content,
)


def test_parse_full_content_round_trip():
    """Round-trip via the actual build_response shape."""
    from bitgn_contest_agent.preflight.response import build_response
    src = WorkspaceSchema(
        inbox_root="10_inbox",
        entities_root="30_entities",
        finance_roots=["50_finance/purchases", "50_finance/invoices"],
        projects_root="40_projects",
        outbox_root="20_outbox",
    )
    content = build_response(summary=src.summary(), data=src.as_data())
    out = parse_schema_content(content)
    assert out.inbox_root == "10_inbox"
    assert out.entities_root == "30_entities"
    assert out.finance_roots == ["50_finance/purchases", "50_finance/invoices"]
    assert out.projects_root == "40_projects"
    assert out.outbox_root == "20_outbox"


def test_parse_partial_content():
    content = '{"summary": "...", "data": {"finance_roots": ["50_finance"]}}'
    out = parse_schema_content(content)
    assert out.inbox_root is None
    assert out.entities_root is None
    assert out.finance_roots == ["50_finance"]
    assert out.projects_root is None


def test_parse_invalid_returns_empty():
    out = parse_schema_content("not json at all")
    assert out == WorkspaceSchema()


def test_parse_none_returns_empty():
    out = parse_schema_content(None)
    assert out == WorkspaceSchema()


def test_parse_missing_data_key_returns_empty():
    out = parse_schema_content('{"summary": "no data"}')
    assert out == WorkspaceSchema()
