"""Wire-shape alignment tests — assert that LocalEcomClient produces
exactly the JSON the PROD ECOM runtime does.

Source of truth for "what PROD looks like": tests/fixtures/prod_wire/,
captured by `scripts/harness_align/probe_prod.py` on a live
bitgn/ecom1-dev trial via the BitGN harness. Each capture file is
``MessageToDict(..., preserving_proto_field_name=True)`` JSON.

What we assert (and what we deliberately don't):

- Field PRESENCE: every key the PROD response set must appear in the
  local response. Local may set additional fields (e.g. sha256) that
  PROD didn't return for the same call; that's tolerated.
- ENUM string values: NodeKind / Outcome render to their proto enum
  name in JSON. If those drift, MessageToJson is producing different
  output for the same underlying value — the LLM would see a
  different shape, so we hard-fail.
- content_type string values: PROD's MIME taxonomy must match. We
  pinned ``text/markdown`` for .MD and ``application/json`` for
  ``.json`` based on the probe; if /AGENTS.MD's content_type drifts,
  the heuristic in ``_content_type_for`` is out of sync.
  Post-freeze note: ``ExecResponse`` no longer carries a content_type
  field (reserved in the proto), so exec-shape tests check stdout
  contents only and assert the field is absent in the serialized JSON.
- Truncation flag: present iff the response was truncated.

We do NOT assert byte-equal content or sha256 — the fixture files are
small subsets of the PROD workspace, not bytecopies.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.protobuf.json_format import MessageToDict

from bitgn_contest_agent.local.ecom_client import (
    LocalEcomClient,
    NODE_KIND_DIR,
    NODE_KIND_FILE,
    NODE_KIND_UNSPECIFIED,
)


PROD_WIRE = Path(__file__).resolve().parent.parent / "fixtures" / "prod_wire"
SHAPED_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "prod_shaped"
)


def _req(**kw):
    return SimpleNamespace(**kw)


def _to_json(msg) -> dict:
    """Same serialization path the adapter uses for the LLM."""
    return MessageToDict(msg, preserving_proto_field_name=True)


def _prod(name: str) -> dict:
    return json.loads((PROD_WIRE / name).read_text(encoding="utf-8"))


@pytest.fixture
def prod_shaped_client() -> LocalEcomClient:
    """LocalEcomClient over the prod-shaped fixture. Fresh per test so
    write/delete state doesn't leak."""
    # NOTE: prod_shaped intentionally has no SQLite catalogue at top
    # level — the catalogue exec tests use a different fixture.
    return LocalEcomClient(SHAPED_FIXTURE, context_date="2026-02-02T02:40:00Z")


# ---- shape contracts shared across tools ----


def _expected_keys(d: dict) -> set[str]:
    """Top-level keys an aligned local response MUST include. Used to
    catch silent regressions: if PROD ever adds a field, the next
    probe refresh will surface it here."""
    return set(d.keys())


# ---- /bin/id and /bin/date replace the retired context() RPC --------


def test_exec_bin_id_two_line_user_roles_shape(
    prod_shaped_client: LocalEcomClient,
) -> None:
    """PROD `/bin/id` returns TWO newline-terminated lines:
        user: <actor>\\nroles: <role>\\n
    Confirmed across all 62 scanned trials (2026-05-15): every
    successful exec produced this exact template. Default actor on
    most trials was 'anonymous' with role 'GUEST'."""
    local = _to_json(prod_shaped_client.exec(
        _req(path="/bin/id", args=[], stdin=""),
    ))
    stdout = local.get("stdout", "")
    assert "content_type" not in local
    assert "truncated" not in local
    assert stdout.startswith("user: "), stdout
    assert "\nroles: " in stdout, stdout
    assert stdout.endswith("\n"), stdout


def test_exec_bin_id_role_override(tmp_path: Path) -> None:
    """Per-snapshot override of actor/roles via the constructor —
    required to reproduce trials whose seed picked a customer or
    employee identity."""
    from types import SimpleNamespace
    client = LocalEcomClient(
        tmp_path, actor_id="cust_032", roles="customer",
    )
    local = _to_json(client.exec(
        SimpleNamespace(path="/bin/id", args=[], stdin=""),
    ))
    assert local["stdout"] == "user: cust_032\nroles: customer\n"


