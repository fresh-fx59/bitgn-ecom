"""bitgn-agent CLI — run-task + run-benchmark.

Fail-fast pattern P6: config validation happens before the thread pool
is created. All runtime wiring lives here; agent.py / orchestrator.py
stay pure.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

import threading as _threading

from bitgn_contest_agent import __version__
from bitgn_contest_agent.adapter.pcm import PcmAdapter
from bitgn_contest_agent.adapter.pcm_tracing import TracingPcmClient
from bitgn_contest_agent.agent import AgentLoop, AgentLoopResult
from bitgn_contest_agent.arch_constants import ArchCategory
from bitgn_contest_agent.arch_log import (
    TaskContextFilter,
    emit_arch,
    reset_task_context,
    set_task_context,
)
from bitgn_contest_agent.backend.base import Backend
from bitgn_contest_agent.backend.openai_compat import OpenAIChatBackend
from bitgn_contest_agent.backend.openai_toolcalling import OpenAIToolCallingBackend
from bitgn_contest_agent.bench.run_metrics import RunMetrics
from bitgn_contest_agent.config import AgentConfig, ConfigError, load_from_env
from bitgn_contest_agent.harness import BitgnHarness, StartedTask
from bitgn_contest_agent.orchestrator import (
    Orchestrator,
    TaskExecutionResult,
    TaskSpec,
)
from bitgn_contest_agent import resume as _resume_mod
from bitgn_contest_agent.reactive_router import ReactiveRouter, load_reactive_router
from bitgn_contest_agent.router import Router, load_router
from bitgn_contest_agent.trace_schema import TRACE_SCHEMA_VERSION, TraceMeta
from bitgn_contest_agent.trace_writer import TraceWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bitgn-agent", description="BitGN PAC1 contest agent")
    parser.add_argument("--version", action="version", version=f"bitgn-agent {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    run_task = subs.add_parser("run-task", help="run a single benchmark task")
    run_task.add_argument("--task-id", required=True)
    run_task.add_argument("--benchmark", default=None)
    run_task.add_argument("--log-dir", default=None)

    run_bench = subs.add_parser("run-benchmark", help="run every task in a benchmark")
    run_bench.add_argument("--benchmark", default=None)
    run_bench.add_argument("--runs", type=int, default=1, help="repeat each task N times")
    run_bench.add_argument("--max-parallel", type=int, default=None)
    run_bench.add_argument("--output", default=None, help="bench_summary.json path")
    run_bench.add_argument("--log-dir", default=None)
    run_bench.add_argument("--smoke", action="store_true",
                           help="run the fixed smoke subset with hardcoded parallelism (180s budget)")
    run_bench.add_argument("--max-inflight-llm", type=int, default=None,
                           help="max concurrent LLM calls across all parallel tasks")
    run_bench.add_argument("--max-trials", type=int, default=None,
                           help="cap the number of trials executed per leaderboard run "
                                "(runs first N of the server's trial list; the rest are "
                                "left unstarted so they consume no VM). Dashboard-visible "
                                "cheap smoke.")
    run_bench.add_argument("--parallel-iterations", dest="parallel_iterations",
                           action="store_true", default=None,
                           help="run --runs iterations concurrently (default on for runs>1 non-smoke)")
    run_bench.add_argument("--no-parallel-iterations", dest="parallel_iterations",
                           action="store_false",
                           help="force serial iteration execution (old behavior)")
    run_bench.add_argument("--resume", nargs="?", default=None, const="__LAST__",
                           metavar="RUN_ID",
                           help="resume a crashed leaderboard run by its BitGN run_id; "
                                "skips trials already DONE/ERROR and submits with force=True. "
                                "Implies --runs 1. If passed with no value, reads run_id "
                                "from .last_run_id (written on start_run).")

    tri = subs.add_parser("triage", help="classify bench failures")
    tri.add_argument("summary", nargs="?", default=None,
                     help="path to a v1.1 bench_summary JSON (single-mode)")
    tri.add_argument("--before", help="baseline summary for diff mode")
    tri.add_argument("--after", help="candidate summary for diff mode")

    return parser


_ADAPTER_DRIVEN_ENV_VARS = (
    "MAX_PARALLEL_TASKS",
    "MAX_INFLIGHT_LLM",
    "TASK_TIMEOUT_SEC",
    "LLM_HTTP_TIMEOUT_SEC",
    "AGENT_REASONING_EFFORT",
)


def _apply_adapter_profile(cfg: AgentConfig) -> AgentConfig:
    """Override ``cfg`` fields with adapter profile defaults where the user
    did not explicitly set the corresponding env var.

    Precedence: env var (highest) > adapter profile > AgentConfig hard default.
    Only runs when ``AGENT_TOOLCALLING=1`` (frontier path never consults the
    registry). Unknown model raises ``ConfigError`` at CLI startup — fail-fast.
    Logs one line per resolved tunable.
    """
    if os.environ.get("AGENT_TOOLCALLING", "").strip() not in {"1", "true", "True"}:
        return cfg
    from bitgn_contest_agent.backend.adapters import get_adapter

    adapter = get_adapter(cfg.model)
    profile = adapter.profile
    overrides: dict = {}
    sources: List[tuple[str, Any, str]] = []

    def _pick(env_name: str, field: str, profile_value: Any) -> None:
        if os.environ.get(env_name):
            sources.append((field, getattr(cfg, field), "env"))
            return
        overrides[field] = profile_value
        sources.append((field, profile_value, "adapter"))

    _pick("MAX_PARALLEL_TASKS", "max_parallel_tasks", profile.max_parallel_tasks)
    _pick("MAX_INFLIGHT_LLM", "max_inflight_llm", profile.max_inflight_llm)
    _pick("TASK_TIMEOUT_SEC", "task_timeout_sec", profile.task_timeout_sec)
    _pick("LLM_HTTP_TIMEOUT_SEC", "llm_http_timeout_sec", profile.llm_http_timeout_sec)
    _pick("AGENT_REASONING_EFFORT", "reasoning_effort", profile.reasoning_effort)

    # classifier_timeout_sec is read directly by classifier.py via env
    # (hardcoded default=10s). That default is too low for local 20B
    # models AND too high for the neuraldeep gateway's 60s server cap.
    # Seed the env from the adapter profile when the user did not set
    # it explicitly — same precedence as the cfg fields above.
    classifier_source = "env"
    if not os.environ.get("BITGN_CLASSIFIER_TIMEOUT_SEC"):
        os.environ["BITGN_CLASSIFIER_TIMEOUT_SEC"] = str(profile.classifier_timeout_sec)
        classifier_source = "adapter"
    sources.append(
        (
            "classifier_timeout_sec",
            int(os.environ["BITGN_CLASSIFIER_TIMEOUT_SEC"]),
            classifier_source,
        )
    )

    logger = logging.getLogger(__name__)
    logger.info(
        "[ARCH:CONFIG] resolved adapter=%s model=%s",
        type(adapter).__name__,
        cfg.model,
    )
    for field, value, source in sources:
        logger.info("  %s=%s (source=%s)", field, value, source)

    return dataclasses.replace(cfg, **overrides) if overrides else cfg


def _resolve_config(args: argparse.Namespace) -> AgentConfig:
    # PLAN DEVIATION: plan uses cfg.__dict__ but AgentConfig is
    # frozen=True, slots=True — slotted dataclasses have no __dict__.
    # dataclasses.replace() is the idiomatic way to override fields.
    cfg = load_from_env()
    cfg = _apply_adapter_profile(cfg)
    overrides: dict = {}
    if getattr(args, "benchmark", None):
        overrides["benchmark"] = args.benchmark
    if getattr(args, "log_dir", None):
        overrides["log_dir"] = args.log_dir
    if getattr(args, "max_parallel", None) is not None:
        overrides["max_parallel_tasks"] = args.max_parallel
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


def _make_harness(cfg: AgentConfig) -> BitgnHarness:
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"
    return BitgnHarness.from_env(
        benchmark=cfg.benchmark,
        bitgn_base_url=base_url,
        bitgn_api_key=cfg.bitgn_api_key,
    )


def _make_backend(cfg: AgentConfig) -> Backend:
    # AGENT_TOOLCALLING=1 routes to the native OpenAI tool-calling backend,
    # required to drive small local models (LM Studio etc.) that can't emit
    # the NextStep envelope as free-text JSON. Default (off) keeps the
    # frontier cliproxyapi path bit-identical.
    if os.environ.get("AGENT_TOOLCALLING", "").strip() in {"1", "true", "True"}:
        return OpenAIToolCallingBackend.from_config(
            base_url=cfg.cliproxy_base_url,
            api_key=cfg.cliproxy_api_key,
            model=cfg.model,
            reasoning_effort=cfg.reasoning_effort,
        )
    return OpenAIChatBackend.from_config(
        base_url=cfg.cliproxy_base_url,
        api_key=cfg.cliproxy_api_key,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
    )


def _trace_path(cfg: AgentConfig, run_id: str, task_id: str, run_index: int) -> Path:
    return Path(cfg.log_dir) / run_id / f"{task_id}__run{run_index}.jsonl"


def _git_commit_short() -> str:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


_ROUTER_SINGLETON: Router | None = None


def _get_router() -> Router:
    """Lazy-load and cache a Router for the current process.

    Skills directory defaults to src/bitgn_contest_agent/skills. Empty at
    M0, populated in M1+. Any skill-load failure is a hard error — we
    want the CLI to refuse to start rather than silently degrade to an
    empty router.
    """
    global _ROUTER_SINGLETON
    if _ROUTER_SINGLETON is None:
        skills_dir = Path(__file__).parent / "skills"
        _ROUTER_SINGLETON = load_router(skills_dir=skills_dir)
    return _ROUTER_SINGLETON


_REACTIVE_ROUTER_SINGLETON: ReactiveRouter | None = None


def _get_reactive_router() -> ReactiveRouter:
    """Load reactive skills from skills/reactive/ on first call.

    Reactive skills use path-based triggers for mid-task injection.
    An empty reactive router (no skills in the directory) is valid at
    any milestone — it evaluates to no-op.
    """
    global _REACTIVE_ROUTER_SINGLETON
    if _REACTIVE_ROUTER_SINGLETON is None:
        reactive_dir = Path(__file__).parent / "skills" / "reactive"
        _REACTIVE_ROUTER_SINGLETON = load_reactive_router(reactive_dir)
    return _REACTIVE_ROUTER_SINGLETON


def _run_single_task(
    *,
    cfg: AgentConfig,
    harness: BitgnHarness,
    backend: Backend,
    task: TaskSpec,
    run_id: str,
    run_index: int,
    cancel_event: threading.Event,
    inflight_semaphore: threading.Semaphore | None = None,
    metrics: RunMetrics | None = None,
    router: Router | None = None,
    reactive_router: ReactiveRouter | None = None,
) -> TaskExecutionResult:
    started: StartedTask | None = None
    writer: TraceWriter | None = None
    task_handler: logging.Handler | None = None
    ctx_token = None
    # In leaderboard flow the real task_id is only known after
    # start_trial; fall back to whatever the orchestrator gave us for
    # crash reporting before the trial is provisioned.
    effective_task_id = task.task_id
    try:
        if task.trial_id is not None:
            started = harness.start_trial(task.trial_id)
        else:
            started = harness.start_task(task.task_id)
        effective_task_id = started.task_id

        # Wrap the runtime in a tracing proxy BEFORE constructing the
        # adapter. Every PCM call (including those made by preflight_*
        # tools that receive the runtime directly) then emits a pcm_op
        # trace record — the same ops counted as "steps" on the BitGN
        # dashboard. Writer is attached immediately after we know the
        # final task_id.
        tracing_runtime = TracingPcmClient(started.runtime_client)
        adapter = PcmAdapter(
            runtime=tracing_runtime,
            max_tool_result_bytes=cfg.max_tool_result_bytes,
        )

        trace_path = _trace_path(cfg, run_id, effective_task_id, run_index)
        writer = TraceWriter(path=trace_path)
        tracing_runtime.set_writer(writer)

        # Per-task stderr log file + task-scoped ContextVar.
        task_log_path = trace_path.with_suffix(".log")
        task_log_path.parent.mkdir(parents=True, exist_ok=True)
        task_handler = logging.FileHandler(
            task_log_path, encoding="utf-8", delay=True,
        )
        task_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s "
            "task=%(task_id)s run=%(run_index)s "
            "skill=%(skill)s category=%(category)s "
            "trace=%(trace_name)s "
            "%(name)s %(message)s"
        ))
        _worker_tid = _threading.get_ident()
        task_handler.addFilter(lambda r: r.thread == _worker_tid)
        task_handler.addFilter(TaskContextFilter())
        logging.getLogger().addHandler(task_handler)

        ctx_token = set_task_context(
            task_id=effective_task_id,
            run_index=run_index,
            trace_name=trace_path.name,
            writer=writer,
        )

        intent_head: str | None = None
        if started is not None and started.instruction:
            intent_head = started.instruction[:240]

        writer.write_meta(
            TraceMeta(
                agent_version=__version__,
                agent_commit=_git_commit_short(),
                model=cfg.model,
                backend="openai_compat",
                reasoning_effort=cfg.reasoning_effort,
                benchmark=cfg.benchmark,
                task_id=effective_task_id,
                task_index=task.task_index,
                started_at=datetime.now(timezone.utc).isoformat(),
                trace_schema_version=TRACE_SCHEMA_VERSION,
                harness_url=started.harness_url,
                intent_head=intent_head,
            )
        )
        emit_arch(
            category=ArchCategory.TASK_START,
            details=intent_head or "",
        )

        loop = AgentLoop(
            backend=backend,
            adapter=adapter,
            writer=writer,
            max_steps=cfg.max_steps,
            llm_http_timeout_sec=float(cfg.llm_http_timeout_sec),
            cancel_event=cancel_event,
            backend_backoff_ms=cfg.rate_limit_backoff_ms,
            inflight_semaphore=inflight_semaphore,
            metrics=metrics,
            router=router,
            reactive_router=reactive_router,
        )
        result: AgentLoopResult = loop.run(
            task_id=effective_task_id,
            task_text=started.instruction,
        )
        writer.close()

        score, detail = harness.end_task(started)
        # Back-fill the grader score (and detail) into the trace so
        # bench_summary sees the authoritative verdict instead of the
        # agent's self-reported OUTCOME_OK, and so content-layer
        # failures can be root-caused offline from the trace.
        # Best-effort — a failure here must not lose the task result.
        try:
            writer.patch_outcome_score(
                float(score),
                score_detail=[str(s) for s in detail] if detail else None,
            )
        except Exception:
            pass
        return TaskExecutionResult(
            task_id=effective_task_id,
            score=float(score),
            terminated_by=result.terminated_by,
            error_kind=result.error_kind,
            error_msg=result.error_msg,
        )
    except Exception as exc:
        import traceback as tb

        msg = f"{type(exc).__name__}: {exc}"
        logging.getLogger(__name__).exception(
            "task %s crashed: %s", effective_task_id, msg,
        )
        # Write crash outcome to trace so bench_summary counts it as a
        # failure instead of silently dropping the task.
        if writer is not None:
            try:
                from bitgn_contest_agent.trace_schema import TraceOutcome

                writer.append_outcome(
                    TraceOutcome(
                        terminated_by="error",
                        reported=None,
                        enforcer_bypassed=False,
                        error_kind="INTERNAL_CRASH",
                        error_msg=msg,
                        total_steps=0,
                        total_llm_calls=0,
                        total_prompt_tokens=0,
                        total_completion_tokens=0,
                        total_cached_tokens=0,
                        total_reasoning_tokens=0,
                    )
                )
                writer.close()
                writer.write_crash_sidecar(
                    msg, traceback_text=tb.format_exc()
                )
            except Exception:
                pass
        if started is not None:
            try:
                harness.end_task(started)
            except Exception:
                pass
        return TaskExecutionResult(
            task_id=effective_task_id,
            score=0.0,
            terminated_by="error",
            error_kind="INTERNAL_CRASH",
            error_msg=msg,
        )
    finally:
        if ctx_token is not None:
            reset_task_context(ctx_token)
        if task_handler is not None:
            logging.getLogger().removeHandler(task_handler)
            task_handler.close()


def _cmd_run_task(args: argparse.Namespace) -> int:
    cfg = _resolve_config(args)
    harness = _make_harness(cfg)
    backend = _make_backend(cfg)
    all_ids = harness.list_task_ids()
    try:
        idx = all_ids.index(args.task_id)
    except ValueError:
        print(f"error: task {args.task_id} not found in {cfg.benchmark}", file=sys.stderr)
        return 2

    # Use the harness instruction as the task text (§harness wrapper).
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    task = TaskSpec(task_id=args.task_id, task_index=idx, task_text="")
    result = _run_single_task(
        cfg=cfg,
        harness=harness,
        backend=backend,
        task=task,
        run_id=run_id,
        run_index=0,
        cancel_event=threading.Event(),
        router=_get_router(),
        reactive_router=_get_reactive_router(),
    )
    print(json.dumps(dataclasses.asdict(result), indent=2))
    return 0 if result.terminated_by == "report_completion" else 1


def _run_tasks_and_summarize(
    cfg: AgentConfig,
    *,
    harness: BitgnHarness,
    backend: Backend,
    run_id: str,
    runs: int,
    output: str | None,
    tasks_for_iteration: Callable[[int], List[TaskSpec]],
    finalize_iteration: Callable[[int, List[TaskExecutionResult]], None] = lambda _i, _r: None,
    inflight_semaphore: threading.Semaphore | None = None,
    metrics: RunMetrics | None = None,
    parallel_iterations: bool = False,
) -> list[TaskExecutionResult]:
    """Execute `runs` iterations and optionally write a bench_summary JSON.

    `tasks_for_iteration(i)` returns the TaskSpec list for iteration i.
    For the leaderboard flow each iteration calls `start_run` here and
    the resulting TaskSpecs carry trial_ids. `finalize_iteration(i, results)`
    runs after each iteration — leaderboard flow uses it to call
    `submit_run(run_id)`. Smoke/playground flows pass a no-op.

    When `parallel_iterations=True`, the iterations run concurrently via a
    ThreadPoolExecutor with `max_workers=runs`. The shared
    `inflight_semaphore` caps total LLM concurrency across every agent in
    every iteration, so rate-limit posture is governed by that cap — not
    by iteration count. Iteration result order is preserved in the
    returned list.
    """

    def run_iteration(run_index: int) -> list[TaskExecutionResult]:
        iter_tasks = tasks_for_iteration(run_index)

        shared_router = _get_router()
        shared_reactive_router = _get_reactive_router()

        def runner(task: TaskSpec, cancel_event: threading.Event, _ri=run_index):
            return _run_single_task(
                cfg=cfg,
                harness=harness,
                backend=backend,
                task=task,
                run_id=run_id,
                run_index=_ri,
                cancel_event=cancel_event,
                inflight_semaphore=inflight_semaphore,
                metrics=metrics,
                router=shared_router,
                reactive_router=shared_reactive_router,
            )

        orch = Orchestrator(
            runner=runner,
            max_parallel_tasks=cfg.max_parallel_tasks,
            task_timeout_sec=cfg.task_timeout_sec,
            task_timeout_grace_sec=cfg.task_timeout_grace_sec,
        )
        iter_results = orch.run(iter_tasks)
        finalize_iteration(run_index, iter_results)
        return iter_results

    all_results: list[TaskExecutionResult] = []
    if parallel_iterations and runs > 1:
        # concurrent.futures import is local — keeps import cost off the
        # serial / --runs 1 / smoke paths.
        from concurrent.futures import ThreadPoolExecutor

        logging.getLogger(__name__).info(
            "running %d iterations in parallel (inflight_semaphore caps LLM concurrency)",
            runs,
        )
        with ThreadPoolExecutor(max_workers=runs) as pool:
            # Submit in order; gather results in order to preserve the
            # (run_index, task_index) identity downstream.
            futures = [pool.submit(run_iteration, i) for i in range(runs)]
            for fut in futures:
                all_results.extend(fut.result())
    else:
        for run_index in range(runs):
            all_results.extend(run_iteration(run_index))

    if output:
        # scripts/ is a sibling of src/, not part of the installed package
        # (pyproject.toml only packages src/). Pytest finds it via its
        # implicit rootdir sys.path injection; at runtime we need to do
        # the same here so the CLI works from both the editable install
        # and a built wheel invoked from the repo checkout.
        _repo_root = Path(__file__).resolve().parents[2]
        if str(_repo_root) not in sys.path:
            sys.path.insert(0, str(_repo_root))
        from scripts.bench_summary import summarize  # type: ignore[attr-defined]

        summary = summarize(logs_dir=Path(cfg.log_dir) / run_id)
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"bench summary → {output}")

        if metrics is not None:
            metrics_path = Path(output).with_name(
                Path(output).stem + ".run_metrics.json"
            )
            metrics_path.write_text(
                json.dumps(metrics.snapshot(), indent=2),
                encoding="utf-8",
            )
            print(f"run metrics → {metrics_path}")

    return all_results


def _cmd_run_benchmark(args: argparse.Namespace) -> int:
    cfg = _resolve_config(args)

    # --smoke overrides task list and parallelism BEFORE harness/backend creation.
    # dataclasses.replace hits max_parallel_tasks a SECOND time (after _resolve_config
    # already set it from --max-parallel) — intentional: smoke beats user args.
    smoke_task_ids: List[str] | None = None
    if args.smoke:
        from bitgn_contest_agent.bench.smoke import (
            SMOKE_TASKS,
            SMOKE_MAX_PARALLEL,
            SMOKE_MAX_INFLIGHT_LLM,
        )
        cfg = dataclasses.replace(
            cfg,
            max_parallel_tasks=SMOKE_MAX_PARALLEL,
            max_inflight_llm=SMOKE_MAX_INFLIGHT_LLM,
        )
        smoke_task_ids = list(SMOKE_TASKS)
    else:
        if args.max_inflight_llm is not None:
            cfg = dataclasses.replace(cfg, max_inflight_llm=args.max_inflight_llm)

    harness = _make_harness(cfg)
    backend = _make_backend(cfg)

    # One semaphore shared across all parallel agents in this run
    inflight_semaphore = threading.Semaphore(cfg.max_inflight_llm)
    metrics = RunMetrics(max_inflight_llm=cfg.max_inflight_llm)

    # Share the semaphore with the classifier so router/validator LLM
    # calls respect the same concurrency cap as the main agent calls.
    from bitgn_contest_agent import classifier as _cls_mod
    _cls_mod.set_inflight_semaphore(inflight_semaphore)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Per-iteration run_id map for leaderboard flow — populated by
    # tasks_for_iteration, consumed by finalize_iteration. Lock protects
    # it when parallel iterations populate concurrently.
    leaderboard_run_ids: dict[int, str] = {}
    leaderboard_run_ids_lock = threading.Lock()

    # --resume pins a single iteration against an existing BitGN run_id.
    # Forces runs=1 because resume semantics only make sense for one run
    # (the run_id is already fixed server-side).
    resume_run_id: Optional[str] = args.resume
    if resume_run_id == "__LAST__":
        last_path = Path(".last_run_id")
        if not last_path.exists():
            print("--resume with no value requires .last_run_id (run a benchmark first)",
                  file=sys.stderr)
            return 2
        resume_run_id = last_path.read_text().strip()
        if not resume_run_id:
            print("--resume: .last_run_id is empty", file=sys.stderr)
            return 2
        logging.getLogger(__name__).info(
            "--resume (default): using .last_run_id=%s", resume_run_id,
        )
    if resume_run_id is not None:
        if args.smoke:
            print("--resume is incompatible with --smoke", file=sys.stderr)
            return 2
        if args.runs != 1:
            logging.getLogger(__name__).info(
                "--resume forces runs=1 (was %d)", args.runs,
            )
            args.runs = 1

    if args.smoke:
        # Smoke uses the playground flow: a small fixed subset of tasks
        # exercised against StartPlayground. Kept invisible to the
        # leaderboard intentionally (cheap local-only smoke).
        assert smoke_task_ids is not None

        def tasks_for_iteration(_i: int) -> List[TaskSpec]:
            return [
                TaskSpec(task_id=tid, task_index=i, task_text="")
                for i, tid in enumerate(smoke_task_ids)
            ]

        finalize_iteration: Callable[[int, List[TaskExecutionResult]], None] = (
            lambda _i, _r: None
        )
    else:
        # Full benchmark uses the leaderboard flow: each --runs iteration
        # calls StartRun, executes every trial the server hands back,
        # then SubmitRun's the leaderboard entry so it appears in the
        # dashboard. The run name is the canonical owner/agent handle
        # fixed by the user (2026-04-11) — the server disambiguates
        # iterations by run_id, not by name.
        _model_slug = cfg.model.rsplit("/", 1)[-1].replace(":", "-")
        LEADERBOARD_RUN_NAME = (
            f"aleksei_aksenov-ai_engineer_helper-bitgn-agent-{_model_slug}"
        )

        def tasks_for_iteration(run_index: int) -> List[TaskSpec]:
            if resume_run_id is not None:
                plan = _resume_mod.plan_resume(harness, resume_run_id)
                with leaderboard_run_ids_lock:
                    leaderboard_run_ids[run_index] = plan.run_id
                logging.getLogger(__name__).info(
                    "resume run_id=%s pending=%d stuck=%d done=%d error=%d",
                    plan.run_id, len(plan.pending), len(plan.stuck),
                    plan.done_count, plan.error_count,
                )
                # Best-effort grade stuck (RUNNING) trials inline. On error
                # the server keeps them in ERROR — force-submit handles it.
                for st in plan.stuck:
                    try:
                        harness.end_task_by_id(st.trial_id)
                    except Exception as exc:
                        logging.getLogger(__name__).warning(
                            "resume: end_task_by_id(%s) failed: %s",
                            st.trial_id, exc,
                        )
                return [
                    TaskSpec(
                        task_id=t.task_id,
                        task_index=i,
                        task_text="",
                        trial_id=t.trial_id,
                    )
                    for i, t in enumerate(plan.pending)
                ]

            rid, trial_ids = harness.start_run(name=LEADERBOARD_RUN_NAME)
            logging.getLogger(__name__).info(
                "started leaderboard run_id=%s trials=%d", rid, len(trial_ids),
            )
            try:
                Path(".last_run_id").write_text(rid)
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "failed to persist .last_run_id: %s", exc,
                )
            with leaderboard_run_ids_lock:
                leaderboard_run_ids[run_index] = rid
            # --max-trials truncates the trial list so the capped remainder
            # is never start_trial'd (no VM, no wall-clock). submit_run(force)
            # still finalizes the leaderboard entry with the partial result.
            cap = getattr(args, "max_trials", None)
            effective_trials = trial_ids if cap is None else trial_ids[: max(0, int(cap))]
            if cap is not None:
                logging.getLogger(__name__).info(
                    "max_trials=%d: running %d of %d trials (leaderboard run %s)",
                    cap, len(effective_trials), len(trial_ids), rid,
                )
            return [
                TaskSpec(
                    task_id=tid,
                    task_index=i,
                    task_text="",
                    trial_id=tid,
                )
                for i, tid in enumerate(effective_trials)
            ]

        def finalize_iteration(run_index: int, _results: List[TaskExecutionResult]) -> None:
            with leaderboard_run_ids_lock:
                rid = leaderboard_run_ids.get(run_index)
            if rid is None:
                return
            # --max-trials leaves some trials unstarted and resume re-submits
            # a partial run; both require force=True so the server finalizes
            # the incomplete leaderboard entry. Full clean runs keep
            # force=False (default) so a real incomplete run fails loudly.
            force_submit = (
                getattr(args, "max_trials", None) is not None
                or resume_run_id is not None
            )
            try:
                state = harness.submit_run(rid, force=force_submit)
                logging.getLogger(__name__).info(
                    "submitted run run_id=%s force=%s state=%s",
                    rid, force_submit, state,
                )
            except Exception as exc:
                # SubmitRun failure must not lose the results we already
                # collected. Log and continue — the results artifact
                # stays on disk for offline analysis.
                logging.getLogger(__name__).warning(
                    "submit_run failed for run_id=%s: %s", rid, exc,
                )

    # Default: parallel iterations ON for non-smoke multi-run benches.
    # Smoke stays serial (small, deliberate, different flow). --runs 1
    # also stays serial (nothing to parallelize).
    if args.parallel_iterations is None:
        parallel_iterations = (args.runs > 1) and (not args.smoke)
    else:
        parallel_iterations = bool(args.parallel_iterations)

    all_results = _run_tasks_and_summarize(
        cfg,
        harness=harness,
        backend=backend,
        run_id=run_id,
        runs=args.runs,
        output=args.output,
        tasks_for_iteration=tasks_for_iteration,
        finalize_iteration=finalize_iteration,
        inflight_semaphore=inflight_semaphore,
        metrics=metrics,
        parallel_iterations=parallel_iterations,
    )

    total = len(all_results)
    passed = sum(1 for r in all_results if r.score >= 1.0)
    print(f"pass rate: {passed}/{total} ({passed / max(1, total) * 100:.1f}%)")
    return 0 if passed == total else 1


def _cmd_triage(args: argparse.Namespace) -> int:
    # sys.path injection for scripts.bench_summary (same pattern as run-benchmark)
    _repo_root = Path(__file__).resolve().parents[2]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from scripts.bench_summary import load_summary  # type: ignore[attr-defined]
    from bitgn_contest_agent.bench.triage import classify_failure, TRIAGE_ORDER

    def cluster(path: Path) -> dict[str, list[str]]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        summary = load_summary(raw)
        buckets: dict[str, list[str]] = {c: [] for c in TRIAGE_ORDER}
        for tid, t in summary.get("tasks", {}).items():
            if t.get("passes", 0) >= t.get("runs", 1):
                continue  # all runs passed — not a failure
            evidence = {
                "task_id": tid,
                "outcome": t.get("last_outcome", "OUTCOME_OK"),
                "grader_failed": True,
                "step_texts": t.get("step_texts", []),
                "latency_ms": t.get("last_latency_ms", 0),
                "timed_out": t.get("timed_out", False),
                "task_category": t.get("category", "other"),
            }
            buckets[classify_failure(evidence)].append(tid)
        return buckets

    if args.before and args.after:
        b = cluster(Path(args.before))
        a = cluster(Path(args.after))
        for c in TRIAGE_ORDER:
            cleared = sorted(set(b[c]) - set(a[c]))
            added = sorted(set(a[c]) - set(b[c]))
            if cleared or added:
                parts = [f"-{t}" for t in cleared] + [f"+{t}" for t in added]
                print(f"{c}: {' '.join(parts)}")
    else:
        if not args.summary:
            print("error: triage requires a summary path or --before/--after", file=sys.stderr)
            return 2
        b = cluster(Path(args.summary))
        for c in TRIAGE_ORDER:
            if b[c]:
                print(f"{c}: {' '.join(sorted(b[c]))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "task=%(task_id)s run=%(run_index)s "
            "skill=%(skill)s category=%(category)s "
            "trace=%(trace_name)s "
            "%(name)s %(message)s"
        ),
    )
    ctx_filter = TaskContextFilter()
    root = logging.getLogger()
    root.addFilter(ctx_filter)
    for h in root.handlers:
        h.addFilter(ctx_filter)
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "run-task":
            return _cmd_run_task(args)
        if args.command == "run-benchmark":
            return _cmd_run_benchmark(args)
        if args.command == "triage":
            return _cmd_triage(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
