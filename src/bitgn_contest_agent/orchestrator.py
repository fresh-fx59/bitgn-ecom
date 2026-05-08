"""Task-level parallelism + cooperative cancel.

Uses ThreadPoolExecutor because the backend interface is synchronous and
the throughput bottleneck is cliproxyapi, not local CPU.

§3.1, §3.2, §4.2 invariant 1 (worker boundary uses except Exception).
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    task_index: int
    task_text: str
    # Leaderboard flow: when set, the runner resolves the task via
    # `harness.start_trial(trial_id)` instead of `start_playground`.
    # In this mode `task_id` starts as a placeholder (the trial_id or
    # an ordinal label) and is replaced with the real task_id from the
    # server's StartTrial response inside the runner.
    trial_id: str | None = None


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    task_id: str
    score: float
    terminated_by: str
    error_kind: Optional[str]
    error_msg: Optional[str]


TaskRunner = Callable[[TaskSpec, threading.Event], TaskExecutionResult]


class Orchestrator:
    def __init__(
        self,
        *,
        runner: TaskRunner,
        max_parallel_tasks: int,
        task_timeout_sec: int,
        task_timeout_grace_sec: int = 20,
    ) -> None:
        self._runner = runner
        self._max_parallel_tasks = max_parallel_tasks
        self._task_timeout_sec = task_timeout_sec
        self._grace_sec = task_timeout_grace_sec

    def run(self, tasks: Sequence[TaskSpec]) -> List[TaskExecutionResult]:
        results: List[TaskExecutionResult] = [None] * len(tasks)  # type: ignore[list-item]
        cancel_events: dict[int, threading.Event] = {i: threading.Event() for i in range(len(tasks))}
        # start_times[i] is None until the worker actually picks up task i.
        # T24 observation: capturing start_times at pool.submit() time
        # burns the deadline of every queued task before it has a chance
        # to run. With 43 tasks and max_parallel=4, tasks 21..42 were
        # pre-cancelled on the first bench run (see artifacts/bench/
        # 9f3ff56_*.json — t21..t43 all show 0 steps, terminated_by=cancel).
        # Deadlines must be measured from the moment the worker begins
        # executing the runner, not from submission time.
        start_times: dict[int, Optional[float]] = {i: None for i in range(len(tasks))}
        deadlines: dict[int, Optional[float]] = {i: None for i in range(len(tasks))}
        start_lock = threading.Lock()

        def _launch(task_idx: int) -> TaskExecutionResult:
            with start_lock:
                start_times[task_idx] = time.monotonic()
            return self._wrap_runner(tasks[task_idx], cancel_events[task_idx])

        with cf.ThreadPoolExecutor(max_workers=self._max_parallel_tasks) as pool:
            futures = {
                pool.submit(_launch, i): i for i in range(len(tasks))
            }

            pending = set(futures.keys())
            while pending:
                done, pending = cf.wait(
                    pending, timeout=0.25, return_when=cf.FIRST_COMPLETED
                )
                # Fire deadlines for tasks that have actually started.
                now = time.monotonic()
                for fut, idx in list(futures.items()):
                    if fut.done():
                        continue
                    with start_lock:
                        started = start_times[idx]
                    if started is None:
                        continue  # still queued; deadline not yet active
                    if deadlines[idx] is None and self._task_timeout_sec > 0:
                        deadlines[idx] = started + self._task_timeout_sec
                    dl = deadlines[idx]
                    if dl is not None and now >= dl:
                        cancel_events[idx].set()
                        # Extend the effective deadline by grace so the
                        # worker has room to flush its trace before the
                        # next loop iteration re-checks.
                        deadlines[idx] = dl + self._grace_sec
                for fut in done:
                    idx = futures[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as exc:
                        results[idx] = TaskExecutionResult(
                            task_id=tasks[idx].task_id,
                            score=0.0,
                            terminated_by="error",
                            error_kind="INTERNAL_CRASH",
                            error_msg=f"{type(exc).__name__}: {exc}",
                        )
        return [r for r in results if r is not None]

    def _wrap_runner(self, task: TaskSpec, cancel_event: threading.Event) -> TaskExecutionResult:
        try:
            return self._runner(task, cancel_event)
        except Exception as exc:
            _LOG.exception("worker crashed on task %s", task.task_id)
            return TaskExecutionResult(
                task_id=task.task_id,
                score=0.0,
                terminated_by="error",
                error_kind="INTERNAL_CRASH",
                error_msg=f"{type(exc).__name__}: {exc}",
            )
