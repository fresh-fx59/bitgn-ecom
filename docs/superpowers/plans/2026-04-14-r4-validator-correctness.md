# R4 Validator Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two validator false-positive modes in the R1 grounding-ref rule so that eventually enabling `force-reject` doesn't regress the benchmark (measured: current wiring would regress from 95 → 77 passes because 18 of 20 terminal REJECTs fire against already-passing tasks).

**Architecture:** The validator's R1 rule in `src/bitgn_contest_agent/validator.py:124-126` does a case-sensitive exact match between each `grounding_ref` the agent claims and `session.seen_refs`, which only contains paths from *successful* reads. Two fixes: (a) compare refs case-insensitively, and (b) also accept refs the agent *attempted* to read where the adapter returned "file not found" (legitimate negative-evidence grounding). Session gains two new sets (`attempted_reads`, `verified_absent`) populated in the agent loop alongside `seen_refs`.

**Tech Stack:** Python 3.12, pydantic v2, pytest. No new dependencies.

**Out of scope:** Regex tier-1 routing stays untouched (Proof 4 showed 4/4 regex-routed tasks pass, 0 evidence of mis-routing). The actual `force-reject` switch is deferred to a follow-up plan once this one lands and FP rate is re-measured on fresh data.

---

## File Structure

- **Modify `src/bitgn_contest_agent/session.py`** — add two `set[str]` fields alongside `seen_refs`
- **Modify `src/bitgn_contest_agent/validator.py:115-162`** — rewrite R1 check to use case-insensitive match + accept verified-absent paths
- **Modify `src/bitgn_contest_agent/agent.py:420-440`** — record every `read` attempt into `attempted_reads`, and into `verified_absent` if the error is file-not-found
- **Modify `tests/test_validator.py`** — add regression tests for the two new behaviours (existing `test_r1_*` tests still pass with no semantic change to true-positive cases)
- **Create `scripts/measure_r4_fp.py`** — one-shot re-measurement tool: counts terminal-REJECT TP/FP over a trace dir using the new rules

All changes are additive to public types (new `Session` fields default to empty sets; existing callers unaffected).

---

## Task 1: Session tracks attempted and verified-absent reads

**Files:**
- Modify: `src/bitgn_contest_agent/session.py:17-28`
- Test: `tests/test_session.py` (create if absent; otherwise add to existing)

- [ ] **Step 1: Check if tests/test_session.py exists**

Run: `ls tests/test_session.py 2>&1 || echo MISSING`
Expected: either path prints, or `MISSING`.

- [ ] **Step 2: Write the failing test**

If `tests/test_session.py` doesn't exist, create it with this content. If it does exist, append the two tests below.

```python
"""Session state unit tests."""
from __future__ import annotations

from bitgn_contest_agent.session import Session


def test_session_tracks_attempted_reads() -> None:
    s = Session()
    assert s.attempted_reads == set()
    s.attempted_reads.add("AGENTS.md")
    s.attempted_reads.add("10_entities/cast/renate.md")
    assert "AGENTS.md" in s.attempted_reads
    assert len(s.attempted_reads) == 2


def test_session_tracks_verified_absent() -> None:
    s = Session()
    assert s.verified_absent == set()
    s.verified_absent.add("00_inbox/556_next-task.md")
    assert "00_inbox/556_next-task.md" in s.verified_absent


def test_session_new_fields_are_independent_of_seen_refs() -> None:
    s = Session()
    s.seen_refs.add("AGENTS.md")
    assert s.attempted_reads == set()
    assert s.verified_absent == set()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_session.py -v`
Expected: FAIL — `AttributeError: 'Session' object has no attribute 'attempted_reads'`

- [ ] **Step 4: Implement the change**

Edit `src/bitgn_contest_agent/session.py`, adding two new fields immediately after `seen_refs`:

```python
@dataclass(slots=True)
class Session:
    seen_refs: set[str] = field(default_factory=set)
    # Paths the agent attempted to read (regardless of success).
    # Used by R1 to distinguish "never tried" (REJECT) from
    # "tried but got not-found" (negative evidence, ACCEPT).
    attempted_reads: set[str] = field(default_factory=set)
    # Subset of attempted_reads where the adapter returned a
    # not-found error. An agent may legitimately cite such a
    # path in grounding_refs as negative evidence.
    verified_absent: set[str] = field(default_factory=set)
    rulebook_loaded: bool = False
    identity_loaded: bool = False
    step: int = 0
    recent_calls: Deque[Tuple[str, ...]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_WINDOW)
    )
    nudges_emitted: int = 0
    mutations: List[Tuple[str, str]] = field(default_factory=list)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_session.py -v`
