# Bench #1 Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address two concrete failure modes from bench #1 (100/104 on commit `8880fc8`) via minimal orchestration-level changes — config default alignment + finance-lookup skill guardrail. Target bench #2 server score ≥ 100 (no regression), directional ≥ 101.

**Architecture:** Two independent, low-risk edits. Fix 1 is a one-line config change that completes a partially-landed commit from two days ago. Fix 2 inserts a single paragraph into an existing skill file.

**Tech Stack:** Python 3.12, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-15-bench1-improvements-design.md`

---

## Task 1: Align load_from_env default for TASK_TIMEOUT_SEC with dataclass default

**Files:**
- Modify: `src/bitgn_contest_agent/config.py:82`
- Modify: `tests/test_config.py` (if the test asserts on the env-loaded value)

- [ ] **Step 1: Confirm current mismatch**

Run: `grep -n "task_timeout_sec\|TASK_TIMEOUT_SEC" src/bitgn_contest_agent/config.py`
Expected output will show:
- line ~33: `task_timeout_sec: int = 600`
- line ~82: `task_timeout_sec=_int_env("TASK_TIMEOUT_SEC", 300),`

If the numbers don't match this description, STOP and investigate before proceeding — the repo has diverged from the plan's assumption.

- [ ] **Step 2: Check for a failing test or add a regression test**

Run: `grep -n "task_timeout_sec\|TASK_TIMEOUT_SEC" tests/test_config.py`

Case A — no existing test asserts on the load_from_env default for task_timeout_sec:
Append to `tests/test_config.py`:

```python
def test_load_from_env_task_timeout_default_matches_dataclass() -> None:
    """Regression: commit 87e9a4d bumped the dataclass default 300->600
    but missed this env loader default. The resulting effective timeout
    was 300s, not 600s as intended. Both defaults must stay in sync.
    """
    import os
    from bitgn_contest_agent.config import AgentConfig, load_from_env

    required = {
        "BITGN_API_KEY": "x",
        "CLIPROXY_BASE_URL": "http://localhost",
        "CLIPROXY_API_KEY": "x",
    }
    saved = {k: os.environ.get(k) for k in list(required) + ["TASK_TIMEOUT_SEC"]}
    try:
        for k, v in required.items():
            os.environ[k] = v
        os.environ.pop("TASK_TIMEOUT_SEC", None)
        cfg = load_from_env()
        dataclass_default = AgentConfig.__dataclass_fields__[
            "task_timeout_sec"
        ].default
        assert cfg.task_timeout_sec == dataclass_default, (
            f"env loader default {cfg.task_timeout_sec} != "
            f"dataclass default {dataclass_default}"
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
```

Case B — an existing test already asserts on 300: update its expected value to 600 instead, AND add the assertion above (so future drift is caught).

- [ ] **Step 3: Run the new test — verify it fails (or the existing one fails after updating)**

Run: `uv run pytest tests/test_config.py -v -k timeout`
Expected: FAIL (because loader still defaults to 300).

- [ ] **Step 4: Implement the fix**

Edit `src/bitgn_contest_agent/config.py`. Replace:

```python
        task_timeout_sec=_int_env("TASK_TIMEOUT_SEC", 300),
```

with:

```python
        task_timeout_sec=_int_env("TASK_TIMEOUT_SEC", 600),
```

- [ ] **Step 5: Verify tests pass**

Run: `uv run pytest tests/test_config.py -v -k timeout`
Expected: PASS.

Run: `uv run pytest -q`
Expected: all green, no regressions.

- [ ] **Step 6: Commit (Lore protocol)**

Bump VERSION to `0.1.11`. Commit message:

```
v0.1.11: align load_from_env task_timeout default with dataclass default

Commit 87e9a4d bumped the dataclass default 300->600s but left the
load_from_env default at 300. Since load_from_env is the only entry
point used by the CLI, no running benchmark has ever had the intended
600s timeout. Bench #1 task t092 (nora_migration, 5-file document
migration) terminated by cancel at step 19 with 4/5 mutations done;
consistent with a ~300s cutoff during a doc-migration skill run.

Constraint: matches the earlier author's stated intent. No new
  behaviour — just completes a partial landing.
Rejected: per-skill timeout multipliers | too much new machinery for
  one known-failing task.
Confidence: high
Scope-risk: narrow (single int literal + regression test)
Directive: if the dataclass default ever changes again, this test
  will catch the drift.
