#!/usr/bin/env -S .venv/bin/python3 -u
"""Generate parameterized test variants from workspace data.

Reads a BitGN workspace snapshot and produces a test catalogue with
hundreds of variants across entity, project, and finance queries.
Tests disambiguation quality without PROD access.

Usage:
    python scripts/generate_test_variants.py \
        --workspace artifacts/ws_snapshots/t053/run_0/workspace \
        --output artifacts/test_cases/generated_variants.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Entity:
    """Parsed entity from 10_entities/cast/*.md."""
    alias: str
    name: str  # heading title, e.g. "Nina Schreiber"
    kind: str  # person, pet, system
    relationship: str
    birthday: str | None = None
    important_dates: dict[str, str] = field(default_factory=dict)
    email: str | None = None
    description: str = ""  # free text after frontmatter
    file_path: str = ""


@dataclass
class Project:
    """Parsed project from 40_projects/*/README.MD."""
    name: str  # heading title, e.g. "Helios Workflow Sprint"
    alias: str
    dir_name: str  # e.g. "2026_04_30_helios_workflow_sprint"
    start_date: str  # extracted from dir_name: "2026-04-30"
    kind: str = ""
    lane: str = ""
    status: str = ""
    linked_entities: list[str] = field(default_factory=list)  # entity aliases
    file_path: str = ""


@dataclass
class FinanceRecord:
    """Parsed invoice or purchase from 50_finance/."""
    record_type: str  # "invoice" or "bill"
    identifier: str  # invoice_number or bill_id
    alias: str
    date_field: str  # issued_on or purchased_on
    total_eur: float
    counterparty: str
    project: str
    related_entity: str
    line_items: list[dict[str, Any]] = field(default_factory=list)
    file_path: str = ""


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_frontmatter_value(line: str) -> str:
    """Extract value from '- key: `value`' line."""
    m = re.search(r"`([^`]*)`", line)
    return m.group(1) if m else line.split(":", 1)[-1].strip()


def parse_entity(path: Path) -> Entity | None:
    """Parse a single entity markdown file."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    if not lines:
        return None

    # Skip AGENTS.MD
    if path.name == "AGENTS.MD":
        return None

    # Extract heading
    name = ""
    for line in lines:
        if line.startswith("# "):
            name = line[2:].strip()
            break

    alias = ""
    kind = ""
    relationship = ""
    birthday = None
    email = None
    important_dates: dict[str, str] = {}
    description_lines: list[str] = []
    in_important_dates = False
    past_frontmatter = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            continue

        # Detect important_dates sub-list
        if stripped.startswith("- important_dates:"):
            in_important_dates = True
            continue

        if in_important_dates:
            if stripped.startswith("- `"):
                # Parse sub-item: - `key`: `value` - optional description
                m = re.match(r"-\s+`([^`]+)`:\s+`([^`]+)`", stripped)
                if m:
                    important_dates[m.group(1)] = m.group(2)
                continue
            elif stripped.startswith("- ") or stripped == "":
                in_important_dates = False
                if stripped == "":
                    continue
            else:
                in_important_dates = False

        # Regular frontmatter
        if stripped.startswith("- alias:"):
            alias = _parse_frontmatter_value(stripped)
        elif stripped.startswith("- kind:"):
            kind = _parse_frontmatter_value(stripped)
        elif stripped.startswith("- relationship:"):
            relationship = _parse_frontmatter_value(stripped)
        elif stripped.startswith("- birthday:"):
            birthday = _parse_frontmatter_value(stripped)
        elif stripped.startswith("- primary_contact_email:"):
            email = _parse_frontmatter_value(stripped)
        elif stripped.startswith("- "):
            # Other frontmatter we skip
            continue
        elif stripped and not stripped.startswith("-") and not stripped.startswith("#"):
            if not stripped.startswith("```"):
                description_lines.append(stripped)

    if not alias:
        return None

    return Entity(
        alias=alias,
        name=name,
        kind=kind,
        relationship=relationship,
        birthday=birthday,
        important_dates=important_dates,
        email=email,
        description=" ".join(description_lines).strip(),
        file_path=str(path),
    )


