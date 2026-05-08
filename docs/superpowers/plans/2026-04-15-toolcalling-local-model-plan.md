# Local-Model Tool-Calling (Plan B-local) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `liquid/lfm2-24b-a2b` and `openai/gpt-oss-20b` drivable through the bitgn agent loop via the native OpenAI tool-calling backend, gated by `AGENT_TOOLCALLING=1`, without regressing the cliproxyapi frontier path.

**Architecture:** Keep today's `OpenAIToolCallingBackend` (already landed in `d3ecb65` / `c8b938e`). Add a content-only salvage path that parses the two malformed shapes these models emit — the bare OpenAI tool shape `{"name","arguments"}` (lfm2) and a `NextStep`-like envelope — so small local models make forward progress instead of losing every turn to `BACKEND_ERROR`. Then run local smoke and a full 104-task PROD benchmark on `gpt-oss-20b`.

**Tech Stack:** Python 3.12, `openai` SDK, `pydantic` v2, `pytest`, LM Studio (OpenAI-compatible local server), bitgn benchmark harness.

---

## Context links

- Spec: `docs/superpowers/specs/2026-04-15-toolcalling-local-model-design.md`
- Backend: `src/bitgn_contest_agent/backend/openai_toolcalling.py`
- Tests: `tests/test_backend_openai_toolcalling.py`
- Factory: `src/bitgn_contest_agent/cli.py:110-125`
- Prior art commits on this branch: `6a0f25c` (spec), `d3ecb65` (backend), `c8b938e` (envelope defaults + directive critique)

---

## Phase 0: Baseline verification

### Task 0.1: Confirm branch is clean and tests pass

**Files:**
- Read: `src/bitgn_contest_agent/backend/openai_toolcalling.py`
- Read: `tests/test_backend_openai_toolcalling.py`

- [ ] **Step 1: Stash or keep the working-tree salvage diff aside**

The branch has an unstaged diff in `openai_toolcalling.py` adding `_extract_first_json_object`, `_try_salvage_from_content`, `_VALID_TOOL_NAMES`, and the salvage wiring in `next_step`. This plan re-introduces that code through TDD in Phase 1, so the working-tree version should be discarded or stashed:

```bash
git stash push -u -m "plan-b-local-pre-tdd-salvage" \
    src/bitgn_contest_agent/backend/openai_toolcalling.py
```

Run: `git stash list | head -1`
Expected: the stash entry is listed.

- [ ] **Step 2: Confirm merge base equals main tip**

Run: `git merge-base main local-toolcalling-lfm2 && git rev-parse main`
Expected: both hashes are identical — no rebase needed.

- [ ] **Step 3: Confirm the test file imports still resolve without salvage**

Run: `python -c "from bitgn_contest_agent.backend.openai_toolcalling import OpenAIToolCallingBackend, build_tool_catalog, _build_next_step; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass (green baseline).

- [ ] **Step 5: Commit nothing — this is baseline verification only**

No commit. Proceed to Phase 1.

---

## Phase 1: Content-only salvage path (TDD)

Adds the fallback declared in the spec under *Risks* (lines 115–117): parse free-text `{"name","arguments"}` and `NextStep`-shaped JSON from `message.content` when `tool_choice="required"` is ignored.

### Task 1.1: Add `_extract_first_json_object` helper — empty / no-JSON cases

**Files:**
- Modify: `src/bitgn_contest_agent/backend/openai_toolcalling.py` — add helper after `_build_next_step`
- Modify: `tests/test_backend_openai_toolcalling.py` — add a new test group near the bottom of the file

- [ ] **Step 1: Write the failing tests for the simple cases**

Append to `tests/test_backend_openai_toolcalling.py`:

```python
from bitgn_contest_agent.backend.openai_toolcalling import (
    _extract_first_json_object,
)


def test_extract_first_json_object_returns_none_for_empty_string() -> None:
    assert _extract_first_json_object("") is None


def test_extract_first_json_object_returns_none_when_no_braces() -> None:
    assert _extract_first_json_object("plain prose, nothing to parse") is None


def test_extract_first_json_object_parses_bare_object() -> None:
    assert _extract_first_json_object('{"a": 1}') == {"a": 1}
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `pytest tests/test_backend_openai_toolcalling.py::test_extract_first_json_object_returns_none_for_empty_string -v`
Expected: FAIL with `ImportError: cannot import name '_extract_first_json_object'`.

- [ ] **Step 3: Add the helper (minimal body, empty / bare-object only)**

Insert after `_build_next_step` in `src/bitgn_contest_agent/backend/openai_toolcalling.py`:

```python
def _extract_first_json_object(text: str) -> Dict[str, Any] | None:
    """Find and parse the first balanced ``{...}`` JSON object in ``text``.

    Small local models sometimes wrap their JSON in prose or code fences.
    Scan for a brace-balanced object and attempt ``json.loads`` on it.
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        obj = _json.loads(candidate)
                    except _json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_backend_openai_toolcalling.py -k extract_first_json_object -v`
Expected: 3 passing.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/backend/openai_toolcalling.py \
        tests/test_backend_openai_toolcalling.py
git commit -m "feat(toolcalling): _extract_first_json_object brace-balanced JSON scan"
```

### Task 1.2: `_extract_first_json_object` — prose-wrapped and nested cases

**Files:**
- Modify: `tests/test_backend_openai_toolcalling.py`
- Modify: `src/bitgn_contest_agent/backend/openai_toolcalling.py` (no code change — existing helper already handles these; this task proves it)

- [ ] **Step 1: Add the harder tests**

Append:

```python
def test_extract_first_json_object_parses_object_wrapped_in_prose() -> None:
    text = 'Sure, here you go:\n{"name": "read", "arguments": {"path": "x"}}\nHope that helps.'
    assert _extract_first_json_object(text) == {
        "name": "read", "arguments": {"path": "x"},
    }


def test_extract_first_json_object_handles_braces_inside_strings() -> None:
    text = '{"s": "has { brace", "n": 1}'
    assert _extract_first_json_object(text) == {"s": "has { brace", "n": 1}


def test_extract_first_json_object_handles_nested_objects() -> None:
    text = '{"outer": {"inner": {"leaf": 1}}}'
    assert _extract_first_json_object(text) == {
        "outer": {"inner": {"leaf": 1}},
    }


def test_extract_first_json_object_skips_broken_first_object_and_finds_next() -> None:
    text = 'garbage {not-json:here} then {"ok": 1}'
    assert _extract_first_json_object(text) == {"ok": 1}
```

- [ ] **Step 2: Run the tests to verify they pass on the existing helper**

Run: `pytest tests/test_backend_openai_toolcalling.py -k extract_first_json_object -v`
Expected: 7 passing (the 3 from Task 1.1 plus these 4).

If any fails, do not modify the helper silently — investigate. The existing implementation should handle all four cases.

- [ ] **Step 3: Commit**

```bash
git add tests/test_backend_openai_toolcalling.py
git commit -m "test(toolcalling): cover prose-wrapped, nested, and recovery JSON-scan paths"
```

### Task 1.3: `_try_salvage_from_content` — bare `{"name","arguments"}` shape

**Files:**
- Modify: `src/bitgn_contest_agent/backend/openai_toolcalling.py`
- Modify: `tests/test_backend_openai_toolcalling.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
from bitgn_contest_agent.backend.openai_toolcalling import (
    _try_salvage_from_content,
)


def test_salvage_parses_bare_name_arguments_shape() -> None:
    """lfm2 emits the OpenAI tool shape as free text. Salvage it."""
    content = '{"name": "read", "arguments": {"path": "AGENTS.md"}}'
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"


def test_salvage_rejects_unknown_tool_name() -> None:
    content = '{"name": "rm_minus_rf", "arguments": {"path": "/"}}'
    assert _try_salvage_from_content(content) is None


def test_salvage_returns_none_on_empty_content() -> None:
    assert _try_salvage_from_content("") is None


def test_salvage_returns_none_when_arguments_missing() -> None:
    content = '{"name": "read"}'
    assert _try_salvage_from_content(content) is None
```

- [ ] **Step 2: Run to confirm the tests fail**

Run: `pytest tests/test_backend_openai_toolcalling.py -k salvage -v`
Expected: FAIL with `ImportError: cannot import name '_try_salvage_from_content'`.

- [ ] **Step 3: Add the valid-tool frozenset and salvage helper (name+arguments branch only)**

In `src/bitgn_contest_agent/backend/openai_toolcalling.py`, after `_extract_first_json_object`:

```python
_VALID_TOOL_NAMES: frozenset[str] = frozenset({
    "read", "write", "delete", "mkdir", "move",
    "list", "tree", "find", "search", "context",
    "report_completion",
})


def _try_salvage_from_content(content: str) -> NextStep | None:
    """Attempt to build a NextStep from a content-only reply.

    Shape 1: ``{"name": "<tool>", "arguments": {...}}`` — bare OpenAI tool
    shape emitted as free text (liquid/lfm2 trained behavior).
    """
    obj = _extract_first_json_object(content)
    if obj is None:
        return None
    if "name" in obj and isinstance(obj.get("arguments"), dict):
        tool_name = obj.get("name")
        if tool_name in _VALID_TOOL_NAMES:
            try:
                return _build_next_step(tool_name, obj["arguments"])
            except ValidationError:
                return None
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_backend_openai_toolcalling.py -k salvage -v`
Expected: 4 passing.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/backend/openai_toolcalling.py \
        tests/test_backend_openai_toolcalling.py
git commit -m "feat(toolcalling): salvage content-only bare {name,arguments} replies"
```

