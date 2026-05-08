"""Core agent step loop (§2.7).

~120 LoC. Responsibilities:
1. Build initial messages (system prompt + task description).
2. Run pre-pass via adapter.
3. Step loop up to max_steps:
   - Call backend.next_step(...).
   - ValidationError → P3 one-shot retry with critique; re-raise if retry fails.
   - Loop detector → P4 inject nudge on next turn, continue.
   - Dispatch tool via adapter. On failure feed error back to model (P1).
   - If terminal → run enforcer. On retry-exhausted failure → submit anyway.
4. Append everything to the trace.
5. Submit final outcome via adapter.submit_terminal.
"""
from __future__ import annotations

import contextvars
import json as _json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence

from pydantic import ValidationError

from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult
from bitgn_contest_agent.adapter.pcm_tracing import pcm_origin, set_pcm_origin
from bitgn_contest_agent.arch_constants import (
    ArchCategory,
    ArchResult,
    RouterSource,
)
from bitgn_contest_agent.arch_log import emit_arch, update_task_context
from bitgn_contest_agent.backend.base import Backend, Message, NextStepResult, TransientBackendError
from bitgn_contest_agent.bench.run_metrics import RunMetrics
from bitgn_contest_agent.validator import StepValidator, Verdict
from bitgn_contest_agent.prompts import critique_injection, loop_nudge, system_prompt
from bitgn_contest_agent.reactive_router import ReactiveRouter
from bitgn_contest_agent.router import Router, RoutingDecision
from bitgn_contest_agent.schemas import (
    READ_ONLY_REQ_TYPES,
    NextStep,
    ReportTaskCompletion,
)
from bitgn_contest_agent.session import Session
from bitgn_contest_agent.task_hints import hint_for_task
from bitgn_contest_agent.trace_schema import (
    StepLLMStats,
    StepSessionAfter,
    StepToolResult,
    TraceOutcome,
)
from bitgn_contest_agent.format_validator import validate_yaml_frontmatter
from bitgn_contest_agent.trace_writer import TraceWriter
from bitgn_contest_agent.verify import VerifyReason, WriteOp


_LOG = logging.getLogger(__name__)
_MAX_NUDGES = 2
_DEFAULT_BACKOFF_MS: tuple[int, ...] = (500, 1500, 4000, 10000)

# After the watchdog force-unloads a model, LM Studio needs to cold-reload
# the weights before the retried request can succeed. Observed ~9s for
# qwen3.5-35b-a3b on the 2026-04-22 PROD run; the generic backoff schedule
# above lands the first three retries inside the reload window. Wait this
# long *in addition* to the normal backoff when the last transient was a
# "Model unloaded." 400 from the watchdog path.
_POST_UNLOAD_RELOAD_SEC: float = 12.0

_ROUTER_SOURCE_MAP = {
    "regex": RouterSource.TIER1_REGEX,
    "classifier": RouterSource.TIER2_LLM,
    "unknown": RouterSource.NONE,
}


def _record_read_attempt(
    session: "Session", path: str, tool_result: "ToolResult"
) -> None:
    """Record a read dispatch into the session tracking sets.

    Called unconditionally for every `read` tool call, regardless of
    success. The validator's R1 rule uses `verified_absent` to accept
    grounding_refs that point to files the agent has evidence don't
    exist (legitimate negative grounding).
    """
    if not path:
        return
    session.attempted_reads.add(path)
    if tool_result.ok:
        return
    err = (tool_result.error or "").lower()
    # The PCM server owns the error wording; the adapter passes it through
    # verbatim (see adapter/pcm.py). We match known ENOENT substrings as a
    # heuristic. Follow-up: expose structured error_code=NOT_FOUND from the
    # adapter so this string match can become a fallback signal.
    if "file not found" in err or "no such file" in err:
        session.verified_absent.add(path)


def _extract_outbox_attachments(content: str, session: "Session") -> None:
    """Parse YAML frontmatter from an outbox write and record attachment paths.

    The terminal R5 rule rejects report_completion if any outbox attachment
    was never read — forces the agent to ground every attachment it cites.
    Defensive: silently ignores malformed YAML.
    """
    import yaml as _yaml

    # Extract YAML frontmatter between --- delimiters.
    if not content.startswith("---"):
        return
    end = content.find("\n---", 3)
    if end == -1:
        return
    try:
        fm = _yaml.safe_load(content[3:end])
    except Exception:
        return
    if not isinstance(fm, dict):
        return
    attachments = fm.get("attachments")
    if isinstance(attachments, list):
        for path in attachments:
            if isinstance(path, str) and path.strip():
                session.outbox_attachments.add(path.strip())


@dataclass(frozen=True, slots=True)
class AgentLoopResult:
    terminated_by: str
    reported: Optional[str]
    enforcer_bypassed: bool
    error_kind: Optional[str]
    error_msg: Optional[str]
    total_steps: int
    total_llm_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cached_tokens: int
    total_reasoning_tokens: int


