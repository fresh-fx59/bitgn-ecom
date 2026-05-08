"""Pydantic schemas for the planner tool surface.

Single source of truth: the NextStep Union mirrors the PcmRuntime RPC
surface exactly. The coverage test in tests/test_tool_coverage.py keeps
this correspondence mechanical.
"""
from __future__ import annotations

from typing import Annotated, List, Literal, Union

from pydantic import BaseModel, Field
from pydantic.types import StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class Req_Read(BaseModel):
    tool: Literal["read"]
    path: NonEmptyStr


class Req_Write(BaseModel):
    tool: Literal["write"]
    path: NonEmptyStr
    content: str


class Req_Delete(BaseModel):
    tool: Literal["delete"]
    path: NonEmptyStr


class Req_MkDir(BaseModel):
    tool: Literal["mkdir"]
    path: NonEmptyStr


class Req_Move(BaseModel):
    tool: Literal["move"]
    from_name: NonEmptyStr
    to_name: NonEmptyStr


class Req_List(BaseModel):
    tool: Literal["list"]
    name: NonEmptyStr


class Req_Tree(BaseModel):
    tool: Literal["tree"]
    root: NonEmptyStr


class Req_Find(BaseModel):
    tool: Literal["find"]
    root: NonEmptyStr
    name: str = ""
    type: Literal["TYPE_ALL", "TYPE_FILES", "TYPE_DIRS"] = "TYPE_ALL"
    limit: int = Field(default=100, ge=1, le=10_000)


class Req_Search(BaseModel):
    tool: Literal["search"]
    root: NonEmptyStr
    pattern: NonEmptyStr
    limit: int = Field(default=100, ge=1, le=10_000)


class Req_Context(BaseModel):
    tool: Literal["context"]


class Req_PreflightSchema(BaseModel):
    """Discover the workspace layout (roots and roles). Always safe to call."""
    tool: Literal["preflight_schema"]


class Req_PreflightSemanticIndex(BaseModel):
    """Emit a compact per-record digest of cast and projects so the agent
    can match informal descriptors (role phrases, lane labels) against
    canonical IDs. Runs once per task in the prepass, after schema
    discovery. Always safe to call.
    """
    tool: Literal["preflight_semantic_index"]


class ReportTaskCompletion(BaseModel):
    tool: Literal["report_completion"]
    message: NonEmptyStr
    grounding_refs: List[str]
    rulebook_notes: NonEmptyStr
    outcome_justification: NonEmptyStr
    completed_steps_laconic: List[str]
    outcome: Literal[
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    ]


FunctionUnion = Annotated[
    Union[
        Req_Read,
        Req_Write,
        Req_Delete,
        Req_MkDir,
        Req_Move,
        Req_List,
        Req_Tree,
        Req_Find,
        Req_Search,
        Req_Context,
        Req_PreflightSchema,
        Req_PreflightSemanticIndex,
        ReportTaskCompletion,
    ],
    Field(discriminator="tool"),
]


ReadOnlyFunctionUnion = Annotated[
    Union[
        Req_Read,
        Req_List,
        Req_Tree,
        Req_Find,
        Req_Search,
        Req_Context,
    ],
    Field(discriminator="tool"),
]


READ_ONLY_REQ_TYPES: tuple[type[BaseModel], ...] = (
    Req_Read,
    Req_List,
    Req_Tree,
    Req_Find,
    Req_Search,
    Req_Context,
)


class NextStep(BaseModel):
    current_state: NonEmptyStr
    plan_remaining_steps_brief: Annotated[List[str], Field(min_length=1, max_length=5)]
    identity_verified: bool
    observation: NonEmptyStr
    outcome_leaning: Literal[
        "GATHERING_INFORMATION",
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
    ]
    function: FunctionUnion = Field(..., discriminator="tool")
    parallel_reads: Annotated[
        List[ReadOnlyFunctionUnion],
        Field(
            default_factory=list,
            max_length=8,
            description=(
                "Optional batch of additional read-only ops "
                "(read/list/tree/find/search/context) dispatched in "
                "parallel with `function`. Only honored when `function` "
                "is itself a read-only op. Use this to collapse multiple "
                "independent reads into a single LLM turn — every entry "
                "must be independent of the others (no entry's choice "
                "depends on another's result). Never include "
                "writes/deletes/moves/report_completion."
            ),
        ),
    ]


# Convenience: the set of all Req_* model classes, in canonical order.
REQ_MODELS: tuple[type[BaseModel], ...] = (
    Req_Read,
    Req_Write,
    Req_Delete,
    Req_MkDir,
    Req_Move,
    Req_List,
    Req_Tree,
    Req_Find,
    Req_Search,
    Req_Context,
    Req_PreflightSchema,
    Req_PreflightSemanticIndex,
)


class Req_PreflightUnknown(BaseModel):
    """Fires when the router returns UNKNOWN (no bound skill). The
    preflight classifies the task and emits a structured investigation
    scaffold so the agent doesn't cold-start exploration.
    """
    tool: Literal["preflight_unknown"] = "preflight_unknown"
    task_text: str
    workspace_schema_summary: str
    # Allowed roots constrain the LLM's recommended_roots — it can only
    # point at paths that actually exist in the workspace schema. This
    # is the hallucination guard.
    allowed_roots: list[str] = Field(default_factory=list)


class UnknownRecommendedRoot(BaseModel):
    path: str
    why: str


class Rsp_PreflightUnknown(BaseModel):
    """Structured scaffold the preflight emits for the agent."""
    likely_class: Literal[
        "entity_attribute_lookup",
        "inbox_processing",
        "security_refusal",
        "cleanup_receipts",
        "ambiguous_referent",
        "other",
    ]
    clarification_risk_flagged: bool
    clarification_risk_why: str = ""
    recommended_roots: list[UnknownRecommendedRoot] = Field(default_factory=list)
    investigation_plan: list[str] = Field(default_factory=list)
    known_pitfalls: list[str] = Field(default_factory=list)