### Task 1.4: `_try_salvage_from_content` — full NextStep envelope shape

**Files:**
- Modify: `src/bitgn_contest_agent/backend/openai_toolcalling.py`
- Modify: `tests/test_backend_openai_toolcalling.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_salvage_parses_full_next_step_envelope_shape() -> None:
    """gpt-oss-20b sometimes emits the full envelope as free text."""
    payload = {
        **_envelope_copy(),
        "function": {"tool": "read", "path": "AGENTS.md"},
    }
    content = f"Sure thing:\n{json.dumps(payload)}\n"
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"
    assert ns.current_state == "reading rules"


def test_salvage_returns_none_for_envelope_missing_function_tool() -> None:
    payload = {**_envelope_copy(), "function": {"tool": "read"}}  # no path
    content = json.dumps(payload)
    assert _try_salvage_from_content(content) is None


def test_salvage_prefers_name_arguments_shape_when_both_keys_present() -> None:
    """If content contains {name, arguments, function}, the name/arguments
    branch wins (it's the one small models emit — the envelope key is
    coincidental)."""
    content = json.dumps({
        "name": "read",
        "arguments": {"path": "A.md"},
        "function": {"tool": "write", "path": "B.md", "content": "x"},
    })
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "A.md"
```

- [ ] **Step 2: Run to confirm the first test fails**

Run: `pytest tests/test_backend_openai_toolcalling.py::test_salvage_parses_full_next_step_envelope_shape -v`
Expected: FAIL — the existing helper returns `None` for envelope-shaped JSON.

- [ ] **Step 3: Extend `_try_salvage_from_content` with the envelope branch**

Replace the helper body with:

```python
def _try_salvage_from_content(content: str) -> NextStep | None:
    """Attempt to build a NextStep from a content-only reply.

    Two shapes to handle:
      1. ``{"name": "<tool>", "arguments": {...}}`` — bare OpenAI tool
         shape emitted as free text (liquid/lfm2 trained behavior).
      2. ``{"current_state": ..., "function": {"tool": ..., ...}}`` — the
         full NextStep envelope that the OpenAIChatBackend expects.

    Returns the parsed ``NextStep`` on success, ``None`` otherwise.
    """
    obj = _extract_first_json_object(content)
    if obj is None:
        return None
    if "name" in obj and isinstance(obj.get("arguments"), dict):
        tool_name = obj.get("name")
        if tool_name in _VALID_TOOL_NAMES:
            try:
                return _build_next_step(tool_name, obj["arguments"])
            except ValidationError:
                return None
    if "function" in obj and isinstance(obj["function"], dict):
        try:
            return NextStep.model_validate(obj)
        except ValidationError:
            return None
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_backend_openai_toolcalling.py -k salvage -v`
Expected: 7 passing (4 from Task 1.3 plus 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/backend/openai_toolcalling.py \
        tests/test_backend_openai_toolcalling.py
git commit -m "feat(toolcalling): salvage full NextStep envelope emitted as content"
```

### Task 1.5: Wire salvage into `next_step`

**Files:**
- Modify: `src/bitgn_contest_agent/backend/openai_toolcalling.py` — the `if not tool_calls:` branch of `next_step`
- Modify: `tests/test_backend_openai_toolcalling.py`

- [ ] **Step 1: Write the failing wiring tests**

Append:

```python
def _mk_content_only_completion(*, content: str,
                                prompt_tokens: int = 4,
                                completion_tokens: int = 2) -> MagicMock:
    msg = MagicMock()
    msg.tool_calls = []
    msg.content = content
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    completion.usage = MagicMock(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        completion_tokens_details=MagicMock(reasoning_tokens=0),
    )
    return completion


def test_next_step_salvages_content_only_name_arguments_reply() -> None:
    """When tool_calls is empty but content holds a bare {name,arguments}
    object, the backend salvages it into a NextStep instead of raising."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = \
        _mk_content_only_completion(
            content='{"name": "read", "arguments": {"path": "AGENTS.md"}}',
        )
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    out = backend.next_step(
        [Message(role="user", content="t")], NextStep, 30.0,
    )
    assert isinstance(out, NextStepResult)
    assert out.parsed.function.tool == "read"
    assert out.parsed.function.path == "AGENTS.md"
    assert out.prompt_tokens == 4
    assert out.completion_tokens == 2


def test_next_step_raises_validation_error_when_salvage_fails() -> None:
    """Empty content (no JSON to salvage) must still surface ValidationError."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = \
        _mk_content_only_completion(content="I don't know what to do.")
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(ValidationError):
        backend.next_step([Message(role="user", content="t")], NextStep, 30.0)
```

Also update the pre-existing `test_next_step_no_tool_calls_is_validation_error`
to assert the *critique* message explicitly mentions the OpenAI tool_calls
mechanism (so the critique remains informative even after salvage lands):

```python
def test_next_step_no_tool_calls_is_validation_error() -> None:
    """Content-only replies that cannot be salvaged (no JSON) surface as
    ValidationError so the agent's P3 critique retry kicks in."""
    fake_client = MagicMock()
    msg = MagicMock()
    msg.tool_calls = []
    msg.content = ""
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    completion.usage = MagicMock(prompt_tokens=1, completion_tokens=0,
                                 completion_tokens_details=None)
    fake_client.chat.completions.create.return_value = completion
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(ValidationError) as ei:
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )
    assert "tool_calls" in str(ei.value)