Expected: PASS (all 3 tests green).

- [ ] **Step 6: Run the full suite to confirm no regression**

Run: `uv run pytest -q`
Expected: same test count as before + 3 new passing tests, no failures.

- [ ] **Step 7: Commit**

```bash
git add src/bitgn_contest_agent/session.py tests/test_session.py
git commit -m "feat(session): track attempted_reads and verified_absent

Prep for R1 validator correctness fix: distinguish 'never tried'
from 'tried and got not-found'. New fields default to empty sets
and are not read anywhere yet."
```

---

## Task 2: Agent loop populates the new session fields

**Files:**
- Modify: `src/bitgn_contest_agent/agent.py:420-440` (the tool-dispatch block)
- Test: `tests/test_agent_read_tracking.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_read_tracking.py`:

```python
"""Agent populates Session.attempted_reads / verified_absent on every read."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from bitgn_contest_agent.session import Session


@dataclass
class _FakeResult:
    ok: bool
    refs: Tuple[str, ...] = ()
    error: str | None = None
    error_code: str | None = None
    content: str | None = None
    truncated: bool = False
    wall_ms: int = 0


def _record_read(session: Session, path: str, result: _FakeResult) -> None:
    """Mirror of the logic the agent loop runs on every read dispatch."""
    from bitgn_contest_agent.agent import _record_read_attempt  # noqa: WPS433
    _record_read_attempt(session, path, result)


def test_successful_read_adds_to_seen_refs_and_attempted() -> None:
    session = Session()
    result = _FakeResult(ok=True, refs=("AGENTS.md",))
    _record_read(session, "AGENTS.md", result)
    assert "AGENTS.md" in session.seen_refs
    assert "AGENTS.md" in session.attempted_reads
    assert "AGENTS.md" not in session.verified_absent


def test_not_found_read_adds_to_attempted_and_verified_absent() -> None:
    session = Session()
    result = _FakeResult(
        ok=False, error="file not found", error_code="UNKNOWN"
    )
    _record_read(session, "00_inbox/absent.md", result)
    assert "00_inbox/absent.md" in session.attempted_reads
    assert "00_inbox/absent.md" in session.verified_absent
    assert "00_inbox/absent.md" not in session.seen_refs


def test_other_read_error_adds_only_to_attempted() -> None:
    session = Session()
    result = _FakeResult(
        ok=False, error="permission denied", error_code="UNKNOWN"
    )
    _record_read(session, "private/x.md", result)
    assert "private/x.md" in session.attempted_reads
    assert "private/x.md" not in session.verified_absent
    assert "private/x.md" not in session.seen_refs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_read_tracking.py -v`
Expected: FAIL — `ImportError: cannot import name '_record_read_attempt' from 'bitgn_contest_agent.agent'`.

- [ ] **Step 3: Extract the read-tracking helper in agent.py**

Open `src/bitgn_contest_agent/agent.py` and add this helper near the top of the module (after the imports, before the first class definition):

```python
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
    # Adapter surfaces ENOENT as a plain-text "file not found" message
    # (see src/bitgn_contest_agent/adapter/pcm.py). We match substrings
    # rather than error_code because pcm currently labels everything
    # UNKNOWN; tightening the adapter classification is a separate
    # concern.
    if "file not found" in err or "no such file" in err:
        session.verified_absent.add(path)
```

Both `Session` (line 43) and `ToolResult` (line 29) are already imported at the top of `agent.py` — no new imports needed.

- [ ] **Step 4: Rewire the agent loop to call the helper**

In `src/bitgn_contest_agent/agent.py`, find the block currently at lines 420-440:

```python
            tool_result = self._adapter.dispatch(fn)
            if tool_result.ok:
                for ref in tool_result.refs:
                    session.seen_refs.add(ref)
                # Track successful mutations for terminal integrity check.
                tool_name = getattr(fn, "tool", "")
                if tool_name in ("write", "delete", "move"):
                    mut_path = getattr(fn, "path", "") or getattr(fn, "from_name", "")
                    session.mutations.append((tool_name, mut_path))
                # Cache file content on successful read for body validation.
                if getattr(fn, "tool", "") == "read":
                    read_path = getattr(fn, "path", "")
                    if read_path and tool_result.content:
                        try:
                            parsed = _json.loads(tool_result.content)
                            file_text = parsed.get("content", "")
                        except (ValueError, AttributeError):
                            file_text = ""
                        if file_text:
                            read_cache[read_path] = file_text
```

