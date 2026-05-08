"""Round-trip tests for the NextStep Union (§5.2 Test 2)."""
from __future__ import annotations

import json
from typing import Any

import pytest

from bitgn_contest_agent import schemas
from bitgn_contest_agent.schemas import (
    NextStep,
    REQ_MODELS,
    Req_Context,
    Req_Find,
    Req_List,
    Req_Move,
    Req_Read,
    Req_Search,
    Req_Tree,
    Req_Write,
    Req_Delete,
    Req_MkDir,
    ReportTaskCompletion,
)


def test_module_imports():
    assert hasattr(schemas, "NextStep")
    assert hasattr(schemas, "ReportTaskCompletion")


def _sample_function_payloads() -> list[dict[str, Any]]:
    return [
        {"tool": "read", "path": "AGENTS.md"},
        {"tool": "write", "path": "/tmp/a", "content": "hello"},
        {"tool": "delete", "path": "/tmp/a"},
        {"tool": "mkdir", "path": "/tmp/new"},
        {"tool": "move", "from_name": "a", "to_name": "b"},
        {"tool": "list", "name": "/"},
        {"tool": "tree", "root": "/"},
        {
            "tool": "find",
            "root": "/",
            "name": "*.py",
            "type": "TYPE_FILES",
            "limit": 50,
        },
        {"tool": "search", "root": "/", "pattern": "TODO", "limit": 25},
        {"tool": "context"},
        {
            "tool": "report_completion",
            "message": "done",
            "grounding_refs": ["AGENTS.md"],
            "rulebook_notes": "followed identity pass",
            "outcome_justification": "answer grounded in AGENTS.md",
            "completed_steps_laconic": ["read AGENTS.md", "answered"],
            "outcome": "OUTCOME_OK",
        },
    ]


@pytest.mark.parametrize("payload", _sample_function_payloads())
def test_next_step_round_trip_every_variant(payload: dict[str, Any]) -> None:
    step = NextStep(
        current_state="exploring",
        plan_remaining_steps_brief=["verify", "report"],
        identity_verified=True,
        observation="read AGENTS.md, found workspace rules",
        outcome_leaning="GATHERING_INFORMATION",
        function=payload,
    )
    dumped = step.model_dump_json()
    reparsed = NextStep.model_validate_json(dumped)
    assert reparsed.model_dump() == step.model_dump()
    # JSON is canonicalizable: dump → parse → dump is a fixed point.
    assert json.loads(reparsed.model_dump_json()) == json.loads(dumped)


def test_req_models_are_discriminated_by_tool_field() -> None:
    """Each Req_* model must declare a Literal["..."] tool field — otherwise
    Pydantic cannot discriminate the union. Regression guard for schema
    drift during refactors."""
    from typing import get_args

    for model in REQ_MODELS:
        tool_field = model.model_fields["tool"]
        literal_args = get_args(tool_field.annotation)
        assert literal_args and len(literal_args) == 1, (
            f"{model.__name__}.tool must be Literal['...'], got "
            f"{tool_field.annotation}"
        )


def test_req_preflight_unknown_roundtrip():
    from bitgn_contest_agent.schemas import Req_PreflightUnknown
    req = Req_PreflightUnknown(
        tool="preflight_unknown",
        task_text="When was my ambient AI buddy born?",
        workspace_schema_summary="entities_root=10_entities/cast/, projects_root=40_projects/, ...",
        allowed_roots=["10_entities/cast/", "40_projects/", "50_finance/invoices/"],
    )
    assert req.tool == "preflight_unknown"
    js = req.model_dump_json()
    rt = Req_PreflightUnknown.model_validate_json(js)
    assert rt.task_text == req.task_text


def test_rsp_preflight_unknown_roundtrip():
    from bitgn_contest_agent.schemas import Rsp_PreflightUnknown, UnknownRecommendedRoot
    rsp = Rsp_PreflightUnknown(
        likely_class="entity_attribute_lookup",
        clarification_risk_flagged=True,
        clarification_risk_why="descriptor may be ambiguous",
        recommended_roots=[
            UnknownRecommendedRoot(path="10_entities/cast/", why="task references a person"),
        ],
        investigation_plan=["enumerate candidates", "verify unique match"],
        known_pitfalls=["descriptor is not a unique-name match"],
    )
    js = rsp.model_dump_json()
    rt = Rsp_PreflightUnknown.model_validate_json(js)
    assert rt.likely_class == "entity_attribute_lookup"
