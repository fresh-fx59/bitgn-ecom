"""Provider-agnostic backend protocol.

The planner only ever talks to Backend.next_step — it never knows which
provider is in use. A second backend (anthropic_compat, etc.) is a new
file, not a refactor.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, TypeVar, runtime_checkable

from pydantic import BaseModel

from bitgn_contest_agent.schemas import NextStep

_T = TypeVar("_T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class Message:
    role: str                                 # "system" | "user" | "assistant" | "tool"
    content: str | None = None                # nullable on assistant-with-tool_calls turns
    reasoning: str | None = None              # gpt-oss CoT to replay on next turn
    tool_calls: list[dict] | None = None      # raw tool_calls from prior assistant turn
    tool_call_id: str | None = None           # pairs tool-result to its originating call


@dataclass(frozen=True, slots=True)
class NextStepResult:
    """Wraps a parsed NextStep with token accounting from the provider."""
    parsed: "NextStep"  # type: ignore[name-defined]
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    reasoning: str | None = None
    tool_calls: list[dict] | None = None


class TransientBackendError(Exception):
    """Rate limit, 5xx, or network timeout. Caller retries with backoff."""

    def __init__(self, message: str, *, attempt: int = 0) -> None:
        super().__init__(message)
        self.attempt = attempt


@runtime_checkable
class Backend(Protocol):
    def next_step(
        self,
        messages: Sequence[Message],
        response_schema: type[NextStep],
        timeout_sec: float,
    ) -> NextStepResult:
        ...

    def call_structured(
        self, prompt: str, response_schema: type[_T], *, timeout_sec: float = 30.0,
    ) -> _T:
        """One-shot structured call — takes a text prompt and a Pydantic
        schema, returns an instance of that schema. Used by preflight
        tools that need LLM classification without the full message-list
        plumbing of next_step."""
        ...