```

- [ ] **Step 2: Run to confirm the new tests fail**

Run: `pytest tests/test_backend_openai_toolcalling.py -k "salvages_content_only or raises_validation_error_when_salvage_fails or no_tool_calls_is_validation_error" -v`
Expected: FAIL — today's `next_step` raises unconditionally when `tool_calls` is empty; and the existing critique text does not yet mention `tool_calls`.

- [ ] **Step 3: Update `next_step` to call salvage before raising**

Replace the `if not tool_calls:` block in `next_step` with:

```python
        choice = completion.choices[0]
        tool_calls = getattr(choice.message, "tool_calls", None) or []
        content = getattr(choice.message, "content", None) or ""
        if not tool_calls:
            # LM Studio (and similar local servers) do not always honor
            # tool_choice="required" — small models may emit the OpenAI
            # tool shape {"name","arguments"} as free-text content, or
            # even a NextStep-like JSON blob. Try to salvage either shape
            # before giving up with a critique.
            salvaged = _try_salvage_from_content(content)
            if salvaged is not None:
                parsed = salvaged
            else:
                content_head = content[:200]
                raise ValidationError.from_exception_data(
                    "NextStep",
                    [
                        {
                            "type": "missing",
                            "loc": ("function",),
                            "input": {"hint": (
                                "You replied with prose instead of a tool "
                                "call. You MUST call exactly one tool per "
                                "turn using the OpenAI tool_calls mechanism "
                                "(not free text). "
                                f"Your content started with: {content_head!r}"
                            )},
                        }
                    ],
                )
        else:
            call = tool_calls[0]
            raw_args = call.function.arguments or "{}"
            try:
                args = _json.loads(raw_args)
            except _json.JSONDecodeError as exc:
                raise ValidationError.from_exception_data(
                    "NextStep",
                    [
                        {
                            "type": "json_invalid",
                            "loc": ("function",),
                            "input": raw_args,
                            "ctx": {"error": str(exc)},
                        }
                    ],
                )
            parsed = _build_next_step(call.function.name, args)
```

- [ ] **Step 4: Run the full test file**

Run: `pytest tests/test_backend_openai_toolcalling.py -v`
Expected: all tests pass.

- [ ] **Step 5: Run the whole suite to catch regressions**

Run: `pytest -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/backend/openai_toolcalling.py \
        tests/test_backend_openai_toolcalling.py
git commit -m "feat(toolcalling): wire content-only salvage into next_step"
```

### Task 1.6: Self-critique of Phase 1

- [ ] **Step 1: Re-read the spec Testing section (lines 101–111) and verify coverage**

Spec says tests must cover: (1) tool catalog shape (11 tools, envelope inlined, param types), (2) `tool_call → NextStep` happy path for `Req_Read`, (3) `ReportTaskCompletion` mapping, (4) empty-string envelope surfaces as `ValidationError`, (5) `RateLimitError` → `TransientBackendError`. Verify each has a test. Tasks 1.1–1.5 add the salvage-path coverage that the spec's *Risks* section anticipates.

Run: `pytest tests/test_backend_openai_toolcalling.py --collect-only -q | wc -l`
Expected: at least 20 test entries (original 13 plus 7+ from this phase).

- [ ] **Step 2: No commit — proceed to Phase 2.**

---

## Phase 2: Local smoke on `gpt-oss-20b`

Spec §Testing.3 — single task end-to-end via LM Studio.

### Task 2.1: Verify `.env` local-inference defaults

**Files:**
- Read: `.env`

- [ ] **Step 1: Confirm the local-inference env vars are set**

Run: `grep -E '^(AGENT_TOOLCALLING|AGENT_MODEL|LLM_HTTP_TIMEOUT_SEC|TASK_TIMEOUT_SEC|MAX_PARALLEL_TASKS|MAX_INFLIGHT_LLM|CLIPROXY_BASE_URL)=' .env`
Expected: every listed var is present. `AGENT_TOOLCALLING=1`, `CLIPROXY_BASE_URL` points at LM Studio (typically `http://127.0.0.1:1234/v1`).

