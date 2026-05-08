from pathlib import Path
from unittest.mock import MagicMock

from bitgn_contest_agent.preflight.semantic_index import extract_cast_entries
from bitgn_contest_agent.preflight.semantic_index import extract_project_entries
from bitgn_contest_agent.preflight.semantic_index import format_digest
from bitgn_contest_agent.preflight.semantic_index import build_digest_from_fs
from bitgn_contest_agent.preflight.semantic_index import run_preflight_semantic_index
from bitgn_contest_agent.preflight.schema import WorkspaceSchema


FIXTURE = Path(__file__).parent / "fixtures" / "semantic_index_ws"


def test_extract_cast_entries_parses_bullet_and_yaml_skips_malformed():
    entries = extract_cast_entries(FIXTURE / "10_entities" / "cast")
    # Expect exactly 2 entries (nina + elena), malformed skipped.
    aliases = sorted(e.alias for e in entries)
    assert aliases == ["elena", "nina"]

    nina = next(e for e in entries if e.alias == "nina")
    assert nina.id == "entity.nina"
    assert nina.relationship == "startup_partner"
    assert nina.summary == "Pushes Miles to narrow the product and find a real buyer."

    elena = next(e for e in entries if e.alias == "elena")
    assert elena.id == "entity.elena"
    assert elena.relationship == "day_job_ceo"
    assert elena.summary.startswith("Founder and CEO who cares")


def test_extract_project_entries_prefers_goal_field_falls_back_to_prose():
    entries = extract_project_entries(FIXTURE / "40_projects")
    aliases = sorted(e.alias for e in entries)
    assert aliases == ["black_library_evenings", "harbor_body"]

    harbor = next(e for e in entries if e.alias == "harbor_body")
    assert harbor.id == "project.harbor_body"
    assert harbor.lane == "health"
    assert harbor.status == "active"
    # `goal:` field wins over body prose.
    assert harbor.goal.startswith("Stay functional enough")

    library = next(e for e in entries if e.alias == "black_library_evenings")
    assert library.lane == "family"
    # No `goal:` field → first prose line.
    assert library.goal.startswith("Preserve a protected evening lane")


def test_format_digest_includes_both_blocks_and_semantic_contrast():
    from bitgn_contest_agent.preflight.semantic_index import (
        extract_cast_entries, extract_project_entries,
    )
    cast = extract_cast_entries(FIXTURE / "10_entities" / "cast")
    projects = extract_project_entries(FIXTURE / "40_projects")
    digest = format_digest(cast=cast, projects=projects)

    assert "WORKSPACE SEMANTIC INDEX" in digest
    assert "CAST:" in digest
    assert "PROJECTS:" in digest
    # Semantic contrast visible on one line each:
    assert "entity.nina" in digest
    assert "startup_partner" in digest
    assert "narrow the product" in digest
    assert "entity.elena" in digest
    assert "day_job_ceo" in digest
    assert "project.harbor_body" in digest
    assert "lane=health" in digest
    assert "project.black_library_evenings" in digest
    assert "lane=family" in digest


def test_format_digest_omits_empty_blocks():
    digest = format_digest(cast=[], projects=[])
    # Nothing to index → empty string (caller suppresses).
    assert digest == ""


def test_format_digest_cast_only_when_no_projects():
    from bitgn_contest_agent.preflight.semantic_index import extract_cast_entries
    cast = extract_cast_entries(FIXTURE / "10_entities" / "cast")
    digest = format_digest(cast=cast, projects=[])
    assert "CAST:" in digest
    assert "PROJECTS:" not in digest


def test_build_digest_from_fs_composes_both_blocks():
    digest = build_digest_from_fs(
        root=FIXTURE,
        entities_root="10_entities",
        projects_root="40_projects",
    )
    assert "CAST:" in digest
    assert "PROJECTS:" in digest
    assert "entity.nina" in digest
    assert "project.harbor_body" in digest


def test_build_digest_from_fs_no_roots_returns_empty_string():
    digest = build_digest_from_fs(
        root=FIXTURE, entities_root=None, projects_root=None,
    )
    assert digest == ""


def _mk_pcm_stub_for_fixture():
    """Stub a PcmRuntime that walks the on-disk fixture. Uses list/read
    RPCs exactly as run_preflight_semantic_index does."""
    runtime = MagicMock()

    def _list(req):
        entries = []
        p = FIXTURE / req.name
        if p.is_dir():
            for child in sorted(p.iterdir()):
                e = MagicMock()
                e.name = child.name
                e.is_dir = child.is_dir()
                entries.append(e)
        resp = MagicMock()
        resp.entries = entries
        return resp

    def _read(req):
        p = FIXTURE / req.path
        resp = MagicMock()
        resp.content = p.read_text(encoding="utf-8") if p.is_file() else ""
        return resp

    runtime.list.side_effect = _list
    runtime.read.side_effect = _read
    return runtime


def test_run_preflight_semantic_index_returns_bootstrap_via_pcm():
    runtime = _mk_pcm_stub_for_fixture()
    schema = WorkspaceSchema(
        entities_root="10_entities",
        projects_root="40_projects",
    )
    result = run_preflight_semantic_index(runtime, schema)
    assert result.ok is True
    assert "WORKSPACE SEMANTIC INDEX" in (result.content or "")
    assert "entity.nina" in result.content
    assert "project.harbor_body" in result.content


def test_run_preflight_semantic_index_empty_schema_is_ok_empty_content():
    runtime = MagicMock()
    schema = WorkspaceSchema()
    result = run_preflight_semantic_index(runtime, schema)
    assert result.ok is True
    assert result.content == ""