def parse_project(project_dir: Path) -> Project | None:
    """Parse a single project README.MD."""
    readme = project_dir / "README.MD"
    if not readme.exists():
        return None

    text = readme.read_text(encoding="utf-8")
    lines = text.splitlines()
    dir_name = project_dir.name

    # Extract start date from directory name: YYYY_MM_DD_rest
    m = re.match(r"(\d{4})_(\d{2})_(\d{2})_", dir_name)
    start_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""

    name = ""
    alias = ""
    kind = ""
    lane = ""
    status = ""
    linked_entities: list[str] = []
    in_linked = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("# "):
            name = stripped[2:].strip()
            continue

        if stripped.startswith("- linked_entities:"):
            in_linked = True
            continue

        if in_linked:
            if stripped.startswith("- `entity."):
                entity_alias = stripped.split("`entity.")[1].rstrip("`").strip()
                linked_entities.append(entity_alias)
                continue
            elif stripped.startswith("- ") or stripped == "":
                in_linked = False
                if stripped == "":
                    continue
            else:
                in_linked = False

        if stripped.startswith("- alias:"):
            alias = _parse_frontmatter_value(stripped)
        elif stripped.startswith("- kind:"):
            kind = _parse_frontmatter_value(stripped)
        elif stripped.startswith("- lane:"):
            lane = _parse_frontmatter_value(stripped)
        elif stripped.startswith("- status:"):
            status = _parse_frontmatter_value(stripped)

    if not name:
        return None

    return Project(
        name=name,
        alias=alias or dir_name,
        dir_name=dir_name,
        start_date=start_date,
        kind=kind,
        lane=lane,
        status=status,
        linked_entities=linked_entities,
        file_path=str(readme),
    )


def _parse_table_row(row: str) -> list[str]:
    """Parse a pipe-delimited table row into cell values."""
    cells = row.split("|")
    # Strip first/last empty cells from leading/trailing pipes
    cells = [c.strip() for c in cells if c.strip() and c.strip() != "+"]
    return cells


def _parse_ascii_table(text: str) -> list[dict[str, str]]:
    """Parse an ASCII table (pipe-delimited with +--- separators) into rows.

    For field/value tables: returns [{"field": ..., "value": ...}]
    For line-item tables: returns [{"#": ..., "item": ..., "qty": ..., ...}]
    """
    lines = text.strip().splitlines()
    # Filter out separator lines (starting with +)
    data_lines = [l for l in lines if l.strip() and not l.strip().startswith("+")]
    if len(data_lines) < 2:
        return []

    # Parse header
    header_cells = _parse_table_row(data_lines[0])
    rows = []
    for line in data_lines[1:]:
        cells = _parse_table_row(line)
        if len(cells) == len(header_cells):
            row = dict(zip(header_cells, cells))
            rows.append(row)
    return rows


def parse_finance_record(path: Path) -> FinanceRecord | None:
    """Parse an invoice or purchase markdown file."""
    if path.name == "AGENTS.MD":
        return None

    text = path.read_text(encoding="utf-8")

    # Extract code blocks
    code_blocks = re.findall(r"```text\s*\n(.*?)```", text, re.DOTALL)
    if not code_blocks:
        return None

    # First block is the frontmatter table
    frontmatter_rows = _parse_ascii_table(code_blocks[0])
    fm: dict[str, str] = {}
    for row in frontmatter_rows:
        if "field" in row and "value" in row:
            fm[row["field"]] = row["value"]

    record_type = fm.get("record_type", "")
    identifier = fm.get("invoice_number", "") or fm.get("bill_id", "")
    alias = fm.get("alias", "")
    date_field = fm.get("issued_on", "") or fm.get("purchased_on", "")

    total_str = fm.get("total_eur", "0")
    try:
        total_eur = float(total_str)
    except ValueError:
        total_eur = 0.0

    counterparty = fm.get("counterparty", "")
    project = fm.get("project", "")
    related_entity = fm.get("related_entity", "")

    # Parse line items (second code block)
    line_items: list[dict[str, Any]] = []
    if len(code_blocks) >= 2:
        li_rows = _parse_ascii_table(code_blocks[1])
        for row in li_rows:
            item_name = row.get("item", "")
            if item_name.upper() == "TOTAL" or not item_name:
                continue
            try:
                qty = int(row.get("qty", "0"))
            except ValueError:
                qty = 0
            try:
                unit_eur = float(row.get("unit_eur", "0"))
            except ValueError:
                unit_eur = 0.0
            try:
                line_eur = float(row.get("line_eur", "0"))
            except ValueError:
                line_eur = 0.0
            line_items.append({
                "item": item_name,
                "qty": qty,
                "unit_eur": unit_eur,
                "line_eur": line_eur,
            })

    return FinanceRecord(
        record_type=record_type,
        identifier=identifier,
        alias=alias,
        date_field=date_field,
        total_eur=total_eur,
        counterparty=counterparty,
        project=project,
        related_entity=related_entity,
        line_items=line_items,
        file_path=str(path),
    )


# ---------------------------------------------------------------------------
# Workspace loader
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    """Full parsed workspace data."""
    entities: list[Entity]
    projects: list[Project]
    invoices: list[FinanceRecord]
    purchases: list[FinanceRecord]
    # Derived indexes
    entity_by_alias: dict[str, Entity] = field(default_factory=dict)
    entity_by_relationship: dict[str, Entity] = field(default_factory=dict)
    projects_by_entity: dict[str, list[str]] = field(default_factory=dict)  # alias -> project names


