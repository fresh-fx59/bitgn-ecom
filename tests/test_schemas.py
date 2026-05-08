"""Round-trip tests for the NextStep Union."""
from __future__ import annotations

import json
from typing import Any

import pytest

from bitgn_contest_agent import schemas
from bitgn_contest_agent.schemas import (
    NextStep,
    REQ_MODELS,
    Req_Context,
    Req_Delete,
    Req_Exec,
    Req_Find,
    Req_List,
    Req_Read,
    Req_Search,
    Req_Stat,
    Req_Tree,
    Req_Write,
    ReportTaskCompletion,
)


def test_module_imports():
    assert hasattr(schemas, "NextStep")
    assert hasattr(schemas, "ReportTaskCompletion")


def _sample_function_payloads() -> list[dict[str, Any]]:
    return [
        {"tool": "read", "path": "/AGENTS.MD"},
        {
            "tool": "read",
            "path": "/data/big.csv",
            "start_line": 1,
            "end_line": 50,
        },
        {"tool": "write", "path": "/tmp/a", "content": "hello"},
        {"tool": "delete", "path": "/tmp/a"},
        {"tool": "list", "path": "/"},
        {"tool": "tree", "root": "/", "level": 2},
        {
            "tool": "find",
            "root": "/",
            "name": "invoice",
            "kind": "files",
            "limit": 10,
        },
        {"tool": "search", "root": "/", "pattern": "TODO", "limit": 10},
        {"tool": "stat", "path": "/AGENTS.MD"},
        {
            "tool": "exec",
            "path": "/bin/sql",
            "args": [],
            "stdin": "SELECT count(*) FROM orders;",
        },
        {"tool": "context"},
        {
            "tool": "report_completion",
            "message": "done",
            "grounding_refs": ["/AGENTS.MD"],
            "rulebook_notes": "followed identity pass",
            "outcome_justification": "answer grounded in /AGENTS.MD",
            "completed_steps_laconic": ["read /AGENTS.MD", "answered"],
            "outcome": "OUTCOME_OK",
        },
    ]


@pytest.mark.parametrize("payload", _sample_function_payloads())
def test_next_step_round_trip_every_variant(payload: dict[str, Any]) -> None:
    step = NextStep(
        current_state="exploring",
        plan_remaining_steps_brief=["verify", "report"],
        identity_verified=True,
        observation="read /AGENTS.MD, found workspace rules",
        outcome_leaning="GATHERING_INFORMATION",
        function=payload,
    )
    dumped = step.model_dump_json()
    reparsed = NextStep.model_validate_json(dumped)
    assert reparsed.model_dump() == step.model_dump()
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


def test_exec_default_args_and_stdin():
    """exec is the only request whose stdin commonly carries the SQL body
    or program input. Round-trip without args/stdin must still validate."""
    req = Req_Exec(tool="exec", path="/bin/sql")
    assert req.args == []
    assert req.stdin == ""


def test_req_preflight_unknown_roundtrip():
    from bitgn_contest_agent.schemas import Req_PreflightUnknown
    req = Req_PreflightUnknown(
        tool="preflight_unknown",
        task_text="How many orders did we ship in March?",
        workspace_schema_summary="orders, customers, products",
        allowed_roots=["/data/orders/", "/data/customers/"],
    )
    assert req.tool == "preflight_unknown"
    js = req.model_dump_json()
    rt = Req_PreflightUnknown.model_validate_json(js)
    assert rt.task_text == req.task_text


def test_rsp_preflight_unknown_roundtrip():
    from bitgn_contest_agent.schemas import (
        Rsp_PreflightUnknown,
        UnknownRecommendedRoot,
    )
    rsp = Rsp_PreflightUnknown(
        likely_class="catalogue_lookup",
        clarification_risk_flagged=True,
        clarification_risk_why="descriptor may be ambiguous",
        recommended_roots=[
            UnknownRecommendedRoot(
                path="/data/orders/", why="task references shipments"
            ),
        ],
        investigation_plan=["enumerate candidates", "verify unique match"],
        known_pitfalls=["descriptor is not a unique-name match"],
    )
    js = rsp.model_dump_json()
    rt = Rsp_PreflightUnknown.model_validate_json(js)
    assert rt.likely_class == "catalogue_lookup"
