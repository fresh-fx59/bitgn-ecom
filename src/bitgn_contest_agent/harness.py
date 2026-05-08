"""Thin wrapper around the BitGN HarnessService.

Two supported flows:

  Leaderboard flow (used by run-benchmark, produces dashboard-visible runs):
    1. start_run(name)      → (run_id, trial_ids[])      — reserves trials
    2. start_trial(trial_id) → StartedTask               — provisions runtime
    3. end_trial(trial_id)   → score                     — per-trial grading
    4. submit_run(run_id)    → run state                 — finalizes leaderboard

  Playground flow (used by run-task for ad-hoc single-task debugging):
    1. get_benchmark(...)       → list task ids
    2. start_playground(task_id) → StartedTask           — ad-hoc trial
    3. end_trial(trial_id)       → score

StartPlayground trials are invisible to the leaderboard dashboard; only
StartRun trials appear. Anything that must show up in the contest UI
must go through the leaderboard flow.

Authentication is a ConnectRPC metadata interceptor (taken from the
sibling bitgn_pac1_adapter.py).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Tuple

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    GetBenchmarkRequest,
    GetRunRequest,
    GetTrialRequest,
    StartPlaygroundRequest,
    StartTrialRequest,
    SubmitRunRequest,
)
# NOTE: StartRunRequest is intentionally NOT imported. The locally
# installed bitgn wheel ships a stale protobuf descriptor whose
# StartRunRequest is {benchmark_id, name} and is missing the `api_key`
# field (upstream proto has `string api_key = 3;`). The live server
# requires api_key in the JSON request body (verified 2026-04-11 via
# curl probe — Bearer header returns 401 "missing BitGN API Key").
# Until the wheel is refreshed we bypass the connectrpc client for
# StartRun only and POST raw JSON via urllib. Every other RPC
# (StartTrial, EndTrial, SubmitRun, GetBenchmark, StartPlayground)
# continues to use the normal connectrpc client — those RPCs have not
# changed shape and StartTrial/EndTrial/SubmitRun self-authenticate
# via the trial_id / run_id in their request body.
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
# PLAN DEVIATION: the plan imports MetadataInterceptorSync from
# connectrpc.client_sync, but the installed connectrpc wheel exposes it
# under connectrpc.interceptor. The sibling bitgn_pac1_adapter.py used
# an older path. Verified via pkgutil.iter_modules on 2026-04-10.
from connectrpc.interceptor import MetadataInterceptorSync  # type: ignore[import-not-found]


class _AuthHeaderInterceptor(MetadataInterceptorSync[None]):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def on_start_sync(self, ctx: Any) -> None:  # pragma: no cover — thin glue
        ctx.request_headers()["authorization"] = f"Bearer {self._api_key}"
        return None


@dataclass(frozen=True, slots=True)
class StartedTask:
    trial_id: str
    task_id: str
    benchmark_id: str
    instruction: str
    harness_url: str
    runtime_client: PcmRuntimeClientSync


class BitgnHarness:
    def __init__(
        self,
        *,
        harness_client: HarnessServiceClientSync,
        runtime_client_factory: Callable[[str], PcmRuntimeClientSync],
        benchmark: str,
        base_url: str = "",
        api_key: str = "",
    ) -> None:
        self._harness = harness_client
        self._runtime_factory = runtime_client_factory
        self._benchmark = benchmark
        # Used only by start_run's raw JSON POST path — see module
        # docstring for why StartRun bypasses the connectrpc client.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @classmethod
    def from_env(cls, *, benchmark: str, bitgn_base_url: str, bitgn_api_key: str) -> "BitgnHarness":
        interceptors = (_AuthHeaderInterceptor(bitgn_api_key),)
        harness_client = HarnessServiceClientSync(bitgn_base_url, interceptors=interceptors)
        return cls(
            harness_client=harness_client,
            runtime_client_factory=lambda url: PcmRuntimeClientSync(url, interceptors=interceptors),
            benchmark=benchmark,
            base_url=bitgn_base_url,
            api_key=bitgn_api_key,
        )

    def _connect_post_json(self, method: str, body: Mapping[str, Any]) -> dict:
        """POST a raw Connect-RPC unary call as JSON and return the parsed response.

        Used exclusively by `start_run` to work around the stale local
        protobuf descriptor that lacks the `api_key` field on
        StartRunRequest. Connect-RPC unary calls are just HTTP POST
        with `content-type: application/json` at
        `{base_url}/{package.Service}/{Method}`, and the server
        tolerates unknown fields in the JSON body regardless of what
        our compiled descriptor declares.
        """
        url = f"{self._base_url}/bitgn.harness.HarnessService/{method}"
        data = json.dumps(dict(body)).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "content-type": "application/json",
                "accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"BitGN {method} HTTP {exc.code}: {detail}"
            ) from exc
        return json.loads(payload.decode("utf-8"))

    def list_task_ids(self) -> List[str]:
        resp = self._harness.get_benchmark(GetBenchmarkRequest(benchmark_id=self._benchmark))
        return [t.task_id for t in resp.tasks]

    def start_task(self, task_id: str) -> StartedTask:
        """Playground flow: ad-hoc single-task trial, NOT attached to a run.

        Used by `run-task` for debugging. These trials are invisible to
        the leaderboard dashboard — for dashboard visibility use
        `start_run` + `start_trial` instead.
        """
        resp = self._harness.start_playground(
            StartPlaygroundRequest(benchmark_id=self._benchmark, task_id=task_id)
        )
        runtime = self._runtime_factory(resp.harness_url)
        return StartedTask(
            trial_id=resp.trial_id,
            task_id=resp.task_id,
            benchmark_id=resp.benchmark_id,
            instruction=resp.instruction,
            harness_url=resp.harness_url,
            runtime_client=runtime,
        )

    def start_run(self, *, name: str) -> Tuple[str, List[str]]:
        """Leaderboard flow step 1: reserve a run and its pre-populated trials.

        Returns (run_id, trial_ids). Trials are not yet executing — call
        `start_trial(trial_id)` per trial to provision per-task runtime.
        The task_id is not returned here; it is revealed by start_trial.

        Bypasses the connectrpc client and POSTs raw JSON so we can
        include the `api_key` field that the live server requires and
        the stale local proto descriptor does not know about. The
        response is Connect-RPC's canonical JSON shape with camelCase
        field names (`runId`, `trialIds`).
        """
        resp = self._connect_post_json(
            "StartRun",
            {
                "benchmark_id": self._benchmark,
                "name": name,
                "api_key": self._api_key,
            },
        )
        run_id = resp.get("runId") or resp.get("run_id") or ""
        trial_ids = resp.get("trialIds") or resp.get("trial_ids") or []
        return str(run_id), [str(t) for t in trial_ids]

    def start_trial(self, trial_id: str) -> StartedTask:
        """Leaderboard flow step 2: provision runtime for a reserved trial.

        Server wall-clock for the trial begins here; call this lazily
        from the worker (not at orchestration time) so queued trials do
        not burn their deadline while waiting for a parallel slot.
        """
        resp = self._harness.start_trial(StartTrialRequest(trial_id=trial_id))
        runtime = self._runtime_factory(resp.harness_url)
        return StartedTask(
            trial_id=resp.trial_id,
            task_id=resp.task_id,
            benchmark_id=resp.benchmark_id,
            instruction=resp.instruction,
            harness_url=resp.harness_url,
            runtime_client=runtime,
        )

    def end_task(self, started: StartedTask) -> Tuple[float, list[Any]]:
        """Shared between playground and leaderboard flows: grade one trial."""
        resp = self._harness.end_trial(EndTrialRequest(trial_id=started.trial_id))
        return float(resp.score), list(resp.score_detail)

    def get_run(self, run_id: str):
        """Return the server's view of a run (trial states, scores, stats).

        Used by resume.py to compute which trials still need work after
        a process crash or relaunch. Does not mutate server state.
        """
        return self._harness.get_run(GetRunRequest(run_id=run_id))

    def get_trial(self, trial_id: str):
        """Return the server's view of a single trial (instruction, logs, state).

        Used by resume.py to recover the instruction text for RUNNING
        trials without calling start_trial (which would reset the
        server wall-clock).
        """
        return self._harness.get_trial(GetTrialRequest(trial_id=trial_id))

    def end_task_by_id(self, trial_id: str) -> Tuple[float, list[Any]]:
        """Grade a trial when we only have its id (no StartedTask).

        Symmetric with end_task() but takes a bare trial_id. Used by
        the resume path to attempt a best-effort grade of a RUNNING
        trial left over from a previous process.
        """
        resp = self._harness.end_trial(EndTrialRequest(trial_id=trial_id))
        return float(resp.score), list(resp.score_detail)

    def submit_run(self, run_id: str, *, force: bool = False) -> str:
        """Leaderboard flow step 4: finalize run on the leaderboard.

        Must be called after all trials in the run have been ended.
        Returns the server-reported run state (opaque string).
        """
        resp = self._harness.submit_run(SubmitRunRequest(run_id=run_id, force=force))
        return str(resp.state)