Spec §Timeouts / concurrency (lines 82–89) recommends:
- `LLM_HTTP_TIMEOUT_SEC=180`
- `TASK_TIMEOUT_SEC=900`
- `MAX_PARALLEL_TASKS=2`
- `MAX_INFLIGHT_LLM=2`

If any value disagrees, update `.env` to match (do not commit `.env` — it is gitignored).

- [ ] **Step 2: Confirm LM Studio is serving `gpt-oss-20b`**

Run: `curl -sS "$(grep '^CLIPROXY_BASE_URL=' .env | cut -d= -f2-)/models" | python -m json.tool | head -40`
Expected: JSON listing includes `openai/gpt-oss-20b` (or the `AGENT_MODEL` identifier in `.env`).

If the model is not loaded, load it in LM Studio and retry before proceeding.

- [ ] **Step 3: No commit — env configuration only.**

### Task 2.2: Smoke `run-task` on one task

**Files:**
- No code changes.

- [ ] **Step 1: Pick a simple deterministic task**

Run: `ls benchmarks/*/tasks | head -5` and pick the first task directory (e.g. `t01` or the earliest lexical ID).

- [ ] **Step 2: Execute the task end-to-end**

Run: `AGENT_TOOLCALLING=1 bitgn-agent run-task --task-id <id>`
(If no console script is installed: `python -m bitgn_contest_agent.cli run-task --task-id <id>`.)
Expected: the run terminates with a single `ReportTaskCompletion` and exit code 0.

- [ ] **Step 3: Inspect the trace for `BACKEND_ERROR` events**

Find the trace log under `.claude-logs/` or `logs/` (whichever the project uses):
Run: `grep -l 'BACKEND_ERROR' $(find .claude-logs logs -type f -name '*.jsonl' -mtime -1 2>/dev/null) | head`
Expected: no hits. If there are hits, count them:
Run: `grep -c 'BACKEND_ERROR' <trace-file>`
Acceptance: fewer than 3 hits per task is acceptable on first smoke (P3 critique retry absorbs transient ones). Zero is the target.

- [ ] **Step 4: Record the smoke result in a scratch note**

Append one line to `docs/superpowers/plans/2026-04-15-toolcalling-local-model-plan.md` (under a new `## Smoke log` heading, appended at EOF):

```
- YYYY-MM-DD <task-id> — steps=N, outcome=<OK|DENIED|NONE>, backend_errors=K
```

- [ ] **Step 5: Commit the smoke note**

```bash
git add docs/superpowers/plans/2026-04-15-toolcalling-local-model-plan.md
git commit -m "docs(plan-b-local): smoke log — gpt-oss-20b task <id> <outcome>"
```

### Task 2.3: Self-critique of Phase 2

- [ ] **Step 1: Decide whether to proceed to full PROD**

Criteria:
- Smoke task finished within `TASK_TIMEOUT_SEC` (900 s).
- Outcome was something other than `BACKEND_ERROR` (any of `OUTCOME_OK`, `OUTCOME_DENIED_SECURITY`, `OUTCOME_NONE_*` is fine — this phase is about transport correctness, not grading).
- No unhandled exceptions in stderr.

If any criterion fails, open a Phase 1 debug loop before moving on — the salvage path may need an additional shape. Log the failing content snippet into the plan's Smoke log so Phase 1 can re-open with a concrete reproducer.

- [ ] **Step 2: No commit — gating only.**

---

## Phase 3: Full PROD benchmark on `gpt-oss-20b`

Spec §Testing.4 — `run-benchmark --runs 1`, leaderboard-visible.

### Task 3.1: Execute the full benchmark

**Files:**
- No code changes.

- [ ] **Step 1: Kick off the run in the background and tee the log**

Run:
```bash
AGENT_TOOLCALLING=1 nohup \
  python -m bitgn_contest_agent.cli run-benchmark --runs 1 \
    > logs/plan-b-local-run-$(date +%Y%m%d-%H%M).log 2>&1 &
echo $! > .plan-b-local.pid
```
Expected: the PID file is written, the log file grows.

- [ ] **Step 2: Monitor progress (foreground-safe polling)**

