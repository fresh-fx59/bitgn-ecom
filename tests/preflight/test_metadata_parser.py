from bitgn_contest_agent.preflight.schema import parse_record_metadata


def test_parses_yaml_frontmatter():
    text = (
        "---\n"
        "record_type: project\n"
        "project: Foo\n"
        "start_date: 2026-01-01\n"
        "---\n"
        "Body text.\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "project"
    assert md["project"] == "Foo"
    assert md["start_date"] == "2026-01-01"


def test_parses_bullet_list():
    text = (
        "# Studio Parts Library\n"
        "\n"
        "- record_type: project\n"
        "- project: Studio Parts Library\n"
        "- start_date: 2026-04-21\n"
        "- members: alice, bob\n"
        "\n"
        "Detail body follows.\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "project"
    assert md["project"] == "Studio Parts Library"
    assert md["start_date"] == "2026-04-21"
    assert md["members"] == "alice, bob"


def test_parses_ascii_table():
    text = (
        "# Invoice INV-001\n"
        "\n"
        "| field | value |\n"
        "| --- | --- |\n"
        "| record_type | invoice |\n"
        "| vendor | ACME Corp |\n"
        "| eur_total | 150.00 |\n"
        "\n"
        "Line items follow.\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "invoice"
    assert md["vendor"] == "ACME Corp"
    assert md["eur_total"] == "150.00"


def test_yaml_wins_when_all_three_present():
    text = (
        "---\n"
        "record_type: project\n"
        "project: FromYaml\n"
        "---\n"
        "\n"
        "- record_type: project\n"
        "- project: FromBullet\n"
    )
    md = parse_record_metadata(text)
    assert md["project"] == "FromYaml"


def test_empty_on_no_metadata():
    text = "Just prose, no metadata here."
    assert parse_record_metadata(text) == {}


def test_bullet_fallback_when_yaml_malformed():
    # YAML frontmatter missing closing delimiter → skipped; bullet wins.
    text = (
        "---\n"
        "not: really: yaml\n"
        "\n"
        "- record_type: person\n"
        "- name: Alice\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "person"
    assert md["name"] == "Alice"


def test_keys_lowercased():
    text = (
        "- Record_Type: project\n"
        "- PROJECT: Foo\n"
    )
    md = parse_record_metadata(text)
    assert "record_type" in md
    assert "project" in md


def test_prod_bullet_strips_backticks():
    # PROD bullet-list records wrap scalar values in backticks.
    text = (
        "# Hearthline\n"
        "\n"
        "- alias: `hearthline`\n"
        "- owner_id: `entity.miles`\n"
        "- kind: `house_system`\n"
        "- status: `active`\n"
    )
    md = parse_record_metadata(text)
    assert md["alias"] == "hearthline"
    assert md["owner_id"] == "entity.miles"
    assert md["status"] == "active"


def test_prod_invoice_ascii_table_in_code_fence():
    # PROD invoices put an ASCII-art `+---+---+` separator table inside
    # a ```text code fence. Must still parse.
    text = (
        "# Northstar early design-partner invoice\n"
        "\n"
        "```text\n"
        "+----------------+-------------------------------+\n"
        "| field          | value                         |\n"
        "+----------------+-------------------------------+\n"
        "| record_type    | invoice                       |\n"
        "| invoice_number | INV-0001                      |\n"
        "| total_eur      | 610                           |\n"
        "+----------------+-------------------------------+\n"
        "```\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "invoice"
    assert md["invoice_number"] == "INV-0001"
    assert md["total_eur"] == "610"


def test_prod_bill_classification():
    # PROD purchase/bill records use the same ASCII-table-in-codefence shape.
    from bitgn_contest_agent.preflight.schema import _classify_dir
    text = (
        "# ESP32 batch\n"
        "```text\n"
        "+---+---+\n"
        "| record_type | bill |\n"
        "+---+---+\n"
        "```\n"
    )
    md = parse_record_metadata(text)
    assert md["record_type"] == "bill"
    assert _classify_dir([md, md, md]) == ["finance"]


def test_prod_project_inferred_from_owner_id():
    # PROD projects lack `record_type`; classifier must infer via `owner_id`.
    from bitgn_contest_agent.preflight.schema import _classify_dir
    text = (
        "# Hearthline\n"
        "- alias: `hearthline`\n"
        "- owner_id: `entity.miles`\n"
        "- kind: `house_system`\n"
    )
    md = parse_record_metadata(text)
    assert "owner_id" in md
    assert _classify_dir([md, md, md]) == ["projects"]


def test_prod_entity_inferred_from_relationship():
    from bitgn_contest_agent.preflight.schema import _classify_dir
    text = (
        "# Badger\n"
        "- alias: `badger`\n"
        "- kind: `system`\n"
        "- relationship: `printer`\n"
        "- important_dates:\n"
    )
    md = parse_record_metadata(text)
    assert _classify_dir([md, md, md]) == ["entities"]


def test_prod_projects_root_rollup_to_parent():
    # Multiple subdirs all classified as projects roll up to common parent.
    from bitgn_contest_agent.preflight.schema import _common_parent
    dirs = [
        "40_projects/2026_03_26_hearthline",
        "40_projects/2026_04_21_studio_parts_library",
        "40_projects/2026_04_25_harbor_body",
    ]
    assert _common_parent(dirs) == "40_projects"


def test_prod_finance_multiroot_preserved():
    # Finance stays multi-valued even when siblings share a common parent.
    from bitgn_contest_agent.preflight.schema import _common_parent
    # _common_parent is used only for single-root fields; finance skips it.
    # This test documents that behavior — sibling dirs are NOT rolled up
    # for finance_roots.
    assert _common_parent(["50_finance/invoices"]) == "50_finance/invoices"
    assert _common_parent(["50_finance/purchases"]) == "50_finance/purchases"