def load_workspace(ws_path: Path) -> Workspace:
    """Load and index entire workspace."""
    entities: list[Entity] = []
    cast_dir = ws_path / "10_entities" / "cast"
    if cast_dir.exists():
        for f in sorted(cast_dir.glob("*.md")):
            ent = parse_entity(f)
            if ent:
                entities.append(ent)

    projects: list[Project] = []
    proj_dir = ws_path / "40_projects"
    if proj_dir.exists():
        for d in sorted(proj_dir.iterdir()):
            if d.is_dir():
                proj = parse_project(d)
                if proj:
                    projects.append(proj)

    invoices: list[FinanceRecord] = []
    inv_dir = ws_path / "50_finance" / "invoices"
    if inv_dir.exists():
        for f in sorted(inv_dir.glob("*.md")):
            rec = parse_finance_record(f)
            if rec:
                invoices.append(rec)

    purchases: list[FinanceRecord] = []
    pur_dir = ws_path / "50_finance" / "purchases"
    if pur_dir.exists():
        for f in sorted(pur_dir.glob("*.md")):
            rec = parse_finance_record(f)
            if rec:
                purchases.append(rec)

    # Build indexes
    entity_by_alias = {e.alias: e for e in entities}
    entity_by_relationship = {e.relationship: e for e in entities}

    # Map entity alias -> list of project names
    projects_by_entity: dict[str, list[str]] = {}
    for proj in projects:
        for ea in proj.linked_entities:
            projects_by_entity.setdefault(ea, []).append(proj.name)
    # Sort project lists alphabetically (PROD expects sorted output)
    for k in projects_by_entity:
        projects_by_entity[k].sort()

    return Workspace(
        entities=entities,
        projects=projects,
        invoices=invoices,
        purchases=purchases,
        entity_by_alias=entity_by_alias,
        entity_by_relationship=entity_by_relationship,
        projects_by_entity=projects_by_entity,
    )


# ---------------------------------------------------------------------------
# Relationship-to-descriptor mappings
# ---------------------------------------------------------------------------

# Exact relationship descriptors (how PROD phrases them)
RELATIONSHIP_DESCRIPTORS: dict[str, list[str]] = {
    "self": ["myself", "me"],
    "wife": ["my wife"],
    "daughter": ["my daughter"],
    "son": ["my son"],
    "mother_in_law": ["my mother-in-law", "my mother in law"],
    "startup_partner": ["my startup partner"],
    "startup_advisor": ["my startup advisor"],
    "consulting_client": ["my consulting client"],
    "day_job_ceo": ["my day-job CEO", "the day-job CEO"],
    "product_manager": ["my product manager"],
    "ops_lead": ["the ops lead", "my ops lead"],
    "engineering_counterpart": ["my engineering counterpart"],
    "maker_friend": ["my maker friend"],
    "gaming_friend": ["my gaming friend"],
    "health_friend": ["my health friend"],
    "bureau_lead": ["the bureau lead"],
    "dog": ["the dog", "our dog", "the family dog"],
    "hamster": ["the hamster", "our hamster"],
    "home_server": ["the home server"],
    "lab_server": ["the lab server"],
    "printer": ["the printer", "the 3D printer"],
    "assistant_prototype": ["the assistant prototype", "the AI assistant"],
}

# Fuzzy/indirect descriptors for harder tests
FUZZY_DESCRIPTORS: dict[str, list[str]] = {
    "startup_partner": ["my design partner", "the finance workflow person"],
    "startup_advisor": ["my sounding board", "the positioning advisor"],
    "consulting_client": ["the tax office contact", "the Helios person"],
    "day_job_ceo": ["my CEO", "the founder"],
    "product_manager": ["the product person"],
    "ops_lead": ["the operations guy", "the exception-case person"],
    "engineering_counterpart": ["the skeptical engineer"],
    "maker_friend": ["the bracket designer", "the mechanical design guy"],
    "gaming_friend": ["the Warhammer friend", "the grimdark buddy"],
    "health_friend": ["my accountability buddy", "the sanity-check friend"],
    "bureau_lead": ["Petra's bureau contact"],
    "home_server": ["Juniper", "the quiet server"],
    "lab_server": ["Foundry", "the loud lab box"],
    "printer": ["Badger", "the family printer"],
    "assistant_prototype": ["NORA", "the AI thing", "the ambient helper"],
    "wife": ["Petra", "the architect"],
    "daughter": ["Ida", "the school-age one"],
    "son": ["Oskar", "the kindergarten one"],
    "dog": ["Bix"],
    "hamster": ["Pepper"],
}

# Month names for multilingual finance queries
MONTHS_EN = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTHS_FR = [
    "", "janvier", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
]
MONTHS_ZH = [
    "", "1月", "2月", "3月", "4月", "5月", "6月",
    "7月", "8月", "9月", "10月", "11月", "12月",
]


# ---------------------------------------------------------------------------
# Test case generators
# ---------------------------------------------------------------------------


def _task_id(counter: list[int]) -> str:
    """Generate sequential task ID."""
    tid = f"gen_{counter[0]:03d}"
    counter[0] += 1
    return tid


