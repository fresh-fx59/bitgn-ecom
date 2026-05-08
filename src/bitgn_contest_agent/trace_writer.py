"""Incremental JSONL writer. Thread-safe per instance.

Each worker creates one TraceWriter, writes records as the run
progresses, and calls close() at the end. On unhandled exception the
worker calls write_crash_sidecar() before re-raising.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from bitgn_contest_agent.trace_schema import (
    StepLLMStats,
    StepSessionAfter,
    StepToolResult,
    TraceArch,
    TraceEvent,
    TraceMeta,
    TraceOutcome,
    TracePcmOp,
    TracePrepass,
    TraceStep,
    TraceTask,
    TraceVerify,
)


class TraceWriter:
    def __init__(self, *, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self._path.open("a", encoding="utf-8", buffering=1)

    @property
    def path(self) -> Path:
        return self._path

    # -- individual record writers ---------------------------------------

    def write_meta(self, meta: TraceMeta) -> None:
        self._write(meta.model_dump(mode="json"))

    def append_task(self, *, task_id: str, task_text: str) -> None:
        rec = TraceTask(task_id=task_id, task_text=task_text)
        self._write(rec.model_dump(mode="json"))

    def append_prepass(
        self,
        *,
        cmd: str,
        ok: bool,
        bytes: int = 0,
        wall_ms: int = 0,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        category: Optional[str] = None,
        query: Optional[str] = None,
        skipped_reason: Optional[str] = None,
        schema_roots: Optional[dict[str, Any]] = None,
        match_found: Optional[bool] = None,
        match_file: Optional[str] = None,
    ) -> None:
        rec = TracePrepass(
            cmd=cmd,
            ok=ok,
            bytes=bytes,
            wall_ms=wall_ms,
            error=error,
            error_code=error_code,
            category=category,
            query=query,
            skipped_reason=skipped_reason,
            schema_roots=schema_roots,
            match_found=match_found,
            match_file=match_file,
        )
        self._write(rec.model_dump(mode="json"))

    def append_step(
        self,
        *,
        step: int,
        wall_ms: int,
        llm: StepLLMStats,
        next_step: dict[str, Any],
        tool_result: StepToolResult,
        session_after: StepSessionAfter,
        enforcer_verdict: list[str] | None = None,
        enforcer_action: str | None = None,
    ) -> None:
        rec = TraceStep(
            step=step,
            wall_ms=wall_ms,
            llm=llm,
            next_step=next_step,
            tool_result=tool_result,
            session_after=session_after,
            enforcer_verdict=enforcer_verdict,
            enforcer_action=enforcer_action,
        )
        self._write(rec.model_dump(mode="json"))

    def append_event(
        self,
        *,
        at_step: int,
        event_kind: str,
        wait_ms: Optional[int] = None,
        attempt: Optional[int] = None,
        details: Optional[str] = None,
        repeated_tuple: Optional[list[str]] = None,
    ) -> None:
        rec = TraceEvent(
            at_step=at_step,
            event_kind=event_kind,
            wait_ms=wait_ms,
            attempt=attempt,
            details=details,
            repeated_tuple=repeated_tuple,
        )
        self._write(rec.model_dump(mode="json"))

    def append_verify(
        self,
        *,
        at_step: int,
        reasons: list[str],
        changed: bool,
    ) -> None:
        rec = TraceVerify(
            at_step=at_step,
            reasons=reasons,
            changed=changed,
        )
        self._write(rec.model_dump(mode="json"))

    def append_pcm_op(
        self,
        *,
        op: str,
        path: Optional[str],
        bytes: int,
        wall_ms: int,
        ok: bool,
        error_code: Optional[str] = None,
        origin: Optional[str] = None,
    ) -> None:
        rec = TracePcmOp(
            op=op,
            path=path,
            bytes=bytes,
            wall_ms=wall_ms,
            ok=ok,
            error_code=error_code,
            origin=origin,
        )
        self._write(rec.model_dump(mode="json"))

    def append_arch(self, record: TraceArch) -> None:
        """Write a TraceArch record (architecture decision event)."""
        self._write(record.model_dump(mode="json"))

    def append_outcome(self, outcome: TraceOutcome) -> None:
        self._write(outcome.model_dump(mode="json"))

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                self._fh.close()

    def patch_outcome_score(
        self,
        score: float,
        *,
        score_detail: Optional[list[str]] = None,
    ) -> None:
        """Back-fill the grader score (and optional detail) into the
        already-written outcome.

        T24 deviation: the agent loop writes the outcome record with
        `score: null` because it doesn't know the grader verdict — that
        only comes back from `harness.end_task()`, which runs AFTER the
        loop has returned. Without back-filling, bench_summary falls
        back to the agent's self-reported OUTCOME_OK, which systematically
        over-counts passes (observed 7 false positives on the first
        full bench run against bitgn/pac1-dev). This method rewrites
        the last outcome record in place so bench_summary sees the
        grader-assessed score.

        Observability add (2026-04-11): grader also returns
        `score_detail` — a list of human-readable strings naming which
        checks failed. Callers pass it here so content-layer failures
        can be root-caused from the trace alone (without this, the
        trace shows what the agent wrote but not what the grader
        expected).

        Must be called after close(). The file is rewritten in full
        because JSONL offers no in-place partial-line edit primitive.
        """
        with self._lock:
            if not self._fh.closed:
                raise RuntimeError(
                    "patch_outcome_score must be called after close()"
                )
            lines = self._path.read_text(encoding="utf-8").splitlines()
            for i in range(len(lines) - 1, -1, -1):
                line = lines[i].strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("kind") == "outcome":
                    rec["score"] = score
                    if score_detail is not None:
                        rec["score_detail"] = list(score_detail)
                    lines[i] = json.dumps(
                        rec, separators=(",", ":"), ensure_ascii=False
                    )
                    self._path.write_text(
                        "\n".join(lines) + "\n", encoding="utf-8"
                    )
                    return
            raise RuntimeError(
                f"no outcome record found in trace {self._path}"
            )

    def write_crash_sidecar(self, error: str, *, traceback_text: str) -> None:
        """Write <trace>_CRASHED.json. Uses a separate I/O path so a broken
        main handle does not lose the crash info."""
        sidecar = self._path.with_name(
            self._path.name.replace(".jsonl", "_CRASHED.json")
        )
        payload = {
            "error": error,
            "traceback": traceback_text,
            "partial_trace": str(self._path),
        }
        sidecar.write_text(json.dumps(payload), encoding="utf-8")

    # -- internals -------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            if self._fh.closed:
                raise RuntimeError("TraceWriter already closed")
            self._fh.write(line)
            self._fh.write("\n")
            self._fh.flush()
