"""Harness wrapper — translates the benchmark 3-step flow."""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from bitgn_contest_agent.harness import BitgnHarness, StartedTask


def test_list_tasks_calls_get_benchmark_and_returns_task_ids() -> None:
    fake_client = MagicMock()
    fake_task = MagicMock()
    fake_task.task_id = "t1"
    fake_task.preview = "do stuff"
    fake_client.get_benchmark.return_value = MagicMock(tasks=[fake_task])

    h = BitgnHarness(
        harness_client=fake_client,
        runtime_client_factory=MagicMock(),
        benchmark="bitgn/pac1-dev",
    )
    task_ids = h.list_task_ids()
    assert task_ids == ["t1"]
    call = fake_client.get_benchmark.call_args.args[0]
    assert call.benchmark_id == "bitgn/pac1-dev"


def test_start_task_calls_start_playground_and_builds_runtime_client() -> None:
    fake_client = MagicMock()
    playground = MagicMock()
    playground.trial_id = "trial-xyz"
    playground.task_id = "t1"
    playground.benchmark_id = "bitgn/pac1-dev"
    playground.instruction = "solve it"
    playground.harness_url = "https://vm.bitgn/t1"
    fake_client.start_playground.return_value = playground

    runtime_factory = MagicMock()
    runtime_factory.return_value = MagicMock(name="runtime")

    h = BitgnHarness(
        harness_client=fake_client,
        runtime_client_factory=runtime_factory,
        benchmark="bitgn/pac1-dev",
    )
    started = h.start_task("t1")
    assert isinstance(started, StartedTask)
    assert started.trial_id == "trial-xyz"
    assert started.instruction == "solve it"
    runtime_factory.assert_called_once_with(playground.harness_url)


def test_end_task_calls_end_trial_and_returns_score() -> None:
    fake_client = MagicMock()
    fake_client.end_trial.return_value = MagicMock(score=0.75, score_detail=[])
    h = BitgnHarness(
        harness_client=fake_client,
        runtime_client_factory=MagicMock(),
        benchmark="bitgn/pac1-dev",
    )
    started = StartedTask(
        trial_id="trial-xyz",
        task_id="t1",
        benchmark_id="bitgn/pac1-dev",
        instruction="...",
        harness_url="...",
        runtime_client=MagicMock(),
    )
    score, detail = h.end_task(started)
    assert score == 0.75
    assert detail == []
    call = fake_client.end_trial.call_args.args[0]
    assert call.trial_id == "trial-xyz"


def test_start_run_posts_raw_json_and_returns_run_id_and_trials() -> None:
    """start_run bypasses the connectrpc client (the local protobuf
    descriptor is stale and has no api_key field) and POSTs raw JSON
    via urllib. Mock urlopen and assert the request envelope shape:

      URL:  {base_url}/bitgn.harness.HarnessService/StartRun
      body: {"benchmark_id": ..., "name": ..., "api_key": ...}
    """
    fake_client = MagicMock()

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = dict(req.headers)
        captured["timeout"] = timeout
        body = json.dumps({
            "runId": "run-42",
            "benchmarkId": "bitgn/pac1-dev",
            "trialIds": ["trial-a", "trial-b", "trial-c"],
        }).encode("utf-8")

        class _Resp(io.BytesIO):
            def __enter__(self_):
                return self_

            def __exit__(self_, *_a):
                return False

        return _Resp(body)

    h = BitgnHarness(
        harness_client=fake_client,
        runtime_client_factory=MagicMock(),
        benchmark="bitgn/pac1-dev",
        base_url="https://api.example.test",
        api_key="bgn-test-key",
    )
    with patch("bitgn_contest_agent.harness.urllib.request.urlopen", side_effect=fake_urlopen):
        run_id, trial_ids = h.start_run(name="plan-b-baseline-abc123-run0")

    assert run_id == "run-42"
    assert trial_ids == ["trial-a", "trial-b", "trial-c"]
    assert captured["url"] == "https://api.example.test/bitgn.harness.HarnessService/StartRun"
    sent = json.loads(captured["data"].decode("utf-8"))
    assert sent == {
        "benchmark_id": "bitgn/pac1-dev",
        "name": "plan-b-baseline-abc123-run0",
        "api_key": "bgn-test-key",
    }
    # urllib normalizes header casing — accept either form
    assert captured["headers"].get("Content-type") == "application/json"
    # connectrpc client must NOT be used for StartRun
    assert not fake_client.start_run.called


def test_start_trial_calls_start_trial_and_builds_runtime_client() -> None:
    fake_client = MagicMock()
    trial = MagicMock()
    trial.trial_id = "trial-a"
    trial.task_id = "t07"
    trial.benchmark_id = "bitgn/pac1-dev"
    trial.instruction = "do the leaderboard thing"
    trial.harness_url = "https://vm.bitgn/trial-a"
    fake_client.start_trial.return_value = trial

    runtime_factory = MagicMock()
    runtime_factory.return_value = MagicMock(name="runtime")

    h = BitgnHarness(
        harness_client=fake_client,
        runtime_client_factory=runtime_factory,
        benchmark="bitgn/pac1-dev",
    )
    started = h.start_trial("trial-a")
    assert isinstance(started, StartedTask)
    assert started.trial_id == "trial-a"
    assert started.task_id == "t07"
    assert started.instruction == "do the leaderboard thing"
    runtime_factory.assert_called_once_with(trial.harness_url)
    req = fake_client.start_trial.call_args.args[0]
    assert req.trial_id == "trial-a"


def test_submit_run_calls_submit_run_and_returns_state() -> None:
    fake_client = MagicMock()
    fake_client.submit_run.return_value = MagicMock(run_id="run-42", state="SUBMITTED")
    h = BitgnHarness(
        harness_client=fake_client,
        runtime_client_factory=MagicMock(),
        benchmark="bitgn/pac1-dev",
    )
    state = h.submit_run("run-42")
    assert state == "SUBMITTED"
    req = fake_client.submit_run.call_args.args[0]
    assert req.run_id == "run-42"
    assert req.force is False
