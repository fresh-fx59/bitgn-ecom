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
    # Normalise with lstrip("/") because the live adapter strips leading
    # slashes from tool_result.refs before seeding session.seen_refs,
    # while the raw read path (our proxy) keeps the slash as-written.
    # Without this the proxy reports fake FPs that the real validator
    # never produces.
    def _norm(p: str) -> str:
        return p.lstrip("/").lower()

    seen_lower = {_norm(r) for r in seen_refs}
    absent_lower = {_norm(r) for r in verified_absent}
    new_rejects: list[str] = []
    for ref in grounding_refs_claimed:
        rl = _norm(ref)
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