def test_exec_bin_date_returns_iso_timestamp(
    prod_shaped_client: LocalEcomClient,
) -> None:
    """Post-freeze prepass anchors temporal arithmetic on
    `exec(/bin/date)` stdout. The ISO8601 stamp must end with `Z`
    (and a trailing newline, matching PROD)."""
    local = _to_json(prod_shaped_client.exec(
        _req(path="/bin/date", args=[], stdin=""),
    ))
    stdout = local.get("stdout", "")
    assert stdout.strip().endswith("Z"), stdout
    assert stdout.endswith("\n"), stdout


def test_exec_bin_checkout_no_args_matches_prod_stderr(tmp_path: Path) -> None:
    """PROD's `/bin/checkout` with no args emits a fixed usage stderr:
        "checkout: expected exactly one basket id\\n"
    Confirmed across all 31 scanned trials of run1 (2026-05-15)."""
    from types import SimpleNamespace
    client = LocalEcomClient(tmp_path)
    local = _to_json(client.exec(
        SimpleNamespace(path="/bin/checkout", args=[], stdin=""),
    ))
    assert local["exit_code"] == 1
    assert local["stderr"] == "checkout: expected exactly one basket id\n"


def test_exec_bin_checkout_with_basket_arg_is_empty_success(tmp_path: Path) -> None:
    """With a basket arg PROD performs the real checkout and returns
    an empty response (no stdout / stderr / exit_code, serialises to
    `{}`). Local mock returns the same shape since cart mutation
    can't be modelled snapshot-agnostically — snapshot grading
    catches wrong refusals via expected_outcome."""
    from types import SimpleNamespace
    client = LocalEcomClient(tmp_path)
    local = _to_json(client.exec(
        SimpleNamespace(path="/bin/checkout", args=["basket_001"], stdin=""),
    ))
    assert local == {}


def test_exec_bin_discount_no_args_matches_prod_stderr(tmp_path: Path) -> None:
    """PROD `/bin/discount` with no args:
        exit 1, stderr =
        "discount: expected basket id, percent, reason code, and issuer id\\n"
    Confirmed across all 31 scanned trials."""
    from types import SimpleNamespace
    client = LocalEcomClient(tmp_path)
    local = _to_json(client.exec(
        SimpleNamespace(path="/bin/discount", args=[], stdin=""),
    ))
    assert local["exit_code"] == 1
    assert local["stderr"] == (
        "discount: expected basket id, percent, reason code, and issuer id\n"
    )


def test_exec_bin_payments_no_args_matches_prod_stderr(tmp_path: Path) -> None:
    """PROD `/bin/payments` with no args:
        exit 1, stderr = "payments: expected subcommand\\n"."""
    from types import SimpleNamespace
    client = LocalEcomClient(tmp_path)
    local = _to_json(client.exec(
        SimpleNamespace(path="/bin/payments", args=[], stdin=""),
    ))
    assert local["exit_code"] == 1
    assert local["stderr"] == "payments: expected subcommand\n"


def test_exec_bin_payments_recover_3ds_shape(tmp_path: Path) -> None:
    """PROD `/bin/payments recover-3ds <pay_id>` returns:
        {stdout: "3ds_recovery_started <pay_id>\\n"}
    Confirmed exactly twice in the baseline dump (pay_002, pay_074)."""
    from types import SimpleNamespace
    client = LocalEcomClient(tmp_path)
    local = _to_json(client.exec(
        SimpleNamespace(
            path="/bin/payments", args=["recover-3ds", "pay_002"], stdin="",
        ),
    ))
    assert local == {"stdout": "3ds_recovery_started pay_002\n"}


