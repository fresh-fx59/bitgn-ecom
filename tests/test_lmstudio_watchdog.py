"""Tests for the LM Studio per-request watchdog.

Covers:
- Timer fires and calls unload when the body runs past the deadline.
- Timer is cancelled on normal exit; unload is never called.
- Timer is cancelled on an exception in the body; unload is never called.
- Unload exceptions inside the timer callback are swallowed (best-effort).
- End-to-end: OpenAIToolCallingBackend.next_step wraps create() with the
  watchdog guard when the adapter exposes an lmstudio_host, and does NOT
  wrap when lmstudio_host is None.

We stub the ``lmstudio.Client`` at module-level to keep these tests
offline — no LM Studio required.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bitgn_contest_agent.backend import lmstudio_watchdog
from bitgn_contest_agent.backend.adapters.gpt_oss import GptOssAdapter
from bitgn_contest_agent.backend.adapters.qwen_a3b_remote import (
    QwenA3bRemoteAdapter,
)
from bitgn_contest_agent.backend.base import Message
from bitgn_contest_agent.backend.openai_toolcalling import (
    OpenAIToolCallingBackend,
)
from bitgn_contest_agent.schemas import NextStep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeLmsClient:
    """Stand-in for ``lmstudio.Client`` used by the watchdog.

    Records calls to ``.llm.unload(model)`` on an attached tracker so tests
    can assert whether unload fired without needing a live LM Studio.
    """

    def __init__(self, host: str, tracker: dict) -> None:
        tracker["host"] = host
        self._tracker = tracker
        self.llm = SimpleNamespace(unload=self._unload)

    def _unload(self, model: str) -> None:
        self._tracker.setdefault("unload_calls", []).append(model)
        if self._tracker.get("raise_on_unload"):
            raise RuntimeError("boom")

    def close(self) -> None:
        self._tracker["closed"] = True


@pytest.fixture
def fake_lms(monkeypatch):
    """Swap ``lmstudio.Client`` with a recorder; return the tracker dict."""
    tracker: dict = {}

    def factory(host: str):
        return _FakeLmsClient(host, tracker)

    monkeypatch.setattr(lmstudio_watchdog.lms, "Client", factory)
    return tracker


# ---------------------------------------------------------------------------
# Timer semantics
# ---------------------------------------------------------------------------


def test_guard_fires_unload_when_body_exceeds_deadline(fake_lms) -> None:
    """Body sleeps past the deadline → timer fires → unload is called."""
    with lmstudio_watchdog.guard(
        request_id="rid1",
        model="qwen3.5-35b-a3b",
        host="localhost:1236",
        deadline_sec=0.05,
    ):
        time.sleep(0.2)  # past the deadline

    # Give the Timer thread a moment to complete the unload.
    for _ in range(50):
        if fake_lms.get("unload_calls"):
            break
        time.sleep(0.02)

    assert fake_lms.get("unload_calls") == ["qwen3.5-35b-a3b"]
    assert fake_lms["host"] == "localhost:1236"


def test_guard_cancels_timer_on_normal_exit(fake_lms) -> None:
    """Body returns fast → timer cancelled → unload never called."""
    with lmstudio_watchdog.guard(
        request_id="rid2",
        model="qwen3.5-35b-a3b",
        host="localhost:1236",
        deadline_sec=1.0,
    ):
        pass  # immediate exit

    # Wait long enough for the timer to have fired had it not been cancelled.
    time.sleep(0.15)
    assert "unload_calls" not in fake_lms


def test_guard_cancels_timer_on_body_exception(fake_lms) -> None:
    """An exception inside the body still cancels the timer on exit."""
    with pytest.raises(RuntimeError, match="inside body"):
        with lmstudio_watchdog.guard(
            request_id="rid3",
            model="qwen3.5-35b-a3b",
            host="localhost:1236",
            deadline_sec=1.0,
        ):
            raise RuntimeError("inside body")

    time.sleep(0.15)
    assert "unload_calls" not in fake_lms


def test_unload_exception_is_swallowed(fake_lms) -> None:
    """If unload raises inside the Timer thread, the main thread is not
    affected. The HTTP timeout has already surfaced; best-effort is fine."""
    fake_lms["raise_on_unload"] = True

    with lmstudio_watchdog.guard(
        request_id="rid4",
        model="qwen3.5-35b-a3b",
        host="localhost:1236",
        deadline_sec=0.05,
    ):
        time.sleep(0.2)

    for _ in range(50):
        if fake_lms.get("unload_calls"):
            break
        time.sleep(0.02)
    # Unload was attempted, and no exception propagated from the guard.
    assert fake_lms.get("unload_calls") == ["qwen3.5-35b-a3b"]


def test_force_unload_calls_sdk(fake_lms) -> None:
    """Public helper used by the operator CLI."""
    lmstudio_watchdog.force_unload("localhost:1236", "qwen3.5-35b-a3b")
    assert fake_lms.get("unload_calls") == ["qwen3.5-35b-a3b"]
    assert fake_lms["host"] == "localhost:1236"


# ---------------------------------------------------------------------------
# Backend wiring
# ---------------------------------------------------------------------------


def _mk_completion_with_tool_call(tool_name: str, arguments: dict) -> MagicMock:
    tc = MagicMock()
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    completion.usage = MagicMock(
        prompt_tokens=1,
        completion_tokens=1,
        completion_tokens_details=MagicMock(reasoning_tokens=0),
    )
    return completion


def _tool_args() -> dict:
    return {
        "current_state": "s",
        "plan_remaining_steps_brief": ["x"],
        "identity_verified": False,
        "observation": "o",
        "outcome_leaning": "GATHERING_INFORMATION",
        "path": "AGENTS.md",
    }


def test_backend_arms_guard_for_lmstudio_adapter(fake_lms) -> None:
    """When adapter.profile.lmstudio_host is set, next_step must wrap the
    create() call in lmstudio_watchdog.guard(...). We verify via a patch
    that records entry/exit with arguments consistent with the adapter's
    timeout + 10s grace."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = \
        _mk_completion_with_tool_call("read", _tool_args())

    adapter = GptOssAdapter()
    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="openai/gpt-oss-20b",
        reasoning_effort="high",
        adapter=adapter,
    )

    with patch.object(
        lmstudio_watchdog, "guard", wraps=lmstudio_watchdog.guard,
    ) as spy:
        backend.next_step(
            messages=[Message(role="user", content="t")],
            response_schema=NextStep,
            timeout_sec=5.0,
        )

    assert spy.call_count == 1
    kwargs = spy.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-oss-20b"
    assert kwargs["host"] == "localhost:1236"
    # Deadline is llm_http_timeout_sec MINUS 10s: watchdog must fire before
    # the HTTP client raises, otherwise the context-manager finally cancels
    # the Timer before unload can run. Observed 2026-04-22 on first PROD
    # launch with the +10s variant — 4 consecutive tasks wedged, zero
    # WATCHDOG FIRED log lines.
    assert kwargs["deadline_sec"] == adapter.profile.llm_http_timeout_sec - 10.0
    # request_id is a short hex id, non-empty.
    assert isinstance(kwargs["request_id"], str) and len(kwargs["request_id"]) >= 6


