# src/bitgn_scraper/phase0.py
"""Phase 0 lifecycle spike — answers the spec's empirical open questions.

Sub-spikes:
  1. rotation              — does StartPlayground rotate instruction text?
  2. url_lifetime          — how long does harness_url stay reachable post-EndTrial?
  3. auto_termination      — does an unended trial auto-terminate? At what age?
  4. state_isolation       — does a write in trial N persist to trial N+1?
  5. answer_replay         — does the grader use the first or last Answer?
  6. rate_limit            — what concurrency level triggers throttling?
  7. size_sanity           — how big are the largest workspaces?

Output: artifacts/harness_db/scrape_runs/<ts>/lifecycle_spike.json
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RotationFinding:
    task_id: str
    n_calls: int
    distinct_instructions: int
    sample_instructions: list[str]


@dataclass(frozen=True)
class UrlLifetimeFinding:
    trial_id: str
    harness_url: str
    probe_offsets_seconds: list[int]
    reachable_at_offset: list[bool]


@dataclass(frozen=True)
class AutoTerminationFinding:
    trial_id: str
    probe_offsets_seconds: list[int]
    reachable_at_offset: list[bool]
    inferred_max_lifetime_seconds: int | None


@dataclass(frozen=True)
class StateIsolationFinding:
    wrote_path: str
    second_trial_saw_write: bool


@dataclass(frozen=True)
class AnswerReplayFinding:
    first_answer: str
    second_answer: str
    graded_against: str  # "first" | "second" | "unknown"


@dataclass(frozen=True)
class RateLimitFinding:
    n_parallel_calls: int
    n_throttled: int
    throttle_status_codes: list[int]


@dataclass(frozen=True)
class SizeSanityFinding:
    sampled_task_ids: list[str]
    byte_totals: list[int]
    max_byte_total: int


@dataclass(frozen=True)
class LifecycleReport:
    started_at: datetime
    rotation: RotationFinding
    url_lifetime: UrlLifetimeFinding
    auto_termination: AutoTerminationFinding
    state_isolation: StateIsolationFinding
    answer_replay: AnswerReplayFinding
    rate_limit: RateLimitFinding
    size_sanity: SizeSanityFinding


def serialize_report(report: LifecycleReport) -> str:
    """JSON-encode a LifecycleReport with ISO datetime."""
    payload: dict[str, Any] = asdict(report)
    payload["started_at"] = report.started_at.isoformat()
    return json.dumps(payload, indent=2, sort_keys=True)


def _spike_rotation(client: Any, task_id: str, n_calls: int) -> RotationFinding:
    """Sub-spike 1 — rotation detection."""
    from bitgn.harness_pb2 import StartPlaygroundRequest

    instructions: list[str] = []
    for _ in range(n_calls):
        resp = client.start_playground(
            StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
        )
        instructions.append(resp.instruction)
    distinct = sorted(set(instructions))
    return RotationFinding(
        task_id=task_id,
        n_calls=n_calls,
        distinct_instructions=len(distinct),
        sample_instructions=distinct[:5],
    )


def _spike_url_lifetime(client: Any, task_id: str) -> UrlLifetimeFinding:
    """Sub-spike 2 — harness_url lifetime after EndTrial."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn_scraper.clients import build_pcm_client
    from bitgn.vm.pcm_pb2 import ContextRequest
    from connectrpc.errors import ConnectError

    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    client.end_trial(EndTrialRequest(trial_id=started.trial_id))

    offsets = [0, 5, 30, 300, 1800]
    reachable: list[bool] = []
    pcm = build_pcm_client(started.harness_url)
    t0 = time.time()
    for off in offsets:
        target = t0 + off
        sleep_for = max(0.0, target - time.time())
        if sleep_for > 0:
            time.sleep(sleep_for)
        try:
            pcm.context(ContextRequest())
            reachable.append(True)
        except ConnectError:
            reachable.append(False)
    return UrlLifetimeFinding(
        trial_id=started.trial_id,
        harness_url=started.harness_url,
        probe_offsets_seconds=offsets,
        reachable_at_offset=reachable,
    )