def test_read_bin_stub_returns_empty_content_with_sha256_of_empty_string(
    tmp_path: Path,
) -> None:
    """All /bin/* binaries are zero-byte stubs on PROD. `read` returns
    {path, content_type=text/plain, sha256=e3b0c442…} with NO `content`
    field. Mirror this regardless of what the local filesystem holds
    so snapshots that copy stub scripts still produce PROD-shaped
    reads."""
    from types import SimpleNamespace
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Local file has non-trivial content; the mock must IGNORE it.
    (bin_dir / "checkout").write_text(
        "#!/bin/sh\necho 'this would never reach the wire'\n"
    )
    client = LocalEcomClient(tmp_path)
    local = _to_json(client.read(
        SimpleNamespace(path="/bin/checkout", number=False,
                        start_line=0, end_line=0),
    ))
    assert "content" not in local, "PROD omits content for zero-byte /bin/*"
    assert local["content_type"] == "text/plain"
    assert local["sha256"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_list_lowercases_echoed_path(tmp_path: Path) -> None:
    """PROD quirk (confirmed across 31 scanned trials): `list`
    lowercases the echoed `path` field, even though tree returns
    mixed-case dir names and `read` preserves case. Mock must do
    the same so the LLM sees identical wire shape."""
    from types import SimpleNamespace
    (tmp_path / "proc" / "catalog" / "3M").mkdir(parents=True)
    (tmp_path / "proc" / "catalog" / "3M" / "SFE-X.json").write_text("{}")
    client = LocalEcomClient(tmp_path)
    local = _to_json(client.list(SimpleNamespace(path="/proc/catalog/3M")))
    assert local["path"] == "/proc/catalog/3m", local["path"]


# ---- tree --------------------------------------------------------------


def test_tree_root_level1_shape_matches_prod(
    prod_shaped_client: LocalEcomClient,
) -> None:
    prod = _prod("tree_root_level1.json")
    local = _to_json(prod_shaped_client.tree(_req(root="/", level=1)))

    # root entry
    assert local["root"]["name"] == "/", (
        "absolute-root tree must label the root entry as '/' to match PROD"
    )
    assert prod["root"]["name"] == "/"
    assert local["root"]["kind"] == "NODE_KIND_DIR"
    assert prod["root"]["kind"] == "NODE_KIND_DIR"

    # NodeKind values across children must all be enum NAME strings
    for entry in local["root"]["children"]:
        assert entry["kind"] in {
            "NODE_KIND_FILE", "NODE_KIND_DIR", "NODE_KIND_UNSPECIFIED",
        }, f"kind serialized as {entry['kind']!r}, not an enum name"

    # AGENTS.MD must surface with content_type=text/markdown — the file
    # the prepass reads on every trial.
    prod_agents = next(
        (c for c in prod["root"]["children"] if c["name"] == "AGENTS.MD"),
        None,
    )
    local_agents = next(
        (c for c in local["root"]["children"] if c["name"] == "AGENTS.MD"),
        None,
    )
    assert prod_agents is not None and local_agents is not None
    assert prod_agents["content_type"] == "text/markdown"
    assert local_agents["content_type"] == "text/markdown"


def test_tree_root_level1_does_not_recurse(
    prod_shaped_client: LocalEcomClient,
) -> None:
    """level=1 → directory children present, but their grandchildren are
    NOT included. Matches PROD's TreeRequest.level semantics."""
    local = _to_json(prod_shaped_client.tree(_req(root="/", level=1)))
    proc = next(c for c in local["root"]["children"] if c["name"] == "proc")
    assert "children" not in proc


# ---- list --------------------------------------------------------------


def test_list_root_shape_matches_prod(
    prod_shaped_client: LocalEcomClient,
) -> None:
    prod = _prod("list_root.json")
    local = _to_json(prod_shaped_client.list(_req(path="/")))
    assert local["path"] == prod["path"] == "/"
    # Every entry MUST carry path + kind. content_type only on files.
    for entry in local["entries"]:
        assert "name" in entry and "path" in entry and "kind" in entry
        if entry["kind"] == "NODE_KIND_FILE":
            assert "content_type" in entry
        else:
            assert "content_type" not in entry


# ---- read --------------------------------------------------------------


def test_read_agents_md_shape_matches_prod(
    prod_shaped_client: LocalEcomClient,
) -> None:
    prod = _prod("read_agents_md.json")
    local = _to_json(prod_shaped_client.read(_req(path="/AGENTS.MD")))
    # PROD response has: path, content_type, content, sha256.
    # `truncated` is omitted when false (proto default).
    for key in ("path", "content_type", "content", "sha256"):
        assert key in prod, f"prod sample malformed: missing {key}"
        assert key in local, f"local lacks {key} key"
    assert local["content_type"] == prod["content_type"] == "text/markdown"
    assert "truncated" not in local, (
        "untruncated read should omit the truncated field — proto default"
    )


def test_read_sliced_number_prepends_cat_n_lines(
    prod_shaped_client: LocalEcomClient,
) -> None:
    """When number=True, PROD prepends `<right-justified N><tab>` to
    each line (cat -n shape). Verified by the PROD probe capture; the
    local mock must mirror this format because the LLM bases its
    `read.number=True` interpretation on the prefix layout."""
    prod = _prod("read_agents_md_sliced.json")
    local = _to_json(prod_shaped_client.read(
        _req(path="/AGENTS.MD", start_line=1, end_line=5, number=True),
    ))
    # Inspect the format: each line in `content` should start with
    # whitespace, a number, a tab.
    for line in local["content"].splitlines():
        if not line.strip():
            continue
        head, _, _ = line.partition("\t")
        assert head.strip().isdigit(), (
            f"numbered line missing leading `<N>\\t` prefix: {line!r}"
        )
    # PROD's prefix uses 6-wide right-justified numbers; check the same.
    prod_first = prod["content"].splitlines()[0]
    local_first = local["content"].splitlines()[0]
    prod_head, _, _ = prod_first.partition("\t")
    local_head, _, _ = local_first.partition("\t")
    assert len(prod_head) == len(local_head) == 6


# ---- find --------------------------------------------------------------


def test_find_no_matches_omits_paths_field(
    prod_shaped_client: LocalEcomClient,
) -> None:
    """PROD: empty find returns `{}` because the repeated `paths` field
    is empty and MessageToJson omits empty repeateds. Local must do the
    same so the LLM sees identical wire shape."""
    prod = _prod("find_files_AGENTS.json")
    assert prod == {}, "fixture capture should be empty {} for no matches"
    local = _to_json(prod_shaped_client.find(
        _req(root="/", name="this-name-does-not-exist",
             kind=NODE_KIND_FILE, limit=5),
    ))
    assert local == {}


def test_find_files_returns_only_file_paths(
    prod_shaped_client: LocalEcomClient,
) -> None:
    """NODE_KIND_FILE filter must exclude directories. Pinned to a
    pattern (`README`) that exists as a file in our fixture so we
    surface at least one hit."""
    local = _to_json(prod_shaped_client.find(
        _req(root="/", name="README", kind=NODE_KIND_FILE, limit=5),
    ))
    assert local.get("paths"), "expected at least one README file"
    for p in local["paths"]:
        assert p.endswith(".md") or p.endswith(".MD"), p


# ---- search ------------------------------------------------------------


def test_search_shape_matches_prod(
    prod_shaped_client: LocalEcomClient,
) -> None:
    prod = _prod("search_TODO.json")
    local = _to_json(prod_shaped_client.search(
        _req(root="/", pattern="catalog", limit=5),
    ))
    # PROD shape: {"matches": [{path, line, line_text}, ...]}
    assert "matches" in prod and "matches" in local
    for m in prod["matches"]:
        assert set(m.keys()) >= {"path", "line", "line_text"}
    for m in local["matches"]:
        assert set(m.keys()) >= {"path", "line", "line_text"}


# ---- stat --------------------------------------------------------------


def test_stat_root_minimum_shape(
    prod_shaped_client: LocalEcomClient,
) -> None:
    """PROD stat on `/` returns only {path, kind}; no writable, no
    content_type, no description. Proto default-omit handles this when
    we leave the fields unset."""
    prod = _prod("stat_root.json")
    local = _to_json(prod_shaped_client.stat(_req(path="/")))
    assert set(prod.keys()) == {"path", "kind"}
    assert set(local.keys()) == {"path", "kind"}
    assert local["kind"] == "NODE_KIND_DIR"


def test_stat_file_includes_content_type(
    prod_shaped_client: LocalEcomClient,
) -> None:
    prod = _prod("stat_agents_md.json")
    local = _to_json(prod_shaped_client.stat(_req(path="/AGENTS.MD")))
    assert local["content_type"] == prod["content_type"] == "text/markdown"
    assert local["kind"] == "NODE_KIND_FILE"


# ---- exec /bin/sql -----------------------------------------------------


def _seed_catalogue(workspace: Path) -> None:
    """Drop a catalogue.db at the workspace root so auto-discovery
    attaches it. Schema mirrors a single-column counting probe so the
    CSV body equals PROD's `n\\n10\\n` capture byte-for-byte."""
    import sqlite3
    db = workspace / "catalogue.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE products (sku TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO products VALUES (?)", [
            (f"SKU-{i}",) for i in range(10)
        ])
        conn.commit()
    finally:
        conn.close()


