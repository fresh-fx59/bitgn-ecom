"""Mechanical contract: the NextStep Union mirrors EcomRuntime RPCs exactly.

If a future bitgn SDK release adds a new *Request type, this test fails
until the Union is updated. Likewise if a Req_* model is added without a
corresponding RPC, we catch it here.

Source of truth on the RPC side: bitgn.vm.ecom.ecom_pb2 — any class whose
name ends with ``Request``.
"""
from __future__ import annotations

import inspect

from bitgn.vm.ecom import ecom_pb2

from bitgn_contest_agent.schemas import REQ_MODELS, ReportTaskCompletion


# The planner emits ReportTaskCompletion and the adapter translates it to
# AnswerRequest on the wire. The coverage test treats AnswerRequest as
# covered by ReportTaskCompletion rather than by a Req_Answer model.
TERMINAL_RPC = "AnswerRequest"

# Internal protobuf plumbing we do not want to inspect.
IGNORED_PROTO_NAMES = frozenset(
    {
        "DESCRIPTOR",
        "SerializedProtobufDescriptor",
    }
)


def _discover_ecom_request_types() -> set[str]:
    names: set[str] = set()
    for name, obj in inspect.getmembers(ecom_pb2):
        if name in IGNORED_PROTO_NAMES:
            continue
        if not inspect.isclass(obj):
            continue
        if not name.endswith("Request"):
            continue
        names.add(name)
    return names


def _req_model_rpc_names() -> set[str]:
    """Map each Req_* model to the proto class name it shadows."""
    mapping: dict[str, str] = {
        "Req_Read": "ReadRequest",
        "Req_Write": "WriteRequest",
        "Req_Delete": "DeleteRequest",
        "Req_List": "ListRequest",
        "Req_Tree": "TreeRequest",
        "Req_Find": "FindRequest",
        "Req_Search": "SearchRequest",
        "Req_Stat": "StatRequest",
        "Req_Exec": "ExecRequest",
        "Req_Context": "ContextRequest",
    }
    names: set[str] = set()
    for model in REQ_MODELS:
        if model.__name__ not in mapping:
            raise AssertionError(
                f"Req_* model {model.__name__} is missing from the coverage "
                f"map in tests/test_tool_coverage.py"
            )
        names.add(mapping[model.__name__])
    return names


def test_ecom_request_types_exactly_covered_by_union():
    rpc_requests = _discover_ecom_request_types()
    covered = _req_model_rpc_names() | {TERMINAL_RPC}

    missing = rpc_requests - covered
    extra = covered - rpc_requests

    assert not missing, (
        f"EcomRuntime exposes {sorted(missing)} but no Req_* model covers them. "
        "Add a new Req_* model in schemas.py and extend REQ_MODELS."
    )
    assert not extra, (
        f"Union covers {sorted(extra)} but EcomRuntime no longer exposes them. "
        "Remove the Req_* model or update the coverage map."
    )


def test_report_task_completion_outcome_matches_proto_enum():
    """Our Literal outcome set must match the proto Outcome enum exactly,
    minus the OUTCOME_UNSPECIFIED placeholder."""
    proto_outcomes = {
        name for name in ecom_pb2.Outcome.keys() if name.startswith("OUTCOME_")
    }
    from typing import get_args

    literal = ReportTaskCompletion.model_fields["outcome"].annotation
    model_outcomes = set(get_args(literal))

    assert model_outcomes == proto_outcomes - {"OUTCOME_UNSPECIFIED"}, (
        f"Outcome mismatch.\n"
        f"  proto (minus UNSPECIFIED): {sorted(proto_outcomes - {'OUTCOME_UNSPECIFIED'})}\n"
        f"  model: {sorted(model_outcomes)}"
    )