def test_backend_no_guard_for_remote_adapter(fake_lms) -> None:
    """qwen3.6 via the neuraldeep gateway has lmstudio_host=None; the
    backend must NOT call guard() on that path (no LM Studio to unload)."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = \
        _mk_completion_with_tool_call("read", _tool_args())

    adapter = QwenA3bRemoteAdapter()
    assert adapter.profile.lmstudio_host is None

    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="qwen3.6-35b-a3b",
        reasoning_effort="high",
        adapter=adapter,
    )

    with patch.object(lmstudio_watchdog, "guard") as spy:
        backend.next_step(
            messages=[Message(role="user", content="t")],
            response_schema=NextStep,
            timeout_sec=5.0,
        )

    spy.assert_not_called()
    # And no accidental unload either.
    assert "unload_calls" not in fake_lms


def test_backend_call_structured_also_wraps(fake_lms) -> None:
    """call_structured uses beta.chat.completions.parse. It must also be
    guarded — the classifier probe can trigger the same runaway."""
    fake_client = MagicMock()
    parsed_obj = MagicMock()
    fake_client.beta.chat.completions.parse.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(parsed=parsed_obj, content=""))],
    )

    adapter = GptOssAdapter()
    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="openai/gpt-oss-20b",
        reasoning_effort="high",
        adapter=adapter,
    )

    class _Schema:
        @classmethod
        def model_validate_json(cls, raw: str):
            return parsed_obj

    with patch.object(
        lmstudio_watchdog, "guard", wraps=lmstudio_watchdog.guard,
    ) as spy:
        backend.call_structured("test prompt", _Schema, timeout_sec=5.0)

    assert spy.call_count == 1
    assert spy.call_args.kwargs["host"] == "localhost:1236"