def _spike_auto_termination(client: Any, task_id: str) -> AutoTerminationFinding:
    """Sub-spike 3 — does an unended trial auto-terminate?"""
    from bitgn.harness_pb2 import StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ContextRequest
    from bitgn_scraper.clients import build_pcm_client
    from connectrpc.errors import ConnectError

    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    pcm = build_pcm_client(started.harness_url)
    offsets = [600, 1800, 7200]
    reachable: list[bool] = []
    t0 = time.time()
    for off in offsets:
        target = t0 + off
        sleep_for = max(0.0, target - time.time())
        if sleep_for > 0:
            time.sleep(sleep_for)
        try:
            pcm.context(ContextRequest())
            reachable.append(True)
        except ConnectError:
            reachable.append(False)
    inferred: int | None = None
    for i, ok in enumerate(reachable):
        if not ok:
            inferred = offsets[i - 1] if i > 0 else 0
            break
    return AutoTerminationFinding(
        trial_id=started.trial_id,
        probe_offsets_seconds=offsets,
        reachable_at_offset=reachable,
        inferred_max_lifetime_seconds=inferred,
    )


def _spike_state_isolation(client: Any, task_id: str) -> StateIsolationFinding:
    """Sub-spike 4 — does a Write in trial N persist to trial N+1?"""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ReadRequest, WriteRequest
    from bitgn_scraper.clients import build_pcm_client
    from connectrpc.errors import ConnectError

    probe_path = "/_scraper_probe.txt"
    probe_content = "scraper-probe"

    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    pcm1 = build_pcm_client(started.harness_url)
    pcm1.write(WriteRequest(path=probe_path, content=probe_content))
    client.end_trial(EndTrialRequest(trial_id=started.trial_id))

    started2 = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    pcm2 = build_pcm_client(started2.harness_url)
    saw = False
    try:
        resp = pcm2.read(ReadRequest(path=probe_path))
        saw = (resp.content == probe_content)
    except ConnectError:
        saw = False
    client.end_trial(EndTrialRequest(trial_id=started2.trial_id))
    return StateIsolationFinding(
        wrote_path=probe_path,
        second_trial_saw_write=saw,
    )


def _spike_answer_replay(client: Any, task_id: str) -> AnswerReplayFinding:
    """Sub-spike 5 — does the grader use the first or last Answer?"""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome
    from bitgn_scraper.clients import build_pcm_client

    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
    )
    pcm = build_pcm_client(started.harness_url)
    pcm.answer(AnswerRequest(message="alpha", outcome=Outcome.OUTCOME_OK))
    pcm.answer(AnswerRequest(message="beta", outcome=Outcome.OUTCOME_OK))

    ended = client.end_trial(EndTrialRequest(trial_id=started.trial_id))
    detail = " ".join(ended.score_detail)
    if "alpha" in detail and "beta" not in detail:
        graded = "first"
    elif "beta" in detail and "alpha" not in detail:
        graded = "second"
    else:
        graded = "unknown"
    return AnswerReplayFinding(
        first_answer="alpha",
        second_answer="beta",
        graded_against=graded,
    )


def _spike_rate_limit(client: Any, task_id: str, n_parallel: int) -> RateLimitFinding:
    """Sub-spike 6 — n_parallel concurrent StartPlayground calls."""
    import concurrent.futures
    from bitgn.harness_pb2 import StartPlaygroundRequest

    def _one() -> tuple[bool, int | None]:
        try:
            client.start_playground(
                StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=task_id)
            )
            return (True, None)
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            return (False, int(code) if isinstance(code, int) else None)

    throttled_codes: list[int] = []
    n_throttled = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_parallel) as ex:
        futs = [ex.submit(_one) for _ in range(n_parallel)]
        for fut in concurrent.futures.as_completed(futs):
            ok, code = fut.result()
            if not ok:
                n_throttled += 1
                if code is not None:
                    throttled_codes.append(code)
    return RateLimitFinding(
        n_parallel_calls=n_parallel,
        n_throttled=n_throttled,
        throttle_status_codes=sorted(set(throttled_codes)),
    )