def generate_which_projects(ws: Workspace, counter: list[int]) -> list[dict[str, Any]]:
    """Template 1: 'Which projects is X involved?'"""
    cases: list[dict[str, Any]] = []

    for entity in ws.entities:
        project_names = ws.projects_by_entity.get(entity.alias, [])
        if not project_names:
            continue

        expected = "\n".join(sorted(project_names))

        # --- Easy: by alias ---
        cases.append({
            "task_id": _task_id(counter),
            "intent": (
                f"In which projects is {entity.alias} involved? "
                "Return only the exact project names, one per line, sorted alphabetically."
            ),
            "category": "PROJECT_INVOLVEMENT",
            "expected_answer": expected,
            "template": "which_projects",
            "params": {"entity_alias": entity.alias, "descriptor_type": "alias"},
            "difficulty": "easy",
        })

        # --- Easy: by full name (if person/pet) ---
        if entity.name and entity.name != entity.alias:
            cases.append({
                "task_id": _task_id(counter),
                "intent": (
                    f"In which projects is {entity.name} involved? "
                    "Return only the exact project names, one per line, sorted alphabetically."
                ),
                "category": "PROJECT_INVOLVEMENT",
                "expected_answer": expected,
                "template": "which_projects",
                "params": {"entity_alias": entity.alias, "descriptor_type": "full_name"},
                "difficulty": "easy",
            })

        # --- Medium: by relationship descriptor ---
        rel_descs = RELATIONSHIP_DESCRIPTORS.get(entity.relationship, [])
        for desc in rel_descs:
            cases.append({
                "task_id": _task_id(counter),
                "intent": (
                    f"In which projects is {desc} involved? "
                    "Return only the exact project names, one per line, sorted alphabetically."
                ),
                "category": "PROJECT_INVOLVEMENT",
                "expected_answer": expected,
                "template": "which_projects",
                "params": {
                    "entity_alias": entity.alias,
                    "descriptor_type": "relationship",
                    "descriptor": desc,
                },
                "difficulty": "medium",
            })

        # --- Hard: by fuzzy/indirect descriptor ---
        fuzzy_descs = FUZZY_DESCRIPTORS.get(entity.relationship, [])
        for desc in fuzzy_descs:
            cases.append({
                "task_id": _task_id(counter),
                "intent": (
                    f"In which projects is {desc} involved? "
                    "Return only the exact project names, one per line, sorted alphabetically."
                ),
                "category": "PROJECT_INVOLVEMENT",
                "expected_answer": expected,
                "template": "which_projects",
                "params": {
                    "entity_alias": entity.alias,
                    "descriptor_type": "fuzzy",
                    "descriptor": desc,
                },
                "difficulty": "hard",
            })

    return cases


def generate_active_project_count(ws: Workspace, counter: list[int]) -> list[dict[str, Any]]:
    """Template: 'How many ACTIVE projects involve X?'"""
    cases: list[dict[str, Any]] = []

    for entity in ws.entities:
        # Count active projects involving this entity
        active_count = 0
        for proj in ws.projects:
            if entity.alias in proj.linked_entities and proj.status == "active":
                active_count += 1

        if active_count == 0:
            continue

        # By full name
        if entity.name and entity.name != entity.alias:
            cases.append({
                "task_id": _task_id(counter),
                "intent": (
                    f"How many ACTIVE projects involve {entity.name}? "
                    "Answer with a number only"
                ),
                "category": "PROJECT_COUNT",
                "expected_answer": str(active_count),
                "template": "active_project_count",
                "params": {"entity_alias": entity.alias, "descriptor_type": "full_name"},
                "difficulty": "easy",
            })

        # By relationship descriptor
        rel_descs = RELATIONSHIP_DESCRIPTORS.get(entity.relationship, [])
        for desc in rel_descs[:1]:  # just first one to limit volume
            cases.append({
                "task_id": _task_id(counter),
                "intent": (
                    f"How many ACTIVE projects involve {desc}? "
                    "Answer with a number only"
                ),
                "category": "PROJECT_COUNT",
                "expected_answer": str(active_count),
                "template": "active_project_count",
                "params": {
                    "entity_alias": entity.alias,
                    "descriptor_type": "relationship",
                    "descriptor": desc,
                },
                "difficulty": "medium",
            })

    return cases