Replace it with:

```python
            tool_result = self._adapter.dispatch(fn)
            tool_name = getattr(fn, "tool", "")
            # Track every read attempt (success or failure) for R1 validator.
            if tool_name == "read":
                _record_read_attempt(
                    session, getattr(fn, "path", ""), tool_result
                )
            if tool_result.ok:
                for ref in tool_result.refs:
                    session.seen_refs.add(ref)
                # Track successful mutations for terminal integrity check.
                if tool_name in ("write", "delete", "move"):
                    mut_path = getattr(fn, "path", "") or getattr(fn, "from_name", "")
                    session.mutations.append((tool_name, mut_path))
                # Cache file content on successful read for body validation.
                if tool_name == "read":
                    read_path = getattr(fn, "path", "")
                    if read_path and tool_result.content:
                        try:
                            parsed = _json.loads(tool_result.content)
                            file_text = parsed.get("content", "")
                        except (ValueError, AttributeError):
                            file_text = ""
                        if file_text:
                            read_cache[read_path] = file_text
```

Semantic change is minimal: only move `tool_name` extraction up one line and add one `_record_read_attempt` call before the `if tool_result.ok` branch.

- [ ] **Step 5: Run the new test — verify it passes**

Run: `uv run pytest tests/test_agent_read_tracking.py -v`
Expected: PASS (all 3 tests green).

- [ ] **Step 6: Run the full suite — verify no regression**

Run: `uv run pytest -q`
Expected: previous tests still pass, 3 new passing tests added.

- [ ] **Step 7: Commit**

```bash
git add src/bitgn_contest_agent/agent.py tests/test_agent_read_tracking.py
git commit -m "feat(agent): record every read into attempted_reads + verified_absent

Every read dispatch now populates Session.attempted_reads; those where
the adapter returned a file-not-found error additionally populate
verified_absent. Consumed by the R1 validator in the next commit.

Matches 'file not found' / 'no such file' on the error string; pcm
currently labels everything UNKNOWN so substring match is the cleanest
boundary."
```

---

## Task 3: R1 validator — case-insensitive match + verified-absent acceptance

**Files:**
- Modify: `src/bitgn_contest_agent/validator.py:115-162`
- Test: `tests/test_validator.py` (append regression tests)

- [ ] **Step 1: Add the failing regression tests**

Append to `tests/test_validator.py`:

```python
# === R1 correctness fixes: case-insensitive match + verified-absent ===

def test_r1_is_case_insensitive_on_filename() -> None:
    """AGENTS.MD in grounding_refs must match AGENTS.md in seen_refs.

    Regression: 18 of 20 terminal REJECTs on trace
    logs/20260414_184041 were this exact false positive.
    """
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = _mk_terminal("OUTCOME_OK", ["AGENTS.MD"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok, verdict.reasons


def test_r1_is_case_insensitive_on_nested_path() -> None:
    session = Session()
    session.seen_refs.add("10_entities/cast/Renate.md")
    step = _mk_terminal("OUTCOME_OK", ["10_entities/cast/renate.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok, verdict.reasons


def test_r1_accepts_verified_absent_as_negative_evidence() -> None:
    """Agent cites file-not-found result as grounding_ref. Legitimate."""
    session = Session()
    session.seen_refs.add("AGENTS.md")
    session.attempted_reads.add("00_inbox/556_next-task.md")
    session.verified_absent.add("00_inbox/556_next-task.md")
    step = _mk_terminal(
        "OUTCOME_OK", ["AGENTS.md", "00_inbox/556_next-task.md"],
    )
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert verdict.ok, verdict.reasons


def test_r1_rejects_ref_never_attempted_and_not_seen() -> None:
    """Baseline stays: pure fabrication still rejected."""
    session = Session()
    session.seen_refs.add("AGENTS.md")
    step = _mk_terminal("OUTCOME_OK", ["fabricated/never-touched.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert not verdict.ok
    assert any("grounding_ref" in r for r in verdict.reasons)


def test_r1_attempted_but_not_verified_absent_still_rejects() -> None:
    """Attempt without file-not-found (e.g. permission denied) is NOT grounding."""
    session = Session()
    session.seen_refs.add("AGENTS.md")
    session.attempted_reads.add("private/locked.md")
    # Note: NOT in verified_absent (different error, e.g. permission)
    step = _mk_terminal("OUTCOME_OK", ["AGENTS.md", "private/locked.md"])
    v = StepValidator()
    verdict = v.check_terminal(session, step)
    assert not verdict.ok
    assert any("grounding_ref" in r for r in verdict.reasons)
```