def _spike_size_sanity(client: Any, task_ids: list[str]) -> SizeSanityFinding:
    """Sub-spike 7 — sample workspace byte totals."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ReadRequest, TreeRequest
    from bitgn_scraper.clients import build_pcm_client

    totals: list[int] = []
    for tid in task_ids:
        started = client.start_playground(
            StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=tid)
        )
        pcm = build_pcm_client(started.harness_url)
        tree = pcm.tree(TreeRequest(root="/"))
        total = _walk_tree_byte_total(pcm, tree.root, "")
        totals.append(total)
        client.end_trial(EndTrialRequest(trial_id=started.trial_id))
    return SizeSanityFinding(
        sampled_task_ids=task_ids,
        byte_totals=totals,
        max_byte_total=max(totals) if totals else 0,
    )


def _walk_tree_byte_total(pcm: Any, entry: Any, prefix: str) -> int:
    """Recursive tree-walk; sums byte counts of all files.

    TreeResponse.Entry exposes only {name, is_dir, children} — no size field,
    so we have to Read each file. ReadResponse.content is proto TYPE_STRING,
    so we measure it via .encode("utf-8"). On RPC failure we count the file
    as 0 rather than aborting the whole scan, but we deliberately let any
    other exception (AttributeError from an SDK rename, etc.) propagate so
    the spike fails loudly instead of silently reporting zero bytes.
    """
    from bitgn.vm.pcm_pb2 import ReadRequest
    from connectrpc.errors import ConnectError

    name = entry.name or ""
    path = prefix + ("/" + name if name and name != "/" else "")
    if entry.is_dir:
        return sum(_walk_tree_byte_total(pcm, c, path) for c in entry.children)
    try:
        resp = pcm.read(ReadRequest(path=path or "/"))
    except ConnectError:
        return 0
    return len(resp.content.encode("utf-8"))


def run_phase0_cli() -> int:
    """Run all 7 sub-spikes and dump lifecycle_spike.json."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="bitgn_scraper phase0")
    parser.add_argument("--task-id", default="t001",
                        help="task to use for single-task spikes (rotation, lifetime, ...)")
    parser.add_argument("--n-rotation-calls", type=int, default=20)
    parser.add_argument("--n-rate-parallel", type=int, default=20)
    parser.add_argument("--size-sample", default="t001,t010,t020,t030,t050")
    parser.add_argument("--out-root", type=Path,
                        default=Path("artifacts/harness_db/scrape_runs"))
    args = parser.parse_args(sys.argv[2:])

    from bitgn_scraper.clients import build_harness_client
    client = build_harness_client()

    started_at = datetime.now(tz=timezone.utc)
    print(f"[phase0] starting at {started_at.isoformat()}", flush=True)

    print(f"[phase0] (1/7) rotation on {args.task_id}", flush=True)
    rotation = _spike_rotation(client, args.task_id, args.n_rotation_calls)

    print(f"[phase0] (2/7) url_lifetime on {args.task_id}", flush=True)
    url_lifetime = _spike_url_lifetime(client, args.task_id)

    print(f"[phase0] (3/7) auto_termination on {args.task_id} — long-running", flush=True)
    auto_termination = _spike_auto_termination(client, args.task_id)

    print(f"[phase0] (4/7) state_isolation on {args.task_id}", flush=True)
    state_isolation = _spike_state_isolation(client, args.task_id)

    print(f"[phase0] (5/7) answer_replay on {args.task_id}", flush=True)
    answer_replay = _spike_answer_replay(client, args.task_id)

    print(f"[phase0] (6/7) rate_limit n={args.n_rate_parallel}", flush=True)
    rate_limit = _spike_rate_limit(client, args.task_id, args.n_rate_parallel)

    print(f"[phase0] (7/7) size_sanity on {args.size_sample}", flush=True)
    size_sanity = _spike_size_sanity(client, args.size_sample.split(","))

    report = LifecycleReport(
        started_at=started_at,
        rotation=rotation,
        url_lifetime=url_lifetime,
        auto_termination=auto_termination,
        state_isolation=state_isolation,
        answer_replay=answer_replay,
        rate_limit=rate_limit,
        size_sanity=size_sanity,
    )

    out_dir = args.out_root / started_at.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "lifecycle_spike.json"
    out_path.write_text(serialize_report(report))
    print(f"[phase0] wrote {out_path}", flush=True)
    return 0
