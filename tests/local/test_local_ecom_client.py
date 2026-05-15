"""Wire-shape tests for LocalEcomClient.

Asserts that the duck-typed responses match the attribute surface the
agent's adapter consumes (read.content, list.entries[].kind,
search.matches[].line_text, find.paths, etc.). If these drift, the
adapter will silently fail against the real ECOM runtime — same as
the find.items vs find.paths bug we caught while building the mock.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bitgn_contest_agent.local.ecom_client import (
    LocalEcomClient,
    NODE_KIND_DIR,
    NODE_KIND_FILE,
    NODE_KIND_UNSPECIFIED,
)


def _req(**kw):
    """Construct a duck-typed request — every ECOM RPC takes a single
    proto request object; SimpleNamespace lets the test stay readable
    without importing the real protos."""
    return SimpleNamespace(**kw)


# ---- tree --------------------------------------------------------------


def test_tree_walks_workspace_at_root(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    resp = client.tree(_req(root="/", level=0))
    names = {c.name for c in resp.root.children}
    assert "AGENTS.MD" in names
    assert "data" in names
    assert "catalogue.db" in names
    data_dir = next(c for c in resp.root.children if c.name == "data")
    assert data_dir.kind == NODE_KIND_DIR
    assert {c.name for c in data_dir.children} == {"orders.csv", "notes.md"}


def test_tree_respects_level_cap(fixture_workspace: Path) -> None:
    """level=1 should expose direct children of root but not their grand-
    children. Mirrors the ECOM TreeRequest.level semantics."""
    client = LocalEcomClient(fixture_workspace)
    resp = client.tree(_req(root="/", level=1))
    data_dir = next(c for c in resp.root.children if c.name == "data")
    assert data_dir.kind == NODE_KIND_DIR
    assert data_dir.children == []


# ---- list --------------------------------------------------------------


def test_list_returns_flat_entries_with_kind_and_path(
    fixture_workspace: Path,
) -> None:
    client = LocalEcomClient(fixture_workspace)
    resp = client.list(_req(path="/data"))
    by_name = {e.name: e for e in resp.entries}
    assert set(by_name) == {"orders.csv", "notes.md"}
    assert by_name["orders.csv"].kind == NODE_KIND_FILE
    assert by_name["orders.csv"].path == "/data/orders.csv"
    assert "csv" in by_name["orders.csv"].content_type


# ---- read --------------------------------------------------------------


def test_read_returns_content_path_sha_and_content_type(
    fixture_workspace: Path,
) -> None:
    client = LocalEcomClient(fixture_workspace)
    resp = client.read(_req(path="/AGENTS.MD"))
    assert "BitGN ECOM Local Fixture" in resp.content
    assert resp.path == "/AGENTS.MD"
    assert "md" in resp.content_type or "markdown" in resp.content_type
    assert len(resp.sha256) == 64  # hex-encoded sha256
    assert resp.truncated is False


def test_read_line_slicing_inclusive_one_based(
    fixture_workspace: Path,
) -> None:
    client = LocalEcomClient(fixture_workspace)
    full = client.read(_req(path="/data/orders.csv")).content
    lines = full.splitlines(keepends=True)
    assert len(lines) == 6  # header + 5 rows

    sliced = client.read(_req(
        path="/data/orders.csv", start_line=2, end_line=4,
    ))
    assert sliced.content == "".join(lines[1:4])
    assert sliced.truncated is True


# ---- find --------------------------------------------------------------


def test_find_returns_paths_and_respects_kind_filter(
    fixture_workspace: Path,
) -> None:
    client = LocalEcomClient(fixture_workspace)
    resp = client.find(_req(
        root="/", name="orders", kind=NODE_KIND_FILE, limit=10,
    ))
    assert resp.paths == ["/data/orders.csv"]

    resp_dirs = client.find(_req(
        root="/", name="data", kind=NODE_KIND_DIR, limit=10,
    ))
    assert resp_dirs.paths == ["/data"]

    # NODE_KIND_UNSPECIFIED returns both — the directory `data` is a
    # substring match for "data".
    resp_all = client.find(_req(
        root="/", name="data", kind=NODE_KIND_UNSPECIFIED, limit=10,
    ))
    assert "/data" in resp_all.paths


def test_find_substring_is_case_sensitive_by_default(
    fixture_workspace: Path,
) -> None:
    """PROD ECOM find is case-sensitive substring; the local mock
    matches PROD by default. ECOM_LOCAL_CASE_INSENSITIVE_FIND=1 is
    a separate test below."""
    client = LocalEcomClient(fixture_workspace)
    resp = client.find(_req(
        root="/", name="ORDERS", kind=NODE_KIND_FILE, limit=10,
    ))
    assert resp.paths == []


def test_find_case_insensitive_via_env(
    fixture_workspace: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("ECOM_LOCAL_CASE_INSENSITIVE_FIND", "1")
    client = LocalEcomClient(fixture_workspace)
    resp = client.find(_req(
        root="/", name="ORDERS", kind=NODE_KIND_FILE, limit=10,
    ))
    assert resp.paths == ["/data/orders.csv"]


# ---- search ------------------------------------------------------------


def test_search_returns_matches_with_path_line_and_text(
    fixture_workspace: Path,
) -> None:
    client = LocalEcomClient(fixture_workspace)
    resp = client.search(_req(root="/", pattern="TODO", limit=10))
    todo_lines = [m for m in resp.matches if "TODO" in m.line_text]
    assert len(todo_lines) >= 2
    first = todo_lines[0]
    assert first.path == "/data/notes.md"
    assert first.line >= 1
    assert "TODO" in first.line_text


def test_search_truncates_at_limit(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    # The CSV has 5 data rows plus a header — pattern matches all six.
    resp = client.search(_req(root="/", pattern=",", limit=2))
    assert len(resp.matches) == 2
    assert resp.truncated is True


# ---- stat --------------------------------------------------------------


def test_stat_reports_kind_and_content_type(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    file_stat = client.stat(_req(path="/data/orders.csv"))
    assert file_stat.kind == NODE_KIND_FILE
    assert "csv" in file_stat.content_type

    dir_stat = client.stat(_req(path="/data"))
    assert dir_stat.kind == NODE_KIND_DIR


def test_stat_missing_path_raises(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    with pytest.raises(FileNotFoundError):
        client.stat(_req(path="/does/not/exist"))


# ---- exec /bin/sql -----------------------------------------------------


def test_exec_sql_count_orders(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    resp = client.exec(_req(
        path="/bin/sql", args=[],
        stdin="SELECT count(*) AS n FROM orders;",
    ))
    assert resp.exit_code == 0
    assert "n" in resp.stdout
    assert "5" in resp.stdout


def test_exec_sql_join_orders_customers(fixture_workspace: Path) -> None:
    """Cross-table join — proves the catalogue exposes both tables and
    the result includes named columns in the header row. Post-freeze
    ExecResponse no longer carries content_type (reserved); CSV-vs-DDL
    distinction now lives in stdout shape only."""
    client = LocalEcomClient(fixture_workspace)
    resp = client.exec(_req(
        path="/bin/sql", args=[],
        stdin=(
            "SELECT c.name, sum(o.total) AS total_cents "
            "FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE o.status = 'paid' GROUP BY c.name "
            "ORDER BY total_cents DESC;"
        ),
    ))
    assert resp.exit_code == 0
    assert "name,total_cents" in resp.stdout
    # Acme Co paid: 4500 + 1200 = 5700; Globex paid: 3300; Pimoroni paid: 200.
    assert "5700" in resp.stdout
    assert "Pimoroni Europe SARL" in resp.stdout


def test_exec_sql_invalid_query_returns_nonzero_exit(
    fixture_workspace: Path,
) -> None:
    client = LocalEcomClient(fixture_workspace)
    resp = client.exec(_req(
        path="/bin/sql", args=[], stdin="SELECT * FROM nope;",
    ))
    assert resp.exit_code != 0
    assert "no such table" in resp.stderr.lower() or "nope" in resp.stderr


def test_exec_unknown_path_returns_127(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    resp = client.exec(_req(
        path="/bin/something-else", args=[], stdin="",
    ))
    assert resp.exit_code == 127
    assert "/bin/sql" in resp.stderr


# ---- context, write, delete -------------------------------------------


def test_exec_date_uses_supplied_date(fixture_workspace: Path) -> None:
    """Post-freeze: trial clock surfaces via exec(/bin/date), not the
    retired context() RPC. The supplied ISO timestamp must round-trip
    on stdout so the prepass can anchor relative-date arithmetic."""
    from types import SimpleNamespace

    client = LocalEcomClient(
        fixture_workspace, context_date="2026-05-08T12:00:00Z",
    )
    resp = client.exec(SimpleNamespace(path="/bin/date", args=[], stdin=""))
    assert resp.exit_code == 0
    assert "2026-05-08T12:00:00Z" in resp.stdout


def test_exec_id_uses_supplied_actor(fixture_workspace: Path) -> None:
    """`/bin/id` replaces the actor portion of the retired context()
    payload — one-line descriptor on stdout."""
    from types import SimpleNamespace

    client = LocalEcomClient(fixture_workspace, actor_id="manager")
    resp = client.exec(SimpleNamespace(path="/bin/id", args=[], stdin=""))
    assert resp.exit_code == 0
    assert "manager" in resp.stdout


def test_write_then_read_round_trip(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    client.write(_req(path="/scratch/note.txt", content="hello ecom"))
    resp = client.read(_req(path="/scratch/note.txt"))
    assert resp.content == "hello ecom"
    assert "scratch/note.txt" in client.writes


def test_delete_removes_file_and_logs(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    client.delete(_req(path="/data/notes.md"))
    with pytest.raises(FileNotFoundError):
        client.read(_req(path="/data/notes.md"))
    assert "data/notes.md" in client.deletes


# ---- safety -----------------------------------------------------------


def test_path_escape_attempt_raises(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    with pytest.raises(ValueError):
        client.read(_req(path="../etc/passwd"))


# ---- ops_log ----------------------------------------------------------


def test_ops_log_records_every_call(fixture_workspace: Path) -> None:
    client = LocalEcomClient(fixture_workspace)
    client.tree(_req(root="/", level=1))
    client.read(_req(path="/AGENTS.MD"))
    client.search(_req(root="/", pattern="TODO", limit=5))
    ops = [entry["op"] for entry in client.ops_log]
    assert ops == ["tree", "read", "search"]