def generate_birthday_queries(ws: Workspace, counter: list[int]) -> list[dict[str, Any]]:
    """Template 3: 'When was X born?'"""
    cases: list[dict[str, Any]] = []

    for entity in ws.entities:
        if not entity.birthday:
            continue

        # --- Easy: by full name ---
        if entity.name and entity.name != entity.alias:
            cases.append({
                "task_id": _task_id(counter),
                "intent": f"When was {entity.name} born? Answer YYYY-MM-DD. Date only",
                "category": "ENTITY_DATE",
                "expected_answer": entity.birthday,
                "template": "birthday",
                "params": {"entity_alias": entity.alias, "descriptor_type": "full_name"},
                "difficulty": "easy",
            })

        # --- Easy: by alias ---
        cases.append({
            "task_id": _task_id(counter),
            "intent": f"When was {entity.alias} born? Answer YYYY-MM-DD. Date only",
            "category": "ENTITY_DATE",
            "expected_answer": entity.birthday,
            "template": "birthday",
            "params": {"entity_alias": entity.alias, "descriptor_type": "alias"},
            "difficulty": "easy",
        })

        # --- Medium: by relationship ---
        rel_descs = RELATIONSHIP_DESCRIPTORS.get(entity.relationship, [])
        for desc in rel_descs[:1]:
            cases.append({
                "task_id": _task_id(counter),
                "intent": f"When was {desc} born? Answer YYYY-MM-DD. Date only",
                "category": "ENTITY_DATE",
                "expected_answer": entity.birthday,
                "template": "birthday",
                "params": {
                    "entity_alias": entity.alias,
                    "descriptor_type": "relationship",
                    "descriptor": desc,
                },
                "difficulty": "medium",
            })

        # --- Medium: alternate date format ---
        if entity.name:
            cases.append({
                "task_id": _task_id(counter),
                "intent": (
                    f"Need {entity.name}'s birthday. "
                    "Reply with the date in MM/DD/YYYY format only."
                ),
                "category": "ENTITY_DATE",
                "expected_answer": _reformat_date(entity.birthday, "MM/DD/YYYY"),
                "template": "birthday_alt_format",
                "params": {
                    "entity_alias": entity.alias,
                    "descriptor_type": "full_name",
                    "date_format": "MM/DD/YYYY",
                },
                "difficulty": "medium",
            })

        # --- Hard: fuzzy descriptor ---
        fuzzy_descs = FUZZY_DESCRIPTORS.get(entity.relationship, [])
        for desc in fuzzy_descs[:1]:
            cases.append({
                "task_id": _task_id(counter),
                "intent": f"When was {desc} born? Answer YYYY-MM-DD. Date only",
                "category": "ENTITY_DATE",
                "expected_answer": entity.birthday,
                "template": "birthday",
                "params": {
                    "entity_alias": entity.alias,
                    "descriptor_type": "fuzzy",
                    "descriptor": desc,
                },
                "difficulty": "hard",
            })

    return cases


def generate_project_start_date(ws: Workspace, counter: list[int]) -> list[dict[str, Any]]:
    """Template 4: 'What is the start date of project X?'"""
    cases: list[dict[str, Any]] = []

    for proj in ws.projects:
        if not proj.start_date:
            continue

        # --- Easy: by exact project name ---
        cases.append({
            "task_id": _task_id(counter),
            "intent": (
                f"What is the start date of the project {proj.name}? "
                "Answer YYYY-MM-DD. Date only"
            ),
            "category": "PROJECT_DATE",
            "expected_answer": proj.start_date,
            "template": "project_start_date",
            "params": {"project_alias": proj.alias, "descriptor_type": "exact_name"},
            "difficulty": "easy",
        })

        # --- Medium: by lane descriptor ---
        if proj.kind:
            # e.g. "the consulting project", "the startup project"
            kind_desc = f"the {proj.kind} project"
            # Only generate if there's a unique project of that kind
            same_kind = [p for p in ws.projects if p.kind == proj.kind]
            if len(same_kind) == 1:
                cases.append({
                    "task_id": _task_id(counter),
                    "intent": (
                        f"What is the start date of {kind_desc}? "
                        "Answer YYYY-MM-DD. Date only"
                    ),
                    "category": "PROJECT_DATE",
                    "expected_answer": proj.start_date,
                    "template": "project_start_date",
                    "params": {
                        "project_alias": proj.alias,
                        "descriptor_type": "kind",
                        "descriptor": kind_desc,
                    },
                    "difficulty": "medium",
                })

        # --- Hard: by indirect/fuzzy descriptor from PROD patterns ---
        # PROD uses descriptors like "the house AI thing" for Hearthline
        fuzzy_project_descs: dict[str, list[str]] = {
            "hearthline": ["the household coordination system", "the house AI thing"],
            "house_mesh": ["the home network project", "the ESP32 project"],
            "northstar_ledger": ["the product startup", "the workflow product"],
            "helios_workflow_sprint": ["the tax office consulting gig", "the Helios project"],
            "dockflow_exception_radar": ["the day-job exception project"],
            "studio_parts_library": ["the 3D printing parts project"],
            "black_library_evenings": ["the Warhammer hobby project"],
            "harbor_body": ["the health project"],
            "toy_forge_saturdays": ["the kids' printing project"],
            "school_helper_kit": ["the school morning project"],
            "family_map_wall": ["the family memory map"],
            "window_farm_notes": ["the plant care project"],
            "reading_spine": ["the reading habit project"],
            "repair_ledger": ["the household repairs project"],
        }
        for desc in fuzzy_project_descs.get(proj.alias, []):
            cases.append({
                "task_id": _task_id(counter),
                "intent": (
                    f"What is the start date of {desc}? "
                    "Answer YYYY-MM-DD. Date only"
                ),
                "category": "PROJECT_DATE",
                "expected_answer": proj.start_date,
                "template": "project_start_date",
                "params": {
                    "project_alias": proj.alias,
                    "descriptor_type": "fuzzy",
                    "descriptor": desc,
                },
                "difficulty": "hard",
            })

    return cases