Tested: regression test in tests/test_config.py; full suite green.
Not-tested: end-to-end t092 rerun deferred to bench #2.
```

---

## Task 2: Finance-lookup — vendor-mismatch disqualifying guardrail

**Files:**
- Modify: `src/bitgn_contest_agent/skills/finance_lookup.md`

This task edits only a skill markdown body. The skill loader parses
frontmatter + body, and the body is injected as a user message when the
router matches. No source-code changes, no tests against this file
(skill bodies are content, not logic).

- [ ] **Step 1: Read current Step 3 section to anchor the edit location**

Run: `uv run python -c "
from pathlib import Path
p = Path('src/bitgn_contest_agent/skills/finance_lookup.md')
lines = p.read_text().splitlines()
start = next(i for i, l in enumerate(lines) if l.startswith('## Step 3'))
for i, l in enumerate(lines[start:start+20], start=start):
    print(f'{i+1}: {l}')
"`

Expected: prints the Step 3 (Cross-Validate and Select) section. Use
the printed line numbers to target the edit.

- [ ] **Step 2: Add the vendor-mismatch guardrail**

In `src/bitgn_contest_agent/skills/finance_lookup.md`, find the
bullet that starts with "- **Primary match criteria: vendor name + item/line-item description.**" (it is the first bullet under `## Step 3`). Keep that bullet as-is. Immediately AFTER that bullet and BEFORE the "Date is contextual" bullet, insert this new bullet:

```markdown
- **Vendor mismatch is disqualifying.** If none of the candidate records' vendor fields match the vendor named in the task, do NOT answer with a number from any of them. Widen the search (Step 2.2 partial match, Step 2.3 different artifact, Step 2.4 broader listing) before falling back to `OUTCOME_NONE_CLARIFICATION`. A numeric answer pulled from a different vendor's invoice is worse than asking for clarification.
```

After the edit, the first three bullets of Step 3 must read, in order:
1. "Read each candidate fully"
2. "Primary match criteria: vendor name + item/line-item description..."
3. "Vendor mismatch is disqualifying..." (the new bullet)
4. "Date is contextual, NOT a strict filter..."

- [ ] **Step 3: Verify skill still loads**

Run: `uv run python -c "
from bitgn_contest_agent.skill_loader import load_all_skills
skills = load_all_skills()
fl = [s for s in skills if s.name == 'finance-lookup'][0]
assert 'Vendor mismatch is disqualifying' in fl.body, fl.body[:500]
print('OK:', fl.name, 'body chars:', len(fl.body))
"`

Expected: prints `OK: finance-lookup body chars: <N>` with N larger than before the edit.

- [ ] **Step 4: Run full test suite — no regression**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 5: Commit (Lore protocol)**

VERSION stays at `0.1.11` (skill body change, not a behaviour bump).

Commit message:

```
v0.1.11: finance-lookup vendor-mismatch guardrail

Bench #1 t030: task named 'Filamenthütte Wien / PLA spool mixed colors',
agent read 'toy_forge_pla_bundle' + 'black_library_terrain_spool' (no
vendor match), answered 72 (the closest-looking filename's eur_000072
id) instead of 24. Finance-lookup already listed 'vendor + item' as
primary match criteria but did not forbid answering from a vendor
mismatch.

New bullet in Step 3 makes vendor mismatch disqualifying: the agent
must widen the search or surrender with OUTCOME_NONE_CLARIFICATION
rather than pull a number from the wrong vendor's record.

Constraint: skill body only — no code, no tests on content
Rejected: post-hoc validator rule | requires validator to know task
  semantics; skill guidance is the right layer.
Confidence: medium (addresses the specific failure but vendor aliasing
  in the real corpus may trigger the guardrail too aggressively —
  partial-match fallback in Step 2.2 should absorb this).
Scope-risk: narrow (single skill body, additive text)
Directive: if bench #2 shows new surrenders on finance tasks that
  bench #1 passed, the guardrail is too strict — soften to 'no
  reasonable match' rather than 'byte-equal'.
Tested: skill_loader round-trip; full pytest green.
Not-tested: end-to-end t030 rerun deferred to bench #2.
```

---

## Self-Review Checklist

After both tasks complete, verify:

- [ ] `uv run pytest -q` — all green
- [ ] `git log --oneline -5` shows two new commits (v0.1.11 x2) on `feat/r4-validator-correctness`
- [ ] `cat VERSION` shows `0.1.11`
- [ ] Diff reviewable: one config line + one test function, plus one skill bullet

## Follow-up (NOT in this plan)

1. Dedicated inbox-position-16 investigation (t041, t066 cluster — always fails)
2. Flip `submit_anyway` → `force-reject` after bench #2 confirms continued zero R1 REJECTs
3. Per-intent skill bodies (e.g. `receipt_total_relative` as its own skill, `nora_migration` with its own step budget)