def _write_routing_log(task_id: str, decision: RoutingDecision) -> None:
    """Append one JSONL line to artifacts/routing/run_<run_id>_routing.jsonl.

    run_id is taken from the BITGN_RUN_ID env var set by the CLI when
    a run is in progress; when unset (unit tests, ad-hoc) we skip the
    write. Best-effort — a logging failure never breaks the agent loop.
    """
    run_id = os.environ.get("BITGN_RUN_ID", "")
    if not run_id:
        return
    try:
        path = Path(f"artifacts/routing/run_{run_id}_routing.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "task_id": task_id,
            "source": decision.source,
            "category": decision.category,
            "confidence": decision.confidence,
            "extracted": decision.extracted,
            "skill_name": decision.skill_name,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with path.open("a") as f:
            f.write(_json.dumps(entry) + "\n")
    except OSError:
        # Logging is best-effort; swallow filesystem errors so a
        # missing artifacts/ dir never kills a bench run.
        return


def _extract_body(content: str) -> str:
    """Extract body text after YAML frontmatter (``---...---``).

    Returns the body if frontmatter is present, empty string otherwise.
    """
    if not content.startswith("---"):
        return ""
    # Find closing --- delimiter (skip the opening one).
    close = content.find("\n---", 3)
    if close < 0:
        return ""
    # Body starts right after the closing delimiter line.
    body_start = content.find("\n", close + 1)
    if body_start < 0:
        return ""
    return content[body_start + 1:]


def _describe_call(req: Any) -> str:
    """Compact label for one tool call (used in batched-result headers)."""
    tool = getattr(req, "tool", type(req).__name__)
    for attr in ("path", "name", "root", "from_name"):
        v = getattr(req, attr, None)
        if isinstance(v, str) and v:
            return f"{tool} {attr}={v!r}"
    pat = getattr(req, "pattern", None)
    if isinstance(pat, str) and pat:
        return f"{tool} pattern={pat!r}"
    return tool


def _dispatch_parallel(adapter: PcmAdapter, ops: List[Any]) -> List[ToolResult]:
    """Dispatch N independent ops concurrently; return results in submission order.

    Uses `contextvars.copy_context().run` per worker so the `pcm_origin`
    ContextVar (set by the agent loop to e.g. `step:7`) propagates into
    every worker's traced PCM call.
    """
    if len(ops) == 1:
        return [adapter.dispatch(ops[0])]
    with ThreadPoolExecutor(max_workers=len(ops)) as ex:
        futures = [
            ex.submit(contextvars.copy_context().run, adapter.dispatch, op)
            for op in ops
        ]
        return [f.result() for f in futures]


def _build_initial_messages(
    *,
    task_text: str,
    router: Optional[Router] = None,
    task_id: str = "",
) -> tuple[List[Message], Optional[RoutingDecision]]:
    """Construct the initial messages for a task, including any router-
    injected bitgn skill body. Also returns the routing decision so the
    caller can use it for harness-side preflight dispatch.

    Order:
      [0] system: system_prompt()
      [1] user:   task_text
      [2] user:   bitgn skill body (if router hit)
      [3] user:   task_hints.hint_for_task(task_text) (if any)

    The system prompt and task-text messages stay bit-identical across
    tasks so the provider-side prompt cache remains hot. Skill and
    task-hint injections are appended as additional user messages.

    Returns `(messages, decision)`. `decision` is None when no router
    was supplied; otherwise it carries the category, skill name, and
    extracted fields the routed-preflight dispatcher consumes.
    """
    messages: List[Message] = [
        Message(role="system", content=system_prompt()),
        Message(role="user", content=task_text),
    ]

    decision: Optional[RoutingDecision] = None
    if router is not None:
        decision = router.route(task_text)
        if task_id:
            _write_routing_log(task_id, decision)
        if decision.skill_name is not None:
            body = router.skill_body_for(decision.skill_name)
            if body is not None:
                emit_arch(
                    category=ArchCategory.SKILL_ROUTER,
                    skill=decision.skill_name,
                    source=_ROUTER_SOURCE_MAP.get(decision.source, RouterSource.NONE),
                    confidence=decision.confidence,
                    details=f"vars={decision.extracted}",
                )
                update_task_context(
                    skill=decision.skill_name,
                    category=decision.category or "UNKNOWN",
                )
                prefix = (
                    f"SKILL CONTEXT (router-injected): {decision.skill_name}\n"
                    f"Captured variables: {_json.dumps(decision.extracted)}\n\n"
                )
                messages.append(
                    Message(role="user", content=prefix + body)
                )
        else:
            emit_arch(
                category=ArchCategory.SKILL_ROUTER,
                source=_ROUTER_SOURCE_MAP.get(decision.source, RouterSource.NONE),
            )
            update_task_context(skill="-", category=decision.category or "UNKNOWN")

    task_hint = hint_for_task(task_text)
    if task_hint is not None:
        messages.append(Message(role="user", content=task_hint))

    return messages, decision


class AgentLoop:
    def __init__(
        self,
        *,
        backend: Backend,
        adapter: PcmAdapter,
        writer: TraceWriter,
        max_steps: int,
        llm_http_timeout_sec: float,
        cancel_event: Optional[threading.Event] = None,
        backend_backoff_ms: tuple[int, ...] = _DEFAULT_BACKOFF_MS,
        inflight_semaphore: Optional[threading.Semaphore] = None,
        metrics: Optional[RunMetrics] = None,
        router: Optional[Router] = None,
        reactive_router: Optional[ReactiveRouter] = None,
    ) -> None:
        self._backend = backend
        self._adapter = adapter
        self._writer = writer
        self._max_steps = max_steps
        self._llm_http_timeout_sec = llm_http_timeout_sec
        self._cancel_event = cancel_event
        self._backoff_ms = backend_backoff_ms
        self._inflight_semaphore = inflight_semaphore
        self._metrics = metrics
        self._router = router
        self._reactive_router = reactive_router
        self._validator = StepValidator(max_corrections=8)
        self._last_backend_error: Optional[str] = None

    def run(self, *, task_id: str, task_text: str) -> AgentLoopResult:
        session = Session()
        messages, decision = _build_initial_messages(
            task_text=task_text,
            router=self._router,
            task_id=task_id,
        )
        if decision is not None and decision.skill_name:
            session.skills_loaded.add(decision.skill_name)

        # Per-model: load extra skills the global router missed.
        # Gpt-oss's tier1 regex misses ~4 inbox-processing tasks per 104
        # (phrases like "inbound note", "invoice-request"). The adapter
        # hook returns those skill names so we inject their bodies here
        # before the first LLM call. Default adapter returns an empty
        # set — non-gpt-oss models see byte-identical behavior.
        self._inject_extra_reactive_skills(
            task_text=task_text, session=session, messages=messages,
        )

        # Pre-pass (best effort). The adapter returns extra user-message
        # content (currently the preflight_schema summary) that must be
        # injected into the conversation so skill bodies can reference
        # discovered workspace roots without a separate LLM step.
        prepass = self._adapter.run_prepass(
            session=session, trace_writer=self._writer
        )
        # Tolerate test mocks that return None or a bare list.
        bootstrap_content = getattr(prepass, "bootstrap_content", None)
        if bootstrap_content is None and isinstance(prepass, list):
            bootstrap_content = prepass
        if bootstrap_content:
            for content in bootstrap_content:
                messages.append(Message(role="user", content=content))

        self._writer.append_task(task_id=task_id, task_text=task_text)

        totals = _Totals()
        pending_critique: Optional[str] = None
        pending_nudge: Optional[str] = None
        pending_validation: Optional[str] = None
        reactive_injected: set[str] = set()
        read_cache: dict[str, str] = {}  # path → content at read time
        write_history: list[WriteOp] = []  # every successful write/delete/move
        verify_attempts = 0  # hard cap ≤1 per task

        step_idx = 0  # visible in except block before first iteration
        try:
          for step_idx in range(1, self._max_steps + 1):
            # Attribute all pcm_ops emitted below to this step number. The
            # TracingPcmClient reads this var per-op so preflight tools
            # dispatched inside a step inherit the label too. Leakage to
            # post-loop code is harmless — nothing after the loop makes
            # PCM calls.
            set_pcm_origin(f"step:{step_idx}")

            if self._cancel_event is not None and self._cancel_event.is_set():
                return self._finish_cancelled(totals, step_idx - 1)

            session.step = step_idx
            step_start = time.monotonic()
            if pending_critique is not None:
                messages.append(Message(role="user", content=pending_critique))
                pending_critique = None
            if pending_nudge is not None:
                messages.append(Message(role="user", content=pending_nudge))
                pending_nudge = None
            if pending_validation is not None:
                messages.append(Message(role="user", content=pending_validation))
                pending_validation = None

            # Backend call + P2 transient retry + P3 validation retry.
            step_result: NextStepResult
            try:
                maybe_step = self._call_backend_with_retry(
                    messages, at_step=step_idx
                )
                if maybe_step is None:
                    return self._finish_error(
                        totals,
                        step_idx,
                        error_kind="BACKEND_ERROR",
                        error_msg=f"transient backend exhausted: {self._last_backend_error}",
                    )
                step_result = maybe_step
                totals.prompt_tokens += maybe_step.prompt_tokens
                totals.completion_tokens += maybe_step.completion_tokens
                totals.reasoning_tokens += maybe_step.reasoning_tokens
                step_obj = maybe_step.parsed
            except ValidationError as exc:
                self._writer.append_event(
                    at_step=step_idx,
                    event_kind="validation_retry",
                    details=str(exc)[:500],
                )
                retry_messages = list(messages) + [
                    Message(
                        role="user",
                        content=self._format_critique(
                            [f"ValidationError: {exc}"], session,
                        ),
                    )
                ]
                try:
                    maybe_retry = self._call_backend_with_retry(
                        retry_messages, at_step=step_idx
                    )
                    if maybe_retry is None:
                        return self._finish_error(
                            totals,
                            step_idx,
                            error_kind="BACKEND_ERROR",
                            error_msg=f"transient backend exhausted on validation retry: {self._last_backend_error}",
                        )
                    step_result = maybe_retry
                    totals.prompt_tokens += maybe_retry.prompt_tokens
                    totals.completion_tokens += maybe_retry.completion_tokens
                    totals.reasoning_tokens += maybe_retry.reasoning_tokens
                    step_obj = maybe_retry.parsed
                except ValidationError as exc2:
                    return self._finish_error(
                        totals,
                        step_idx,
                        error_kind="BACKEND_ERROR",
                        error_msg=f"double validation failure: {exc2}",
                    )
            totals.llm_calls += 1

            # Dispatch.
            fn = step_obj.function
            tool_result: ToolResult
            enforcer_verdict: list[str] | None = None
            enforcer_action: str | None = None

            if isinstance(fn, ReportTaskCompletion):
                # Per-model post-processing of the terminal before validation.
                # Gpt-oss drops grounding_refs that were never read so
                # hallucinated paths don't reject the whole terminal via R1.
                # Default adapter hook is identity — non-gpt-oss models
                # see byte-identical behavior.
                fn = self._post_process_terminal(fn, session)
                if fn is not step_obj.function:
                    step_obj = step_obj.model_copy(update={"function": fn})
                verdict = self._validator.check_terminal(session, step_obj, step_idx)
                if verdict.ok:
                    # Pre-completion verification (spec 2026-04-21).
                    # Hard cap: 1 verification round per task.
                    if verify_attempts == 0:
                        from bitgn_contest_agent.verify import (
                            build_verification_message as _bv,
                            should_verify as _sv,
                        )
                        v_reasons = _sv(
                            next_step=step_obj,
                            session=session,
                            read_cache=read_cache,
                            write_history=write_history,
                            task_text=task_text,
                            skill_name=(decision.skill_name if decision else None),
                        )
                    else:
                        v_reasons = []
                    if v_reasons:
                        verify_attempts += 1
                        verify_messages = list(messages) + [
                            Message(
                                role="assistant",
                                content=step_obj.model_dump_json(),
                            ),
                            Message(
                                role="user",
                                content=_bv(
                                    reasons=v_reasons,
                                    next_step=step_obj,
                                    read_cache=read_cache,
                                    write_history=write_history,
                                    task_text=task_text,
                                ),
                            ),
                        ]
                        try:
                            verify_result = self._call_backend_with_retry(
                                verify_messages, at_step=step_idx,
                            )
                        except ValidationError:
                            verify_result = None
                        changed = False
                        if verify_result is not None:
                            totals.prompt_tokens += verify_result.prompt_tokens
                            totals.completion_tokens += verify_result.completion_tokens
                            totals.reasoning_tokens += verify_result.reasoning_tokens
                            totals.llm_calls += 1
                            v_step = verify_result.parsed
                            v_fn = v_step.function
                            if isinstance(v_fn, ReportTaskCompletion):
                                if v_fn.model_dump() != fn.model_dump():
                                    changed = True
                                    step_obj = v_step
                                    fn = v_fn
                        self._writer.append_verify(
                            at_step=step_idx,
                            reasons=[r.value for r in v_reasons],
                            changed=changed,
                        )
                    tool_result = self._adapter.submit_terminal(fn)
                    enforcer_action = "accept"
                else:
                    enforcer_verdict = list(verdict.reasons)
                    emit_arch(
                        category=ArchCategory.TERMINAL,
                        at_step=step_idx,
                        result=ArchResult.REJECT,
                        details="action=reject",
                        reasons=list(verdict.reasons),
                    )
                    self._writer.append_event(
                        at_step=step_idx,
                        event_kind="enforcer_reject",
                        details="; ".join(verdict.reasons)[:500],
                    )
                    # Attempt one retry by injecting critique on next turn.
                    retry_messages = list(messages) + [
                        Message(
                            role="user",
                            content=self._format_critique(verdict.reasons, session),
                        )
                    ]
                    try:
                        maybe_retry_step = self._call_backend_with_retry(
                            retry_messages, at_step=step_idx
                        )
                        if maybe_retry_step is None:
                            retry_step = step_obj  # fall through to submit_anyway
                        else:
                            totals.prompt_tokens += maybe_retry_step.prompt_tokens
                            totals.completion_tokens += maybe_retry_step.completion_tokens
                            totals.reasoning_tokens += maybe_retry_step.reasoning_tokens
                            retry_step = maybe_retry_step.parsed
                            totals.llm_calls += 1
                    except ValidationError:
                        retry_step = step_obj  # fall through to submit_anyway
                    retry_fn = retry_step.function
                    if isinstance(retry_fn, ReportTaskCompletion):
                        # Same per-model post-processing on the retry's
                        # terminal so the retry benefits from ref filtering.
                        retry_fn = self._post_process_terminal(retry_fn, session)
                        if retry_fn is not retry_step.function:
                            retry_step = retry_step.model_copy(
                                update={"function": retry_fn}
                            )
                        retry_verdict = self._validator.check_terminal(session, retry_step, step_idx)
                        if retry_verdict.ok:
                            tool_result = self._adapter.submit_terminal(retry_fn)
                            enforcer_action = "accept_after_retry"
                            fn = retry_fn
                        else:
                            tool_result = self._adapter.submit_terminal(retry_fn)
                            enforcer_action = "submit_anyway"
                            enforcer_verdict = list(retry_verdict.reasons)
                            fn = retry_fn
                    else:
                        # Retry returned a non-terminal; submit the original anyway.
                        tool_result = self._adapter.submit_terminal(fn)
                        enforcer_action = "submit_anyway"

                self._log_step(
                    step_idx,
                    step_start,
                    step_obj,
                    tool_result,
                    session,
                    prompt_tokens=step_result.prompt_tokens,
                    completion_tokens=step_result.completion_tokens,
                    reasoning_tokens=step_result.reasoning_tokens,
                    enforcer_verdict=enforcer_verdict,
                    enforcer_action=enforcer_action,
                )
                totals.steps += 1
                return self._finish_report(
                    totals,
                    reported=fn.outcome,
                    enforcer_bypassed=(enforcer_action == "submit_anyway"),
                )

            # Non-terminal: dispatch and loop-detect.
            call_tuple = _canonical_call(fn)
            if session.loop_nudge_needed(call_tuple):
                if session.nudge_budget_remaining(max_nudges=_MAX_NUDGES) > 0:
                    session.nudges_emitted += 1
                    pending_nudge = loop_nudge(call_tuple)
                    emit_arch(
                        category=ArchCategory.LOOP_NUDGE,
                        at_step=step_idx,
                        details=f"call={call_tuple}",
                    )
                    self._writer.append_event(
                        at_step=step_idx,
                        event_kind="loop_nudge",
                        repeated_tuple=list(call_tuple),
                    )
                else:
                    return self._finish_error(
                        totals,
                        step_idx,
                        error_kind="INTERNAL_CRASH",
                        error_msg="loop nudge budget exhausted",
                    )

            # Parallel-reads gate: only honor `parallel_reads` when the
            # primary `function` is itself a read-only op. The schema's
            # discriminated union prevents writes/terminals from appearing
            # inside `parallel_reads`, but the primary `function` may still
            # be any tool — so we gate here to keep mutation ordering
            # deterministic.
            primary_is_readonly = isinstance(fn, READ_ONLY_REQ_TYPES)
            requested_batch = list(getattr(step_obj, "parallel_reads", []) or [])
            if requested_batch and not primary_is_readonly:
                # Drop quietly with a trace event — the model emitted an
                # invalid combination; the primary call still runs.
                self._writer.append_event(
                    at_step=step_idx,
                    event_kind="parallel_reads_dropped",
                    details=f"non-readonly fn={getattr(fn, 'tool', '?')} dropped_count={len(requested_batch)}",
                )
                requested_batch = []

            if requested_batch:
                ops: list[Any] = [fn] + requested_batch
                tool_results = _dispatch_parallel(self._adapter, ops)
                tool_result = tool_results[0]
                self._writer.append_event(
                    at_step=step_idx,
                    event_kind="parallel_reads_dispatched",
                    details=(
                        f"count={len(ops)} tools="
                        + ",".join(getattr(o, "tool", "?") for o in ops)
                    ),
                )
            else:
                ops = [fn]
                tool_results = [self._adapter.dispatch(fn)]
                tool_result = tool_results[0]

            # Per-op session updates (read tracking, refs, caches, mutations).
            for op, op_result in zip(ops, tool_results):
                op_tool = getattr(op, "tool", "")
                if op_tool == "read":
                    _record_read_attempt(
                        session, getattr(op, "path", ""), op_result
                    )
                if not op_result.ok:
                    continue
                for ref in op_result.refs:
                    session.seen_refs.add(ref)
                if op_tool in ("write", "delete", "move"):
                    mut_path = (
                        getattr(op, "path", "") or getattr(op, "from_name", "")
                    )
                    session.mutations.append((op_tool, mut_path))
                    write_history.append(WriteOp(
                        op=op_tool,
                        path=mut_path,
                        step=step_idx,
                        content=getattr(op, "content", None) if op_tool == "write" else None,
                    ))
                    if op_tool == "write" and "outbox" in mut_path.lower():
                        _extract_outbox_attachments(
                            getattr(op, "content", ""), session
                        )
                if op_tool == "read":
                    read_path = getattr(op, "path", "")
                    if read_path and op_result.content:
                        try:
                            parsed = _json.loads(op_result.content)
                            file_text = parsed.get("content", "")
                        except (ValueError, AttributeError):
                            file_text = ""
                        if file_text:
                            read_cache[read_path] = file_text

            # Feed the tool result back to the planner.
            #
            # Two paths:
            #
            # 1. Native tool_calls path (OpenAIToolCallingBackend / gpt-oss
            #    on LM Studio). Replays the canonical OpenAI
            #    assistant-with-tool_calls + role="tool" shape so the chat
            #    template can reinject the preserved chain-of-thought into
            #    the next turn. parallel_reads is single-shot in this path
            #    (the native protocol expects one tool_call per assistant
            #    turn) so we always feed back tool_results[0].
            #
            # 2. Salvage / chat path (cliproxyapi-compatible). T24
            #    observation: cliproxyapi translates OpenAI chat-completions
            #    into Codex /v1/responses items. A role="tool" message is
            #    mapped to a function_call_output that requires a matching
            #    call_id, but our salvage assistant messages are plain JSON
            #    content — no native tool_calls — so cliproxyapi emits an
            #    empty call_id and Codex rejects the request. Wrap tool
            #    results in role="user" so they round-trip as plain text.
            if step_result.tool_calls:
                tool_body = (
                    tool_result.content
                    if tool_result.ok
                    else f"ERROR ({tool_result.error_code}): {tool_result.error}"
                )
                messages.append(
                    Message(
                        role="assistant",
                        content=None,
                        tool_calls=step_result.tool_calls,
                        reasoning=step_result.reasoning,
                    )
                )
                tool_call_id = step_result.tool_calls[0].get("id")
                messages.append(
                    Message(
                        role="tool",
                        content=tool_body,
                        tool_call_id=tool_call_id,
                    )
                )
            else:
                messages.append(
                    Message(
                        role="assistant",
                        content=step_obj.model_dump_json(),
                    )
                )
                if len(tool_results) == 1:
                    tool_body = (
                        tool_result.content
                        if tool_result.ok
                        else f"ERROR ({tool_result.error_code}): {tool_result.error}"
                    )
                    messages.append(
                        Message(
                            role="user",
                            content=f"Tool result:\n{tool_body}",
                        )
                    )
                else:
                    parts: list[str] = []
                    for idx, (op, op_result) in enumerate(zip(ops, tool_results), start=1):
                        head = f"=== call {idx}: {_describe_call(op)} ==="
                        body = (
                            op_result.content
                            if op_result.ok
                            else f"ERROR ({op_result.error_code}): {op_result.error}"
                        )
                        parts.append(f"{head}\n{body}")
                    composite = "\n\n".join(parts)
                    messages.append(
                        Message(
                            role="user",
                            content=(
                                f"Parallel tool results ({len(ops)} calls; the "
                                "first corresponds to your `function`, the rest "
                                "to `parallel_reads`):\n" + composite
                            ),
                        )
                    )

            # Format validation hook — catch YAML errors after writes.
            if getattr(fn, "tool", "") == "write" and tool_result.ok:
                write_content = ""
                if hasattr(fn, "content"):
                    write_content = fn.content
                elif hasattr(fn, "model_dump"):
                    write_content = fn.model_dump().get("content", "")
                if write_content:
                    val_result = validate_yaml_frontmatter(write_content)
                    if not val_result.ok:
                        write_path = getattr(fn, "path", "<unknown>")
                        emit_arch(
                            category=ArchCategory.FORMAT_VALIDATOR,
                            at_step=step_idx,
                            details=f"path={write_path} error={val_result.error}",
                        )
                        error_msg = (
                            f"FORMAT VALIDATION ERROR in your last write:\n"
                            f"  File: {write_path}\n"
                            f"  Error: {val_result.error}\n"
                        )
                        if val_result.line is not None:
                            error_msg += f"  Line: {val_result.line}\n"
                        error_msg += "\nFix the error and rewrite the file."
                        messages.append(
                            Message(role="user", content=error_msg)
                        )
                        self._writer.append_event(
                            at_step=step_idx,
                            event_kind="format_validation_error",
                            details=error_msg[:500],
                        )

            # Body preservation hook — after writing to a previously-read
            # non-outbox file, verify the body text wasn't altered.
            if getattr(fn, "tool", "") == "write" and tool_result.ok:
                write_path = getattr(fn, "path", "")
                if (
                    write_path
                    and write_path in read_cache
                    and "outbox" not in write_path.lower()
                ):
                    new_content = ""
                    if hasattr(fn, "content"):
                        new_content = fn.content
                    elif hasattr(fn, "model_dump"):
                        new_content = fn.model_dump().get("content", "")
                    cached = read_cache[write_path]
                    old_body = _extract_body(cached)
                    # If original had no frontmatter, entire content is body.
                    if not old_body and not cached.startswith("---"):
                        old_body = cached
                    new_body = _extract_body(new_content)
                    if old_body and new_body and old_body != new_body:
                        emit_arch(
                            category=ArchCategory.BODY_PRESERVATION,
                            at_step=step_idx,
                            details=f"path={write_path} old_len={len(old_body)} new_len={len(new_body)}",
                        )
                        body_msg = (
                            f"BODY PRESERVATION ERROR in your last write:\n"
                            f"  File: {write_path}\n"
                            f"  The original body text was altered during migration.\n"
                            f"  Expected body length: {len(old_body)} chars\n"
                            f"  Actual body length:   {len(new_body)} chars\n"
                            f"\nRe-read the file and rewrite it, preserving the EXACT "
                            f"original body below the closing `---` delimiter. "
                            f"No extra blank lines, no reformatting."
                        )
                        messages.append(
                            Message(role="user", content=body_msg)
                        )
                        self._writer.append_event(
                            at_step=step_idx,
                            event_kind="body_preservation_error",
                            details=body_msg[:500],
                        )

            # Reactive routing hook — inject skill body mid-conversation
            # when a tool dispatch matches a reactive skill trigger.
            reactive_injected_this_step = False
            if self._reactive_router is not None and tool_result.ok:
                fn_dump = fn.model_dump() if hasattr(fn, "model_dump") else {}
                reactive_decision = self._reactive_router.evaluate(
                    tool_name=getattr(fn, "tool", ""),
                    tool_args=fn_dump,
                    tool_result_text=tool_result.content,
                    already_injected=frozenset(reactive_injected),
                    backend=self._backend,
                )
                if reactive_decision is not None:
                    reactive_injected_this_step = True
                    reactive_injected.add(reactive_decision.skill_name)
                    session.skills_loaded.add(reactive_decision.skill_name)
                    trigger_path = fn_dump.get("path") or fn_dump.get("root") or ""
                    emit_arch(
                        category=ArchCategory.REACTIVE,
                        at_step=step_idx,
                        skill=reactive_decision.skill_name,
                        source=_ROUTER_SOURCE_MAP.get(reactive_decision.source, RouterSource.NONE),
                        confidence=reactive_decision.confidence,
                        details=f"trigger={getattr(fn, 'tool', '')}({trigger_path})",
                    )
                    prefix = (
                        f"REACTIVE SKILL CONTEXT (mid-task): {reactive_decision.skill_name}\n"
                        f"Triggered by: {getattr(fn, 'tool', '')}({trigger_path})\n\n"
                    )
                    messages.append(
                        Message(role="user", content=prefix + reactive_decision.body)
                    )

            # Step validator — deferred injection for next step.
            if tool_result.ok:
                correction = self._validator.check_step(
                    step_obj=step_obj,
                    session=session,
                    step_idx=step_idx,
                    max_steps=self._max_steps,
                    reactive_injected_this_step=reactive_injected_this_step,
                )
                if correction is not None:
                    pending_validation = correction
                    _LOG.info("[ARCH:VALIDATOR] step=%d correction=%s", step_idx, correction[:120])
                    self._writer.append_event(
                        at_step=step_idx,
                        event_kind="validator_correction",
                        details=correction[:500],
                    )

            self._log_step(
                step_idx,
                step_start,
                step_obj,
                tool_result,
                session,
                prompt_tokens=step_result.prompt_tokens,
                completion_tokens=step_result.completion_tokens,
                reasoning_tokens=step_result.reasoning_tokens,
            )
            totals.steps += 1
        except Exception as exc:
            _LOG.exception(
                "unhandled crash at step %d of task %s: %s",
                step_idx, task_id, exc,
            )
            return self._finish_error(
                totals,
                step_idx,
                error_kind="INTERNAL_CRASH",
                error_msg=f"{type(exc).__name__}: {exc}",
            )

        # Exhausted max_steps.
        return self._finish_error(
            totals,
            self._max_steps,
            error_kind="MAX_STEPS",
            error_msg=f"exceeded max_steps={self._max_steps}",
        )

    # -- helpers ---------------------------------------------------------

    def _model_adapter(self):
        """Return the per-model ``ModelAdapter`` on the backend, if any.

        ``OpenAIToolCallingBackend`` exposes ``model_adapter`` (v0.1.25+).
        The frontier ``OpenAIChatBackend`` and test-stub backends do not;
        in those cases we return ``None`` and callers fall back to the
        pre-hook default behavior.
        """
        return getattr(self._backend, "model_adapter", None)

    def _format_critique(
        self,
        reasons: Sequence[str],
        session: Session,
    ) -> str:
        """Build the user-message for a validator/validation rejection.

        Default = ``prompts.critique_injection``. When the backend has a
        ``model_adapter``, delegate to ``adapter.format_retry_critique``
        so gpt-oss can rewrite the prompt as an imperative tool-call
        prescription (2026-04-23 v0.1.24 evidence: 15 R7 tasks ignored
        the descriptive wording).
        """
        adapter = self._model_adapter()
        if adapter is not None:
            return adapter.format_retry_critique(reasons, session)
        return critique_injection(list(reasons))

    def _post_process_terminal(
        self,
        fn: "ReportTaskCompletion",
        session: Session,
    ) -> "ReportTaskCompletion":
        """Per-model mutation of a terminal ``report_completion`` before
        ``StepValidator.check_terminal`` runs. Default = identity; gpt-oss
        drops grounding_refs that were never read to avoid R1 rejection
        cascades on hallucinated paths.
        """
        adapter = self._model_adapter()
        if adapter is None:
            return fn
        return adapter.post_process_terminal(fn, session)

    def _inject_extra_reactive_skills(
        self,
        *,
        task_text: str,
        session: Session,
        messages: List[Message],
    ) -> None:
        """Load additional skills the adapter declares for this task text.

        Called once at task start, right after the proactive router ran.
        Skips any skill that's already in ``session.skills_loaded``
        (proactive router already injected it). Uses
        ``router.skill_body_for`` for the lookup; if the router is not
        attached or can't find the skill body, the skill is skipped
        silently so tests with mocked backends stay green.
        """
        adapter = self._model_adapter()
        if adapter is None or self._router is None:
            return
        extras = adapter.extra_reactive_skills(task_text)
        if not extras:
            return
        for skill_name in sorted(extras):
            if skill_name in session.skills_loaded:
                continue
            body = self._router.skill_body_for(skill_name)
            if body is None:
                _LOG.warning(
                    "extra_reactive_skills: adapter requested %r but router "
                    "has no body for it — skipping",
                    skill_name,
                )
                continue
            session.skills_loaded.add(skill_name)
            emit_arch(
                category=ArchCategory.SKILL_ROUTER,
                skill=skill_name,
                source=RouterSource.ADAPTER_EXTRA,
                details=f"adapter={type(adapter).__name__}",
            )
            prefix = (
                f"SKILL CONTEXT (adapter-extra, model={adapter.name}): "
                f"{skill_name}\n\n"
            )
            messages.append(Message(role="user", content=prefix + body))

    def _call_backend_with_retry(
        self,
        messages: List[Message],
        *,
        at_step: int,
    ) -> Optional[NextStepResult]:
        """P2 — bounded exponential backoff on TransientBackendError.

        Returns NextStepResult on success, or None if all attempts exhausted
        (caller should then finish with BACKEND_ERROR). ValidationError
        propagates to the caller's P3 handler. When an inflight_semaphore is
        configured, the entire retry loop runs inside an acquire — a rate-
        limited request keeps its slot across backoffs so the remote has a
        chance to cool down before another caller tries.
        """
        def _do_retry_loop() -> Optional[NextStepResult]:
            last_exc: Optional[Exception] = None
            for attempt, wait_ms in enumerate([0, *self._backoff_ms], start=0):
                if wait_ms > 0:
                    self._writer.append_event(
                        at_step=at_step,
                        event_kind="rate_limit_backoff",
                        wait_ms=wait_ms,
                        attempt=attempt,
                        details=str(last_exc) if last_exc else None,
                    )
                    time.sleep(wait_ms / 1000.0)
                try:
                    result = self._backend.next_step(
                        messages=messages,
                        response_schema=NextStep,
                        timeout_sec=self._llm_http_timeout_sec,
                    )
                    return result
                except TransientBackendError as exc:
                    last_exc = exc
                    _LOG.warning(
                        "transient backend error (attempt %d at step %d): %s",
                        attempt, at_step, exc,
                    )
                    if self._metrics is not None:
                        self._metrics.on_rate_limit_error()
                    if "model unloaded" in str(exc).lower():
                        _LOG.info(
                            "post-watchdog-unload reload wait %.1fs at step %d",
                            _POST_UNLOAD_RELOAD_SEC, at_step,
                        )
                        time.sleep(_POST_UNLOAD_RELOAD_SEC)
                    continue
            if last_exc is not None:
                self._last_backend_error = str(last_exc)
                return None
            return None

        # Metrics observe the full call cycle (queue wait + semaphore + retries)
        if self._metrics is not None:
            self._metrics.on_call_start()
        try:
            if self._inflight_semaphore is not None:
                with self._inflight_semaphore:
                    return _do_retry_loop()
            return _do_retry_loop()
        finally:
            if self._metrics is not None:
                self._metrics.on_call_end()

    def _log_step(
        self,
        step_idx: int,
        step_start: float,
        step_obj: NextStep,
        tool_result: ToolResult,
        session: Session,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int,
        enforcer_verdict: list[str] | None = None,
        enforcer_action: str | None = None,
    ) -> None:
        wall_ms = int((time.monotonic() - step_start) * 1000)
        self._writer.append_step(
            step=step_idx,
            wall_ms=wall_ms,
            llm=StepLLMStats(
                latency_ms=wall_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
                cached_tokens=0,
                retry_count=0,
            ),
            next_step=step_obj.model_dump(),
            tool_result=StepToolResult(
                ok=tool_result.ok,
                bytes=tool_result.bytes,
                wall_ms=tool_result.wall_ms,
                truncated=tool_result.truncated,
                original_bytes=tool_result.original_bytes,
                error=tool_result.error,
                error_code=tool_result.error_code,
            ),
            session_after=StepSessionAfter(
                seen_refs_count=len(session.seen_refs),
                identity_loaded=session.identity_loaded,
                rulebook_loaded=session.rulebook_loaded,
                mutation_count=len(session.mutations),
            ),
            enforcer_verdict=enforcer_verdict,
            enforcer_action=enforcer_action,
        )

    def _finish_report(
        self,
        totals: "_Totals",
        *,
        reported: str,
        enforcer_bypassed: bool,
    ) -> AgentLoopResult:
        outcome = TraceOutcome(
            terminated_by="report_completion",
            reported=reported,
            enforcer_bypassed=enforcer_bypassed,
            error_kind=None,
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
            total_reasoning_tokens=totals.reasoning_tokens,
        )
        self._writer.append_outcome(outcome)
        return AgentLoopResult(
            terminated_by="report_completion",
            reported=reported,
            enforcer_bypassed=enforcer_bypassed,
            error_kind=None,
            error_msg=None,
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
            total_reasoning_tokens=totals.reasoning_tokens,
        )

    def _finish_error(
        self,
        totals: "_Totals",
        step_idx: int,
        *,
        error_kind: str,
        error_msg: str,
    ) -> AgentLoopResult:
        outcome = TraceOutcome(
            terminated_by="error" if error_kind != "MAX_STEPS" else "exhausted",
            reported=None,
            enforcer_bypassed=False,
            error_kind=error_kind,
            error_msg=error_msg,
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
            total_reasoning_tokens=totals.reasoning_tokens,
        )
        self._writer.append_outcome(outcome)
        return AgentLoopResult(
            terminated_by=outcome.terminated_by,
            reported=None,
            enforcer_bypassed=False,
            error_kind=error_kind,
            error_msg=error_msg,
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
            total_reasoning_tokens=totals.reasoning_tokens,
        )

    def _finish_cancelled(self, totals: "_Totals", step_idx: int) -> AgentLoopResult:
        # Synthetic cancel-path terminal. BYPASSES the enforcer — written
        # directly by the worker per §3.2.
        outcome = TraceOutcome(
            terminated_by="cancel",
            reported="OUTCOME_ERR_INTERNAL",
            enforcer_bypassed=True,
            error_kind="CANCELLED",
            error_msg="cancelled:timeout",
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
            total_reasoning_tokens=totals.reasoning_tokens,
        )
        self._writer.append_outcome(outcome)
        return AgentLoopResult(
            terminated_by="cancel",
            reported="OUTCOME_ERR_INTERNAL",
            enforcer_bypassed=True,
            error_kind="CANCELLED",
            error_msg="cancelled:timeout",
            total_steps=totals.steps,
            total_llm_calls=totals.llm_calls,
            total_prompt_tokens=totals.prompt_tokens,
            total_completion_tokens=totals.completion_tokens,
            total_cached_tokens=totals.cached_tokens,
            total_reasoning_tokens=totals.reasoning_tokens,
        )


@dataclass(slots=True)
class _Totals:
    steps: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


def _canonical_call(fn: object) -> tuple[str, ...]:
    """Produce a stable (tool, sorted-args) tuple for the loop detector."""
    if hasattr(fn, "tool"):
        tool = getattr(fn, "tool")
    else:
        tool = type(fn).__name__
    # Use model_dump so every Req_* turns into a dict of primitives.
    if hasattr(fn, "model_dump"):
        data = fn.model_dump()  # type: ignore[attr-defined]
    else:
        data = {}
    parts = [tool] + [f"{k}={data[k]!r}" for k in sorted(data.keys()) if k != "tool"]
    return tuple(parts)