def _sum_line_items_since(
    records: list[FinanceRecord],
    item_name: str,
    since_date: str,
) -> float:
    """Sum line_eur for matching line items in records issued on or after since_date."""
    total = 0.0
    for rec in records:
        if rec.date_field < since_date:
            continue
        for li in rec.line_items:
            if li["item"].strip().lower() == item_name.strip().lower():
                total += li["line_eur"]
    return total


def _sum_total_by_counterparty(
    records: list[FinanceRecord],
    counterparty: str,
) -> float:
    """Sum total_eur for all records matching a counterparty."""
    total = 0.0
    for rec in records:
        if rec.counterparty.strip() == counterparty.strip():
            total += rec.total_eur
    return total


def generate_finance_line_item(ws: Workspace, counter: list[int]) -> list[dict[str, Any]]:
    """Template 2: 'How much money from service line X since date Y?'"""
    cases: list[dict[str, Any]] = []

    # Collect all unique line item names from invoices
    line_item_names: set[str] = set()
    for inv in ws.invoices:
        for li in inv.line_items:
            line_item_names.add(li["item"])

    # Find the date range of invoices
    invoice_dates = sorted(inv.date_field for inv in ws.invoices if inv.date_field)
    if not invoice_dates:
        return cases

    earliest_year = int(invoice_dates[0][:4])
    latest_year = int(invoice_dates[-1][:4])

    for item_name in sorted(line_item_names):
        # Generate queries for various start dates
        for year in range(earliest_year, latest_year + 1):
            for month in [1, 3, 6, 9, 12]:
                since_date = f"{year}-{month:02d}-01"
                amount = _sum_line_items_since(ws.invoices, item_name, since_date)
                if amount <= 0:
                    continue

                month_en = MONTHS_EN[month]

                # --- Easy: English ---
                cases.append({
                    "task_id": _task_id(counter),
                    "intent": (
                        f"How much money did we make from the service line "
                        f"'{item_name}' since the beginning of {month_en} {year}? "
                        f"Answer with a number only"
                    ),
                    "category": "FINANCE_LINE_ITEM",
                    "expected_answer": _format_amount(amount),
                    "template": "finance_line_item",
                    "params": {
                        "line_item": item_name,
                        "since_month": month,
                        "since_year": year,
                        "language": "en",
                    },
                    "difficulty": "easy",
                })

                # --- Medium: Chinese ---
                cases.append({
                    "task_id": _task_id(counter),
                    "intent": (
                        f"\u4ece{month_en} {year}\u5f00\u59cb\uff0c"
                        f"\u6211\u4eec\u901a\u8fc7\u670d\u52a1\u9879\u76ee"
                        f"\u201c{item_name}\u201d\u8d5a\u4e86\u591a\u5c11\u94b1\uff1f"
                        f"\u53ea\u56de\u7b54\u4e00\u4e2a\u6570\u5b57\u3002"
                    ),
                    "category": "FINANCE_LINE_ITEM",
                    "expected_answer": _format_amount(amount),
                    "template": "finance_line_item",
                    "params": {
                        "line_item": item_name,
                        "since_month": month,
                        "since_year": year,
                        "language": "zh",
                    },
                    "difficulty": "medium",
                })

                # --- Medium: French ---
                month_fr = MONTHS_FR[month]
                cases.append({
                    "task_id": _task_id(counter),
                    "intent": (
                        f"Combien d'argent avons-nous gagne avec la ligne de service "
                        f"'{item_name}' depuis le debut de {month_fr} {year}? "
                        f"Repondre avec un nombre seulement."
                    ),
                    "category": "FINANCE_LINE_ITEM",
                    "expected_answer": _format_amount(amount),
                    "template": "finance_line_item",
                    "params": {
                        "line_item": item_name,
                        "since_month": month,
                        "since_year": year,
                        "language": "fr",
                    },
                    "difficulty": "medium",
                })

    return cases


