"""Tests for the backend Protocol and its error taxonomy."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bitgn_contest_agent.backend.base import (
    Backend,
    Message,
    NextStepResult,
    TransientBackendError,
)
from bitgn_contest_agent.schemas import NextStep


def test_message_is_frozen_dataclass() -> None:
    msg = Message(role="user", content="hi")
    with pytest.raises(FrozenInstanceError):
        msg.content = "bye"  # type: ignore[misc]


def test_transient_backend_error_is_exception_subclass() -> None:
    assert issubclass(TransientBackendError, Exception)
    err = TransientBackendError("rate limit", attempt=2)
    assert err.attempt == 2
    assert "rate limit" in str(err)


def test_backend_protocol_is_runtime_checkable() -> None:
    class Fake:
        def next_step(self, messages, response_schema, timeout_sec):  # type: ignore[override]
            return NextStepResult(
                parsed=NextStep(
                    current_state="x",
                    plan_remaining_steps_brief=["done"],
                    identity_verified=True,
                    observation="context loaded",
                    outcome_leaning="GATHERING_INFORMATION",
                    function={"tool": "context"},
                ),
                prompt_tokens=0,
                completion_tokens=0,
                reasoning_tokens=0,
            )

        def call_structured(self, prompt, response_schema, *, timeout_sec=30.0):  # type: ignore[override]
            return response_schema.model_construct()

    assert isinstance(Fake(), Backend)