def test_exec_sql_select_returns_csv_stdout(tmp_path: Path) -> None:
    """Post-freeze `/bin/sql` SELECT: CSV body on stdout (`n\\n10\\n`
    for a single-column COUNT). `content_type` is reserved on
    ExecResponse, so the local mock must NOT emit it — wire-shape match
    is now stdout-only."""
    _seed_catalogue(tmp_path)
    client = LocalEcomClient(tmp_path)
    local = _to_json(client.exec(_req(
        path="/bin/sql", args=[],
        stdin="SELECT count(*) AS n FROM products;",
    )))
    assert "content_type" not in local, (
        "post-freeze ExecResponse must omit the reserved content_type field"
    )
    assert local["stdout"] == "n\n10\n"


def test_exec_sql_dot_commands_rejected_with_syntax_error(tmp_path: Path) -> None:
    """PROD-aligned: `/bin/sql` does NOT implement sqlite dot-commands
    (.schema / .tables). They surface as the raw sqlite syntax error.
    Confirmed across all 62 scanned trials (2026-05-15). Agents must
    query `sqlite_schema` instead. content_type is absent (reserved)."""
    _seed_catalogue(tmp_path)
    client = LocalEcomClient(tmp_path)
    for body in (".schema", ".tables"):
        local = _to_json(client.exec(_req(
            path="/bin/sql", args=[], stdin=body,
        )))
        assert "content_type" not in local
        assert local["exit_code"] == 1, body
        assert local["stderr"] == (
            'SQL logic error: near ".": syntax error (1)\n'
        ), body