def generate_finance_counterparty_total(
    ws: Workspace, counter: list[int],
) -> list[dict[str, Any]]:
    """Template 5: 'How much did I pay to X in total?'"""
    cases: list[dict[str, Any]] = []

    # Collect unique counterparties from purchases
    purchase_counterparties: dict[str, float] = {}
    for pur in ws.purchases:
        cp = pur.counterparty.strip()
        purchase_counterparties[cp] = purchase_counterparties.get(cp, 0) + pur.total_eur

    for cp, total in sorted(purchase_counterparties.items()):
        if total <= 0:
            continue

        cases.append({
            "task_id": _task_id(counter),
            "intent": (
                f"How much did I pay to {cp} in total? Number only"
            ),
            "category": "FINANCE_COUNTERPARTY",
            "expected_answer": _format_amount(total),
            "template": "finance_counterparty_total",
            "params": {"counterparty": cp, "record_type": "purchases"},
            "difficulty": "easy",
        })

    # Also for invoice counterparties (revenue side)
    invoice_counterparties: dict[str, float] = {}
    for inv in ws.invoices:
        cp = inv.counterparty.strip()
        invoice_counterparties[cp] = invoice_counterparties.get(cp, 0) + inv.total_eur

    for cp, total in sorted(invoice_counterparties.items()):
        if total <= 0:
            continue

        cases.append({
            "task_id": _task_id(counter),
            "intent": (
                f"How much did {cp} pay me in total? Number only"
            ),
            "category": "FINANCE_COUNTERPARTY",
            "expected_answer": _format_amount(total),
            "template": "finance_counterparty_revenue",
            "params": {"counterparty": cp, "record_type": "invoices"},
            "difficulty": "easy",
        })

        # --- Medium: entity-based reference ---
        # Find which entity is related to this counterparty
        entity_names = set()
        for inv in ws.invoices:
            if inv.counterparty.strip() == cp:
                entity_names.add(inv.related_entity)
        for en in sorted(entity_names):
            cases.append({
                "task_id": _task_id(counter),
                "intent": (
                    f"How much revenue came from invoices related to {en}? Number only"
                ),
                "category": "FINANCE_COUNTERPARTY",
                "expected_answer": _format_amount(total),
                "template": "finance_entity_revenue",
                "params": {
                    "counterparty": cp,
                    "related_entity": en,
                    "record_type": "invoices",
                },
                "difficulty": "medium",
            })

    return cases


def generate_finance_line_item_counterparty(
    ws: Workspace, counter: list[int],
) -> list[dict[str, Any]]:
    """Template: 'How much did X charge me for line item Y?'"""
    cases: list[dict[str, Any]] = []

    # Group: counterparty -> line item -> total
    cp_items: dict[str, dict[str, float]] = {}
    for rec in ws.invoices + ws.purchases:
        cp = rec.counterparty.strip()
        for li in rec.line_items:
            cp_items.setdefault(cp, {})
            cp_items[cp][li["item"]] = cp_items[cp].get(li["item"], 0) + li["line_eur"]

    for cp, items in sorted(cp_items.items()):
        for item_name, total in sorted(items.items()):
            if total <= 0:
                continue
            cases.append({
                "task_id": _task_id(counter),
                "intent": (
                    f"How much did {cp} charge me in total for the line item "
                    f"{item_name}? Answer with the EUR amount, number only"
                ),
                "category": "FINANCE_LINE_COUNTERPARTY",
                "expected_answer": _format_amount(total),
                "template": "finance_line_item_counterparty",
                "params": {"counterparty": cp, "line_item": item_name},
                "difficulty": "medium",
            })

    return cases


def generate_important_date_queries(
    ws: Workspace, counter: list[int],
) -> list[dict[str, Any]]:
    """Template: 'When was X commissioned?' or other important_dates lookups."""
    cases: list[dict[str, Any]] = []

    date_label_to_question: dict[str, str] = {
        "commissioned_on": "When was {name} commissioned? Answer YYYY-MM-DD. Date only",
        "prototype_started": "When did {name} start? Answer YYYY-MM-DD. Date only",
        "annual_vet_window": "When is {name}'s next vet window? Answer YYYY-MM-DD. Date only",
        "maintenance_window": "When is {name}'s maintenance window? Answer YYYY-MM-DD. Date only",
    }

    for entity in ws.entities:
        for label, date_val in entity.important_dates.items():
            if label == "birthday":
                continue  # handled by birthday template
            template_q = date_label_to_question.get(label)
            if not template_q:
                continue
            question = template_q.format(name=entity.name or entity.alias)
            cases.append({
                "task_id": _task_id(counter),
                "intent": question,
                "category": "ENTITY_DATE",
                "expected_answer": date_val,
                "template": "important_date",
                "params": {
                    "entity_alias": entity.alias,
                    "date_label": label,
                },
                "difficulty": "medium",
            })

    return cases