- [ ] **Step 2: Run the tests — expect failures**

Run: `uv run pytest tests/test_validator.py -v -k r1`
Expected:
- `test_r1_fires_when_grounding_ref_not_in_seen_refs` PASS (existing, baseline)
- `test_r1_passes_when_all_grounding_refs_were_seen` PASS (existing, baseline)
- `test_r1_is_case_insensitive_on_filename` FAIL
- `test_r1_is_case_insensitive_on_nested_path` FAIL
- `test_r1_accepts_verified_absent_as_negative_evidence` FAIL
- `test_r1_rejects_ref_never_attempted_and_not_seen` PASS (unchanged by rewrite)
- `test_r1_attempted_but_not_verified_absent_still_rejects` PASS (unchanged)

- [ ] **Step 3: Implement the R1 fix**

In `src/bitgn_contest_agent/validator.py`, replace lines 123-126 (the R1 block) with:

```python
        # R1 — grounding-refs reachability.
        # Case-insensitive match against seen_refs (successful reads) OR
        # verified_absent (reads where the adapter returned file-not-found,
        # a legitimate form of negative evidence).
        seen_lower = {r.lower() for r in session.seen_refs}
        absent_lower = {r.lower() for r in session.verified_absent}
        for ref in fn.grounding_refs:
            rl = ref.lower()
            if rl in seen_lower:
                continue
            if rl in absent_lower:
                continue
            reasons.append(f"grounding_ref {ref!r} never successfully read")
```

The rest of `check_terminal` (R2, R3, R4, verdict emission) is unchanged.

- [ ] **Step 4: Run the tests — verify they pass**

Run: `uv run pytest tests/test_validator.py -v -k r1`
Expected: all 7 tests PASS (2 existing + 5 new).

- [ ] **Step 5: Run the full validator test file — no regression elsewhere**

Run: `uv run pytest tests/test_validator.py -q`
Expected: all tests green, no failures.

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/bitgn_contest_agent/validator.py tests/test_validator.py
git commit -m "fix(validator): R1 case-insensitive match + verified-absent grounding

Two false-positive modes caused 18/20 terminal REJECTs to fire against
passing tasks on the 2026-04-14 trace, blocking any future enablement
of force-reject.

(a) Case-sensitivity: agent writes AGENTS.MD (uppercase, as referenced
    in skill bodies) but filesystem has AGENTS.md. Both now compare
    via .lower().

(b) Negative evidence: an agent that tried to read 00_inbox/X.md and
    got 'file not found' has grounds to claim the file doesn't exist;
    listing the path in grounding_refs is correct. Accept refs that
    are in verified_absent."