Run (periodically): `tail -n 20 logs/plan-b-local-run-*.log | head -40`
Expected: steady per-task lines, no traceback spam. 104 tasks × ~40 steps × local model — plan on multi-hour wall time (spec §Risks, line 118).

- [ ] **Step 3: Wait for completion**

Run: `wait $(cat .plan-b-local.pid) && echo DONE`
Expected: `DONE`. If the process already exited, the `wait` returns immediately with the stored exit code.

- [ ] **Step 4: Pull the summary**

Run: `python -m scripts.bench_summary --latest` (or the project's equivalent summarizer — check `scripts/` for the actual entry).
Expected: a JSON/markdown summary with per-task outcomes and aggregates.

- [ ] **Step 5: Commit the run log and summary under `runs/`**

The project convention (inferred from the `f97ac09 bench: PROD runs from Apr 13-14` commit) is to version PROD benchmark artifacts. Copy or rename the log and summary into whatever `runs/` / `benchmarks/runs/` directory already exists, mirroring the most recent prior commit's layout. Then:

```bash
git add <the-new-run-artifacts>
git commit -m "bench(plan-b-local): gpt-oss-20b --runs 1 — P/N/104"
```
Where `P` is the pass count and `N` is completions-attempted.

### Task 3.2: Write a closeout note

**Files:**
- Modify: `docs/superpowers/plans/2026-04-15-toolcalling-local-model-plan.md` — append a `## Closeout` section

- [ ] **Step 1: Summarize the outcome**

Append to the plan file, in 3–6 bullets:
- Pass rate on `gpt-oss-20b` vs. the spec's 11/11 `BACKEND_ERROR` baseline.
- Salvage-path hit count (grep the trace for the salvage-wire log line if one exists; otherwise note absence as a follow-up).
- Time-to-completion wall clock.
- Any failure modes that surfaced that Phase 1 did not anticipate.
- Next-step recommendations (e.g., a third salvage shape, a prompt tweak, a model switch).

- [ ] **Step 2: Commit the closeout**

```bash
git add docs/superpowers/plans/2026-04-15-toolcalling-local-model-plan.md
git commit -m "docs(plan-b-local): closeout — PROD run on gpt-oss-20b"
```

### Task 3.3: Hand-off, no merge

- [ ] **Step 1: Push the branch for review**

Run: `git push -u origin local-toolcalling-lfm2`
Expected: remote branch is created or updated.

- [ ] **Step 2: Do NOT merge to main**

Per explicit user instruction on 2026-04-15: the `local-toolcalling-lfm2` branch stays unmerged. A PR may be opened for review, but integration is deferred to a future, separate decision.

- [ ] **Step 3: No commit — handoff only.**

---

## PROD Run Closeout — `run-22JdoW4LSzNYdohQZDoUJygbP`

**Date:** 2026-04-17  
**Model:** gpt-oss-20b via LM Studio (local)  
**Duration:** ~49 hours (2948 min), 4.6M input tokens  
**Config:** `MAX_PARALLEL_TASKS=6, MAX_INFLIGHT_LLM=6, TASK_TIMEOUT_SEC=900`

### Results

| Metric | This run | Previous run | Delta |
|--------|----------|-------------|-------|
| Pass rate | **35/104 (33.7%)** | 22/104 (21.2%) | **+13 net** |
| OUTCOME_OK (correct) | 12 | 6 | +6 |
| OUTCOME_OK (wrong) | 40 | 19 | +21 |
| DENIED_SECURITY (correct) | 12 | 8 | +4 |
| ERR_INTERNAL | 9 | 7 | +2 |
| Timeouts | 9 | — | — |
| Salvage misses | 14 | N/A | — |
| Classifier JSON failures | 93 (across 51 tasks) | N/A | — |

### Failure Breakdown (69 failed tasks)

| Failure mode | Count | Share |
|---|---|---|
| Wrong answer (OUTCOME_OK but eval fails) | **40** | **58%** |
| Gave up (OUTCOME_NONE_CLARIFICATION) | 14 | 20% |
| Timeout/crash (OUTCOME_ERR_INTERNAL) | 9 | 13% |
| False deny (OUTCOME_DENIED_SECURITY) | 3 | 4% |
| Refused (OUTCOME_NONE_UNSUPPORTED) | 3 | 4% |

### Key Observations

1. **Wrong answers dominate.** 40 tasks report OUTCOME_OK but fail eval — the model "thinks" it answered correctly but the data is wrong. 16/40 answered in ≤3 steps after doing 100+ PCM prepass ops. The model doesn't verify its answer against the data.
2. **Classifier can't produce JSON.** 93 JSON-parse failures across 51 tasks. The reactive classifier asks gpt-oss-20b for structured JSON via free-text chat completion, but the model emits unparseable output. Wastes 2-4 retries per failure.
3. **Routing works when it fires.** 41/104 tasks routed to skills. Pass rate is similar (34% routed vs 33% unrouted) — the bottleneck is answer quality, not routing.
4. **Document migration is 0/5.** The model can't execute complex multi-step file operations.
5. **Salvage misses are bare values.** 6/14 salvage misses are the model emitting a raw answer (`"780"`, `"Tobias"`, `"08/16/2019"`) instead of a tool call.

### Per-Skill Pass Rates

| Skill | Pass/Total | Rate |
|---|---|---|
| entity-message-lookup | 3/4 | 75% |
| bill-query | 3/7 | 43% |
| finance-lookup | 5/14 | 36% |
| UNKNOWN (unrouted) | 21/63 | 33% |
| project-involvement | 3/11 | 27% |
| document-migration | 0/5 | 0% |

---

## Phase 4: Answer quality improvements

Goal: move from 35/104 (33.7%) toward 50+/104 by fixing the three dominant failure modes — wrong answers, classifier failures, and bare-value salvage misses.

### Task 4.1: Salvage bare-value terminal replies (P1 — quick win)

**Files:**
- Modify: `src/bitgn_contest_agent/backend/openai_toolcalling.py` — extend `_try_salvage_from_content`
- Modify: `tests/test_backend_openai_toolcalling.py`

**Background:** 6/14 salvage misses are the model emitting a short answer (`"780"`, `"Tobias"`, `"08/16/2019"`) with no JSON. The salvage path returns `None` because `_extract_first_json_object` finds no braces. We should synthesize a `report_completion(message=<content>, outcome=OUTCOME_OK)` for these.

- [ ] **Step 1: Write failing tests**

```python
def test_salvage_bare_numeric_value() -> None:
    """Model emits '780' as a raw answer — synthesize report_completion."""
    ns = _try_salvage_from_content("780")
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert "780" in ns.function.message

def test_salvage_bare_name_value() -> None:
    ns = _try_salvage_from_content("Tobias")
    assert ns is not None
    assert ns.function.tool == "report_completion"

def test_salvage_bare_date_value() -> None:
    ns = _try_salvage_from_content("08/16/2019")
    assert ns is not None
    assert ns.function.tool == "report_completion"

def test_salvage_does_not_fire_on_long_prose() -> None:
    """Long prose is not a bare value — let critique handle it."""
    ns = _try_salvage_from_content("I'm not sure what to do here. " * 20)
    assert ns is None
```

- [ ] **Step 2: Run to confirm they fail**

- [ ] **Step 3: Add bare-value detection before JSON extraction**

In `_try_salvage_from_content`, before the `_extract_first_json_object` call:

```python
# Bare-value reply: short content with no JSON braces.
# Local models sometimes emit a raw answer ("780", "Tobias") instead of
# a tool call. Synthesize report_completion so the answer isn't lost.
stripped = content.strip()
if stripped and "{" not in stripped and len(stripped) < 200:
    try:
        return _build_next_step("report_completion", {
            "message": stripped,
            "outcome": "OUTCOME_OK",
            "outcome_justification": "bare-value salvage",
            "rulebook_notes": "—",
        })
    except ValidationError:
        pass  # fall through to JSON extraction
```

- [ ] **Step 4: Run tests — verify pass**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(toolcalling): salvage bare-value replies as report_completion"
```

### Task 4.2: Wire `call_structured` into reactive classifier (P2)

**Files:**
- Modify: `src/bitgn_contest_agent/classifier.py` — add `classify_structured()` accepting a backend
- Modify: `src/bitgn_contest_agent/reactive_router.py` — use `classify_structured` when backend available
- Modify: `src/bitgn_contest_agent/agent.py` — pass backend to reactive router
- Modify: `tests/test_classifier.py` (if exists) or create
- Modify: `tests/test_reactive_router.py` (if exists) or create

**Background:** The classifier (`classifier.py:67-76`) builds its own OpenAI client and expects free-text JSON. gpt-oss-20b fails to produce valid JSON 93 times across 51 tasks. The `call_structured` method (`openai_toolcalling.py:846-883`) uses `response_format=<schema>` which forces valid JSON output from LM Studio. Wire it in.

- [ ] **Step 1: Define `ClassificationResult` Pydantic schema**

```python
class ClassificationResult(BaseModel):
    category: str
    confidence: float = 1.0
    query: str = ""
```

- [ ] **Step 2: Add `classify_structured(backend, system, user)` alongside existing `classify()`**

Keep the existing `classify()` as fallback for non-toolcalling backends.

- [ ] **Step 3: In reactive_router, prefer `classify_structured` when backend is available**

- [ ] **Step 4: Thread backend from agent.py through to reactive_router**

- [ ] **Step 5: Tests — mock backend.call_structured, verify JSON failures drop to zero**

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(classifier): use call_structured for reactive classification"
```

### Task 4.3: Add minimum-exploration rule to step validator (P0)

**Files:**
- Modify: `src/bitgn_contest_agent/validator.py` — add rule in `_check_rules()`
- Modify: `tests/test_validator.py`

**Background:** 16/40 wrong-answer tasks completed in ≤3 steps. The model does a 100+ op prepass, then immediately reports without verifying. The step validator already has `step_idx` (line 86) and `max_steps` (line 230) but enforces no floor. Add a rule: block `report_completion` if `step_idx < min_exploration_steps` (default 3) unless outcome is DENIED_SECURITY or ERR_INTERNAL.

- [ ] **Step 1: Write failing tests**

Test that report_completion at step 1 with outcome OK is rejected.
Test that report_completion at step 1 with outcome DENIED_SECURITY is allowed.
Test that report_completion at step 4 with outcome OK is allowed.

- [ ] **Step 2: Add rule `R0_MIN_EXPLORE` in `_check_rules()`**

```python
# R0: Minimum exploration — don't accept terminal before step N
# unless outcome is DENIED_SECURITY (immediate refusal is valid).
MIN_EXPLORE_STEPS = 3
if (step_idx < MIN_EXPLORE_STEPS
        and tool_name == "report_completion"
        and outcome not in ("OUTCOME_DENIED_SECURITY", "OUTCOME_ERR_INTERNAL")):
    return Verdict(
        accept=False,
        rule="R0_MIN_EXPLORE",
        reason=f"Too early to report — explore at least {MIN_EXPLORE_STEPS} steps first",
    )
```

- [ ] **Step 3: Run tests — verify pass**

- [ ] **Step 4: Run full suite for regressions**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(validator): R0 minimum-exploration rule blocks premature terminal"
```

### Task 4.4: Finance aggregation guidance in skill prompt (P3)

**Files:**
- Modify: `src/bitgn_contest_agent/skills/finance_lookup.md`
- No test changes (prompt-only)

**Background:** 9/14 finance-lookup tasks fail with wrong numbers. The model reads one invoice and answers, instead of searching for ALL matching invoices and summing. Add explicit guidance to the skill prompt.

- [ ] **Step 1: Add aggregation instructions to skill body**

Append to the finance-lookup skill:

```
## Revenue/Payment Queries
When asked "how much money" or "total amount" for a service line or vendor:
1. Use `search` to find ALL invoices matching the query term
2. Read EVERY matching invoice to extract amounts
3. Sum all amounts before reporting
4. Include the count of invoices found in your answer justification
Do NOT answer from a single invoice — always aggregate.
```

- [ ] **Step 2: Commit**

```bash
git commit -m "feat(skills): finance-lookup aggregation guidance for sum queries"
```

### Task 4.5: Commit uncommitted changes (housekeeping)

**Files:**
- `src/bitgn_contest_agent/backend/openai_toolcalling.py` — empty-string placeholder + `call_structured`
- `src/bitgn_contest_agent/cli.py` — `--resume` reads `.last_run_id`

- [ ] **Step 1: Run tests to confirm current uncommitted code is green**

- [ ] **Step 2: Commit the two features separately**

```bash
git add src/bitgn_contest_agent/backend/openai_toolcalling.py
git commit -m "feat(toolcalling): empty-string placeholder in salvage + call_structured method"

git add src/bitgn_contest_agent/cli.py
git commit -m "feat(cli): --resume reads .last_run_id when no value given"
```

---

## Phase 5: Re-run PROD benchmark

### Task 5.1: Execute with reduced parallelism

- [ ] **Step 1: Update `.env`**

```
MAX_PARALLEL_TASKS=4
MAX_INFLIGHT_LLM=4
```

- [ ] **Step 2: Run**

```bash
AGENT_TOOLCALLING=1 nohup \
  python -m bitgn_contest_agent.cli run-benchmark --runs 1 \
    > logs/plan-b-local-run-$(date +%Y%m%d-%H%M).log 2>&1 &
```

- [ ] **Step 3: Compare with run-22JdoW4LSzNYdohQZDoUJygbP baseline**

Target: 50+/104 (48%+), up from 35/104 (33.7%).

---

## What this plan does NOT do

- Does not change `openai_compat.OpenAIChatBackend` or the frontier path.
- Does not restructure the system prompt (spec §Out of scope, line 124).
- Does not attempt lfm2 end-to-end — gpt-oss-20b is the designated target.
- Does not merge to main, does not tag a release.
