"""Pydantic schemas for the planner tool surface.

Single source of truth: the `NextStep` Union mirrors the EcomRuntime RPC
surface exactly. The coverage test in tests/test_tool_coverage.py keeps
this correspondence mechanical.

ECOM tool surface (vs the PAC1 lineage this is forked from):

  Added:    stat, exec
  Removed:  mkdir, move (not part of the ECOM RPC surface)
  Removed:  context (retired at the 2026-05-15 API freeze — actor
            identity moved to exec(/bin/id), trial date to exec(/bin/date))
  Removed:  preflight_schema, preflight_semantic_index
            (workspace-schema/semantic-index discovery was a vault-only
            concept; ECOM tasks ground via tree+/AGENTS.MD+/bin/id+/bin/date)
  Adjusted: read gains start_line / end_line / number (line slicing)
            list keys on `path` (was `name`)
            tree gains `level` cap
            find keys on `kind` (was `type`); allowed values are
            "all" / "files" / "dirs"
"""
from __future__ import annotations

from typing import Annotated, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field
from pydantic.types import StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


# ── TaskSpec (v0.1.98 P1) ────────────────────────────────────────────
# Optional structured restatement of the task. Used by post-pass
# completers to resolve catalogue SKUs deterministically instead of
# regex-parsing the agent's natural-language report. Soft-validated
# at parse time: malformed task_spec is dropped, the rest of the
# completion stays. See sku_completer module.
#
# Schema is intentionally FLAT (single model, kind=enum) rather than
# a discriminated union — the tool catalog has a no-oneOf/anyOf
# invariant (test_tool_catalog_no_oneof_nodes) because small local
# models fail on JSON Schema with unions. Empty fields outside the
# `kind` variant's scope are ignored by the completer.
class ProductFilter(BaseModel):
    brand: str = Field(description="Brand name as it appears in the task")
    series: str = Field(
        default="",
        description="Series / line name (incl. model code if part of the line)",
    )
    model: str = Field(default="", description="Model code")
    name: str = Field(
        default="",
        description="Product category name (e.g. 'Cordless Drill Driver')",
    )
    attributes: Dict[str, str] = Field(
        default_factory=dict,
        description="Snake_case property key → value as named in the task",
    )


class TaskSpec(BaseModel):
    kind: Literal[
        "none", "count_per_store", "catalogue_count", "yes_no_sku"
    ] = Field(
        default="none",
        description=(
            "Which shape: 'count_per_store' for 'How many of these "
            "products have at least N available at <store>: ...'; "
            "'catalogue_count' for 'How many catalogue products are "
            "X?' / 'How many X products report today?'; "
            "'yes_no_sku' for 'A support note claims we stock the X "
            "from <brand> in the <line> ... Check ...'; 'none' (the "
            "default) when the task is none of the above."
        ),
    )
    # count_per_store fields
    store_descriptor: str = Field(
        default="",
        description="The city descriptor or store name from task text",
    )
    threshold: int = Field(
        default=0,
        ge=0,
        description="`at least N items` value for count_per_store",
    )
    products: List[ProductFilter] = Field(
        default_factory=list,
        description="Parsed product list for count_per_store",
    )
    # catalogue_count fields
    category_human: str = Field(
        default="",
        description=(
            "Verbatim category as named in task (e.g. 'Pliers and "
            "Wrenches'). Required when kind='catalogue_count'."
        ),
    )


class Req_Read(BaseModel):
    tool: Literal["read"]
    path: NonEmptyStr
    number: bool = Field(
        default=False,
        description="Return 1-based line numbers in the output (`cat -n`).",
    )
    start_line: int = Field(
        default=0,
        ge=0,
        description="1-based inclusive start line; 0 means from the first line.",
    )
    end_line: int = Field(
        default=0,
        ge=0,
        description="1-based inclusive end line; 0 means through the last line.",
    )


class Req_Write(BaseModel):
    tool: Literal["write"]
    path: NonEmptyStr
    content: str


class Req_Delete(BaseModel):
    tool: Literal["delete"]
    path: NonEmptyStr


class Req_List(BaseModel):
    tool: Literal["list"]
    path: NonEmptyStr


class Req_Tree(BaseModel):
    tool: Literal["tree"]
    root: NonEmptyStr
    level: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Max tree depth; 0 means unlimited.",
    )


class Req_Find(BaseModel):
    tool: Literal["find"]
    name: NonEmptyStr
    root: str = "/"
    kind: Literal["all", "files", "dirs"] = "all"
    limit: int = Field(default=10, ge=1, le=20)


class Req_Search(BaseModel):
    tool: Literal["search"]
    pattern: NonEmptyStr
    root: str = "/"
    limit: int = Field(default=10, ge=1, le=20)


class Req_Stat(BaseModel):
    tool: Literal["stat"]
    path: NonEmptyStr


class Req_Exec(BaseModel):
    tool: Literal["exec"]
    path: NonEmptyStr = Field(
        description=(
            "Absolute path of the executable (e.g. `/bin/sql` for catalogue "
            "queries, `/bin/id` for actor identity, `/bin/date` for the "
            "current trial date). See /AGENTS.MD for the live inventory."
        ),
    )
    args: List[str] = Field(default_factory=list)
    stdin: str = Field(
        default="",
        description=(
            "Standard input fed to the program. For `/bin/sql` this is the "
            "SQL query body."
        ),
    )


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
    task_spec: TaskSpec = Field(
        default_factory=TaskSpec,
        description=(
            "Optional structured restatement of the task. Set "
            "task_spec.kind='count_per_store' on multi-product "
            "availability tasks ('How many of these products have at "
            "least N available at <store>: <list>') and fill "
            "store_descriptor + threshold + products. Set "
            "task_spec.kind='catalogue_count' on catalogue-count "
            "tasks ('How many catalogue products are X?' or 'How "
            "many X products [should I] report') and fill "
            "category_human. Leave kind='none' otherwise. The "
            "post-pass uses this to verify your cited SKUs / addenda."
        ),
    )


FunctionUnion = Annotated[
    Union[
        Req_Read,
        Req_Write,
        Req_Delete,
        Req_List,
        Req_Tree,
        Req_Find,
        Req_Search,
        Req_Stat,
        Req_Exec,
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
        Req_Stat,
    ],
    Field(discriminator="tool"),
]


READ_ONLY_REQ_TYPES: tuple[type[BaseModel], ...] = (
    Req_Read,
    Req_List,
    Req_Tree,
    Req_Find,
    Req_Search,
    Req_Stat,
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
                "(read/list/tree/find/search/stat) dispatched in "
                "parallel with `function`. Only honored when `function` "
                "is itself a read-only op. Use this to collapse multiple "
                "independent reads into a single LLM turn — every entry "
                "must be independent of the others (no entry's choice "
                "depends on another's result). Never include "
                "writes/deletes/exec/report_completion."
            ),
        ),
    ]


# Convenience: the set of all Req_* model classes, in canonical order.
REQ_MODELS: tuple[type[BaseModel], ...] = (
    Req_Read,
    Req_Write,
    Req_Delete,
    Req_List,
    Req_Tree,
    Req_Find,
    Req_Search,
    Req_Stat,
    Req_Exec,
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
        "catalogue_lookup",
        "sql_aggregation",
        "data_correction",
        "security_refusal",
        "ambiguous_referent",
        "other",
    ]
    clarification_risk_flagged: bool
    clarification_risk_why: str = ""
    recommended_roots: list[UnknownRecommendedRoot] = Field(default_factory=list)
    investigation_plan: list[str] = Field(default_factory=list)
    known_pitfalls: list[str] = Field(default_factory=list)