```

---

## Task 4: Re-measurement script — verify FP reduction on existing trace

**Files:**
- Create: `scripts/measure_r4_fp.py`

- [ ] **Step 1: Write the script**

Create `scripts/measure_r4_fp.py`:

```python
#!/usr/bin/env python3
"""Replay R1 validator against an existing trace directory.

Counts how many tasks would still get a TERMINAL REJECT if R1's
case-insensitive + verified-absent fixes were in effect, and splits
into TP (task actually failed) vs FP (task actually passed).

Usage:
    measure_r4_fp.py logs/20260414_184041

Output:
    Before fix: TP=2 FP=18 (total 20 REJECTs)
    After fix:  TP=X FP=Y (total Z REJECTs)
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

_here = Path(__file__).resolve()
_repo_root = _here.parent.parent
if str(_repo_root / "src") not in sys.path:
    sys.path.insert(0, str(_repo_root / "src"))

from bitgn_contest_agent.arch_constants import (  # noqa: E402
    ArchCategory,
    ArchResult,
)
from bitgn_contest_agent.trace_schema import (  # noqa: E402
    TraceArch,
    TraceMeta,
    TraceOutcome,
    TraceStep,
    load_jsonl,
)


def _replay_task(jsonl: Path) -> dict | None:
    """Build a digest of what the *new* R1 would say for a trace.

    Returns a dict with:
      task_id, score, original_rejects (list of reason strings),
      new_reject_reasons (list after applying the fix),
      attempted_reads (set), verified_absent (set),
      grounding_refs_claimed (list).
    """
    task_id = None
    score = None
    original_rejects: list[str] = []
    attempted_reads: set[str] = set()
    verified_absent: set[str] = set()
    grounding_refs_claimed: list[str] = []
    seen_refs: set[str] = set()

    for rec in load_jsonl(jsonl):
        if isinstance(rec, TraceMeta):
            task_id = rec.task_id
        elif isinstance(rec, TraceOutcome):
            score = rec.score
        elif isinstance(rec, TraceArch):
            if rec.category == ArchCategory.TERMINAL and rec.result == ArchResult.REJECT:
                original_rejects.extend(rec.reasons or [])
        elif isinstance(rec, TraceStep):
            ns = rec.next_step or {}
            fn = ns.get("function") or {}
            tool = fn.get("tool")
            if tool == "read":
                path = fn.get("path") or ""
                if path:
                    attempted_reads.add(path)
                    tr = rec.tool_result
                    if not tr.ok:
                        err = (tr.error or "").lower()
                        if "file not found" in err or "no such file" in err:
                            verified_absent.add(path)
                    else:
                        # approximate seen_refs via successful read paths
                        # (pcm actually adds the canonical refs but for
                        # re-measurement the read path is a good proxy)
                        seen_refs.add(path)
            if tool == "report_completion":
                grounding_refs_claimed = list(fn.get("grounding_refs") or [])

    if task_id is None:
        return None

    # Apply the new R1 rule.
    seen_lower = {r.lower() for r in seen_refs}
    absent_lower = {r.lower() for r in verified_absent}
    new_rejects: list[str] = []
    for ref in grounding_refs_claimed:
        rl = ref.lower()
        if rl in seen_lower or rl in absent_lower:
            continue
        new_rejects.append(f"grounding_ref {ref!r} never successfully read")

    return {
        "task_id": task_id,
        "score": score,
        "original_rejects": original_rejects,
        "new_reject_reasons": new_rejects,
        "passed": (score or 0) >= 0.999,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("trace_dir", type=Path)
    args = ap.parse_args(argv)

    before_tp = before_fp = 0
    after_tp = after_fp = 0
    rows: list[tuple[str, bool, int, int]] = []  # (tid, passed, orig, new)

    for jsonl in sorted(args.trace_dir.glob("t*.jsonl")):
        d = _replay_task(jsonl)
        if d is None:
            continue
        has_orig_r1 = any(
            "never successfully read" in r for r in d["original_rejects"]
        )
        has_new_r1 = bool(d["new_reject_reasons"])
        passed = d["passed"]
        if has_orig_r1:
            if passed:
                before_fp += 1
            else:
                before_tp += 1
        if has_new_r1:
            if passed:
                after_fp += 1
            else:
                after_tp += 1
        if has_orig_r1 or has_new_r1:
            rows.append((d["task_id"], passed, int(has_orig_r1), int(has_new_r1)))

    print(f"Trace dir: {args.trace_dir}")
    print(f"Before fix: TP={before_tp} FP={before_fp} total_rejects={before_tp+before_fp}")
    print(f"After fix:  TP={after_tp} FP={after_fp} total_rejects={after_tp+after_fp}")
    print(f"FP reduction: {before_fp - after_fp} ({before_fp} -> {after_fp})")
    print()
    print(f"{'task':<8} {'passed':>7} {'orig_R1':>8} {'new_R1':>7}")
    for tid, passed, orig, new in rows:
        flag = ""
        if orig and not new:
            flag = " FIXED"
        elif orig and new:
            flag = " still-rejected"
        elif not orig and new:
            flag = " NEW-REJECT"  # should not happen; new rule is strictly looser
        print(f"{tid:<8} {str(passed):>7} {orig:>8} {new:>7}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the script on the existing trace**

Run: `uv run python scripts/measure_r4_fp.py logs/20260414_184041`

Expected (numbers approximate — exact count depends on how the trace's `seen_refs` proxy compares to canonical refs):
```
Trace dir: logs/20260414_184041
Before fix: TP=2 FP=18 total_rejects=20
After fix:  TP=<=2 FP=<<18 total_rejects=<much smaller>
FP reduction: >= 15
```

Acceptance criterion: the `After fix` FP count must be strictly less than 18. If the FP drop is less than 10 the fix is not effective enough — go back to Task 3 and widen the match (e.g. also strip leading `/` or normalise separators).

- [ ] **Step 3: Commit the measurement tool**

```bash
git add scripts/measure_r4_fp.py
git commit -m "feat(scripts): measure_r4_fp.py replays R1 against trace dir

One-shot tool to re-measure TP/FP of the terminal R1 REJECT after the
case-insensitive + verified-absent fixes. Run against any past trace
to confirm the correctness improvement before flipping force-reject."
```

---

## Task 5: Record the Proof 4 decision (regex tier-1 stays)

**Files:**
- Modify: `docs/superpowers/plans/2026-04-14-toward-104-fixes.md` (the existing plan — mark the regex-removal task as deferred with rationale)

If `2026-04-14-toward-104-fixes.md` doesn't reference regex removal, create a short decision note at `docs/decisions/2026-04-14-keep-regex-tier1.md`.

- [ ] **Step 1: Check for existing regex-removal task**

Run: `grep -n -i "regex\|remove" docs/superpowers/plans/2026-04-14-toward-104-fixes.md | head -20`

- [ ] **Step 2a: If the existing plan has a regex-removal task**

Edit that task's heading to prepend `[DEFERRED]` and add a one-paragraph rationale:

```markdown
### [DEFERRED] Task N: Remove regex tier-1 routing

**Status:** Deferred 2026-04-14. Proof 4 on trace `logs/20260414_184041`
showed 4 of 104 tasks use tier-1 regex and all 4 passed (100%), vs 91%
on tier-2 LLM. No evidence of mis-routing; removing would increase
latency and cost with no accuracy benefit. Revisit only if a future
trace shows regex-routed tasks failing at above-baseline rate.
```

- [ ] **Step 2b: If no such task exists**

Create `docs/decisions/2026-04-14-keep-regex-tier1.md`:

```markdown
# Keep regex tier-1 routing — 2026-04-14

## Decision

Regex tier-1 in `src/bitgn_contest_agent/router.py:62-82` stays in place.
Do NOT remove in favour of tier-2-only routing.

## Evidence

Trace `logs/20260414_184041` (bench: `f9613a7_v019_archlog_p10i15_prod_runs1.json`):

| Source        | Tasks | Pass rate |
|---------------|-------|-----------|
| tier1_regex   | 4     | 100%      |
| tier2_llm     | 100   | 91%       |

Zero mis-routing observed. Removing tier-1 would replace 4 free
regex hits with 4 extra LLM calls and gain nothing.

## Revisit when

A future trace shows a regex-routed task failing because the regex
skill was wrong for the task (e.g. it grabbed a task that should have
matched a different category). Grep the `arch_report.py --category
SKILL_ROUTER --source tier1_regex` output against failing task ids.
```

- [ ] **Step 3: Commit**

```bash
git add -- docs/
git commit -m "docs: defer regex tier-1 removal — proof 4 shows no mis-routing"
```

---

## Self-Review Checklist

After all tasks are complete, verify:

- [ ] `uv run pytest -q` — all green
- [ ] `uv run python scripts/measure_r4_fp.py logs/20260414_184041` — FP count dropped to single digits
- [ ] `uv run python scripts/intent_report.py --failures-only --with-arch artifacts/bench/f9613a7_v019_archlog_p10i15_prod_runs1.json` still runs (exercises the new session fields indirectly via the trace replay)
- [ ] No reference to "force-reject" behaviour change in this plan — that's a deferred follow-up

## Follow-up (NOT in this plan)

1. Flip `submit_anyway` → `force-reject` after running a fresh benchmark and confirming TP/FP ratio is inverted (TP ≥ FP).
2. Fix the outbox duplicate-write bug (t021 / t071 / t072) — separate plan; validator already detects it via mutation-integrity REJECT so the fix is in the agent loop's retry logic, not the validator.
3. `finance_lookup.md` Step 5 for open-ended date ranges (t033, t100).
4. `project-involvement` alias fuzzy match (t026).
5. Step budget 30 → 60 for inbox-handling tasks (t097).