def generate_next_birthday(ws: Workspace, counter: list[int]) -> list[dict[str, Any]]:
    """Template: 'Who has the next upcoming birthday?'"""
    cases: list[dict[str, Any]] = []

    # Compute next birthday from a reference date
    # Use a few reference dates to test different answers
    ref_dates = [
        "2026-04-01",
        "2026-05-01",
        "2026-06-01",
        "2026-07-01",
        "2026-08-01",
        "2026-01-01",
    ]

    for ref_str in ref_dates:
        ref = datetime.strptime(ref_str, "%Y-%m-%d").date()
        year = ref.year

        # Find next birthday
        best_entities: list[Entity] = []
        best_date: date | None = None

        for entity in ws.entities:
            if not entity.birthday:
                continue
            bd = datetime.strptime(entity.birthday, "%Y-%m-%d").date()
            # This year's birthday
            this_year_bd = bd.replace(year=year)
            if this_year_bd < ref:
                this_year_bd = bd.replace(year=year + 1)

            if best_date is None or this_year_bd < best_date:
                best_date = this_year_bd
                best_entities = [entity]
            elif this_year_bd == best_date:
                best_entities.append(entity)

        if not best_entities or best_date is None:
            continue

        # Expected answer: names sorted alphabetically
        names = sorted(e.name or e.alias for e in best_entities)
        expected = "\n".join(names)

        cases.append({
            "task_id": _task_id(counter),
            "intent": (
                f"Who has the next upcoming birthday after {ref_str}? "
                "If there is a tie on the date, include everyone. "
                "Return names only, one per line, sorted alphabetically."
            ),
            "category": "ENTITY_DATE",
            "expected_answer": expected,
            "template": "next_birthday",
            "params": {"reference_date": ref_str},
            "difficulty": "hard",
        })

    return cases


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_amount(amount: float) -> str:
    """Format a EUR amount as an integer string if whole, else with decimals."""
    if amount == int(amount):
        return str(int(amount))
    return f"{amount:.2f}"


def _reformat_date(date_str: str, fmt: str) -> str:
    """Reformat a YYYY-MM-DD date string to another format."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    if fmt == "MM/DD/YYYY":
        return d.strftime("%m/%d/%Y")
    return date_str


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate parameterized test variants from workspace data",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Path to workspace root (e.g. artifacts/ws_snapshots/t053/run_0/workspace)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output JSON path",
    )
    parser.add_argument(
        "--templates",
        nargs="+",
        default=["all"],
        help="Which templates to generate (default: all). "
        "Options: which_projects, birthday, project_start_date, "
        "finance_line_item, finance_counterparty, finance_line_counterparty, "
        "active_project_count, important_date, next_birthday, all",
    )
    args = parser.parse_args()

    ws_path = Path(args.workspace)
    if not ws_path.exists():
        print(f"ERROR: workspace path {ws_path} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Loading workspace from {ws_path}")
    ws = load_workspace(ws_path)

    print(f"  Entities: {len(ws.entities)}")
    print(f"  Projects: {len(ws.projects)}")
    print(f"  Invoices: {len(ws.invoices)}")
    print(f"  Purchases: {len(ws.purchases)}")

    templates_to_run = set(args.templates)
    run_all = "all" in templates_to_run

    counter = [0]
    all_cases: list[dict[str, Any]] = []

    generators: list[tuple[str, Any]] = [
        ("which_projects", generate_which_projects),
        ("active_project_count", generate_active_project_count),
        ("birthday", generate_birthday_queries),
        ("project_start_date", generate_project_start_date),
        ("finance_line_item", generate_finance_line_item),
        ("finance_counterparty", generate_finance_counterparty_total),
        ("finance_line_counterparty", generate_finance_line_item_counterparty),
        ("important_date", generate_important_date_queries),
        ("next_birthday", generate_next_birthday),
    ]

    for name, gen_fn in generators:
        if run_all or name in templates_to_run:
            cases = gen_fn(ws, counter)
            print(f"  {name}: {len(cases)} variants")
            all_cases.extend(cases)

    # Summary by difficulty
    by_diff: dict[str, int] = {}
    for c in all_cases:
        d = c.get("difficulty", "unknown")
        by_diff[d] = by_diff.get(d, 0) + 1

    by_cat: dict[str, int] = {}
    for c in all_cases:
        cat = c["category"]
        by_cat[cat] = by_cat.get(cat, 0) + 1

    print(f"\nTotal variants: {len(all_cases)}")
    print(f"  By difficulty: {json.dumps(by_diff)}")
    print(f"  By category:   {json.dumps(by_cat)}")

    # Build output catalogue
    catalogue = {
        "source": "generated_variants",
        "workspace_path": str(ws_path.resolve()),
        "generated_at": datetime.now().isoformat(),
        "total_variants": len(all_cases),
        "summary": {
            "by_difficulty": by_diff,
            "by_category": by_cat,
            "by_template": _count_by_key(all_cases, "template"),
        },
        "test_cases": all_cases,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalogue, indent=2, ensure_ascii=False))
    print(f"\nCatalogue saved to {output_path}")


def _count_by_key(cases: list[dict[str, Any]], key: str) -> dict[str, int]:
    """Count cases by a given key."""
    counts: dict[str, int] = {}
    for c in cases:
        val = c.get(key, "unknown")
        counts[val] = counts.get(val, 0) + 1
    return counts


if __name__ == "__main__":
    main()
