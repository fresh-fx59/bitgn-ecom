import json
from pathlib import Path

from bitgn_contest_agent.preflight.schema import (
    WorkspaceSchema,
    _classify_dir,
    discover_schema_from_fs,
)


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_ws"


def test_discover_schema_identifies_all_roots():
    schema = discover_schema_from_fs(FIXTURE)
    assert schema.inbox_root == "00_inbox"
    assert schema.entities_root == "20_entities"
    assert "50_finance/purchases" in schema.finance_roots
    assert schema.projects_root == "30_projects"
    assert schema.outbox_root == "60_outbox/outbox"


def test_schema_summary_mentions_each_role():
    schema = discover_schema_from_fs(FIXTURE)
    s = schema.summary()
    assert "inbox" in s.lower()
    assert "finance" in s.lower()
    assert "entit" in s.lower()
    assert "project" in s.lower()
    assert "outbox" in s.lower()


def test_schema_as_data_dict_roundtrips_json():
    schema = discover_schema_from_fs(FIXTURE)
    data = schema.as_data()
    # Must be JSON serializable
    json.dumps(data)
    assert data["inbox_root"] == "00_inbox"


def test_classify_prod_invoices_as_finance_only():
    # PROD-shape invoice: bullet list with record_type=invoice and a
    # line_items section that mentions "project" — must NOT be classified
    # as projects.
    invoices = [
        {
            "record_type": "invoice",
            "vendor": "ACME",
            "line_items": "project management, consulting",
        }
        for _ in range(3)
    ]
    roles = _classify_dir(invoices)
    assert "finance" in roles
    assert "projects" not in roles


def test_classify_prod_projects_as_projects():
    projects = [
        {"record_type": "project", "project": "Studio Parts Library", "start_date": "2026-04-21"},
        {"record_type": "project", "project": "Toy Forge Saturdays", "start_date": "2026-03-01"},
    ]
    roles = _classify_dir(projects)
    assert "projects" in roles


def test_classify_prod_entities_as_entities():
    people = [
        {"record_type": "person", "name": "Alice"},
        {"record_type": "person", "name": "Bob"},
        {"record_type": "cast", "name": "Crew A"},
    ]
    roles = _classify_dir(people)
    assert "entities" in roles


def test_classify_prod_inbox_as_inbox():
    inbox_items = [
        {"record_type": "inbound_email", "from": "a@example.com"},
        {"record_type": "inbox", "from": "b@example.com"},
    ]
    roles = _classify_dir(inbox_items)
    assert "inbox" in roles


def test_classify_prod_outbox_as_outbox():
    outbox_items = [
        {"record_type": "outbound_email", "to": "a@example.com", "subject": "hi"},
        {"record_type": "outbox", "to": "b@example.com", "subject": "hello"},
    ]
    roles = _classify_dir(outbox_items)
    assert "outbox" in roles