def test_exec_sql_sqlite_schema_query_works(tmp_path: Path) -> None:
    """The PROD-supported replacement for dot-commands: query
    sqlite_schema directly. Confirmed in the baseline dump: PROD
    agents use `SELECT name, sql FROM sqlite_schema …` for DDL."""
    _seed_catalogue(tmp_path)
    client = LocalEcomClient(tmp_path)
    local = _to_json(client.exec(_req(
        path="/bin/sql", args=[],
        stdin="SELECT name FROM sqlite_schema WHERE type='table';",
    )))
    assert "content_type" not in local
    assert local["stdout"].startswith("name\n")
    assert "products" in local["stdout"]


# ---- response stringification matches MessageToJson --------------------


def test_response_to_text_produces_json_not_repr(
    prod_shaped_client: LocalEcomClient,
) -> None:
    """The adapter's ``_response_to_text`` formats responses via
    MessageToJson. With dataclass responses, that fell through to
    ``str(resp)`` and the LLM saw Python repr instead of JSON — the
    exact divergence we caught when comparing local vs PROD. Confirm
    real proto responses round-trip through MessageToJson cleanly."""
    from bitgn_contest_agent.adapter.ecom import _response_to_text

    text = _response_to_text(prod_shaped_client.tree(_req(root="/", level=1)))
    parsed = json.loads(text)
    assert parsed["root"]["name"] == "/"
    assert parsed["root"]["kind"] == "NODE_KIND_DIR"
