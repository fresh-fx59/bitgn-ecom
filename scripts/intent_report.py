"""Intent-based PROD benchmark reporting.

Classifies tasks by stable intent (not task ID), aggregates pass rates per
intent group, compares against historical baselines, and shows failure details.

Task positions map stably to intents across PROD runs (101/104 confirmed).
Content varies (randomised entities, phrasing, language) but intent is fixed.

Usage:
    # Report on a single run
    uv run python scripts/intent_report.py \
        artifacts/bench/e875e9a_m3_p16i24_gpt54_20260412T194544Z_prod_runs1.json

    # Compare two runs
    uv run python scripts/intent_report.py \
        --baseline artifacts/bench/aab6675_m0gate_p16i24_gpt54_20260411T223213Z_prod_runs1.json \
        artifacts/bench/e875e9a_m3_p16i24_gpt54_20260412T194544Z_prod_runs1.json

    # Show only failing intents
    uv run python scripts/intent_report.py --failures-only <bench.json>

    # Filter to specific intents
    uv run python scripts/intent_report.py --intent receipt_total_relative --intent inbox_en <bench.json>

    # JSON output for downstream tooling
    uv run python scripts/intent_report.py --json <bench.json>
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# repo root on path so this script runs from the checkout
_here = Path(__file__).resolve()
_repo_root = _here.parent.parent
if str(_repo_root / "src") not in sys.path:
    sys.path.insert(0, str(_repo_root / "src"))

# ---------------------------------------------------------------------------
# Intent-to-position map (stable across 5+ PROD runs, 101/104 confirmed)
# ---------------------------------------------------------------------------
INTENT_POSITIONS: dict[str, list[str]] = {
    "birthday_lookup":        ["t000", "t025", "t050", "t075"],
    "project_start_date":     ["t001", "t026", "t051", "t076"],
    "last_message":           ["t002", "t027", "t052", "t077"],
    "project_involvement":    ["t003", "t028", "t053", "t078"],
    "project_count":          ["t004", "t029", "t054", "t079"],
    "receipt_total_relative": ["t005", "t030", "t055", "t080"],
    "receipt_delete":         ["t006", "t031", "t056", "t081"],
    "service_revenue_en":     ["t008", "t033", "t058", "t083"],
    "service_revenue_i18n":   ["t009", "t034", "t059", "t084"],
    "next_birthday":          ["t012", "t037", "t062", "t087"],
    "nora_migration":         ["t017", "t042", "t067", "t092"],
    "bill_query":             ["t024", "t049", "t074", "t099"],
    "finance_accounting":     ["t100", "t101", "t102", "t103"],
    "inbox_en": [
        "t007", "t011", "t014", "t015", "t016", "t018", "t019", "t020",
        "t021", "t022", "t023", "t032", "t036", "t039", "t040", "t041",
        "t043", "t044", "t045", "t046", "t047", "t048", "t057", "t061",
        "t064", "t065", "t066", "t068", "t069", "t070", "t071", "t072",
        "t073", "t082", "t086", "t089", "t090", "t091", "t093", "t094",
        "t095", "t096", "t097", "t098",
    ],
    "inbox_i18n": [
        "t010", "t013", "t035", "t038", "t060", "t063", "t085", "t088",
    ],
}

# Reverse map: task_id -> intent
POSITION_TO_INTENT: dict[str, str] = {}
for _intent, _positions in INTENT_POSITIONS.items():
    for _pos in _positions:
        POSITION_TO_INTENT[_pos] = _intent

# Historical 5-run baseline pass rates (from analysis of 5 scored PROD runs)
HISTORICAL_BASELINE: dict[str, float] = {
    "receipt_total_relative": 0.30,
    "receipt_delete":         0.60,
    "inbox_en":               0.63,
    "project_involvement":    0.65,
    "last_message":           0.75,
    "finance_accounting":     0.75,
    "birthday_lookup":        0.85,
    "project_start_date":     0.85,
    "nora_migration":         0.90,
    "bill_query":             0.92,
    "inbox_i18n":             0.95,
    "next_birthday":          0.95,
    "project_count":          0.95,
    "service_revenue_en":     1.00,
    "service_revenue_i18n":   1.00,
}

# Always-failing inbox positions (inbox item sequence number, 0/5 runs)
ALWAYS_FAILING_INBOX_POSITIONS = [16, 25, 29, 38, 42, 51]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _task_score(task: dict) -> float | None:
    """Return server score if available, else local score."""
    s = task.get("bitgn_score")
    if s is not None:
        return float(s)
    # Fallback: local pass count
    passes = task.get("passes", 0)
    runs = task.get("runs", 1)
    if runs > 0:
        return passes / runs
    return None


def _classify_failure(task: dict) -> str:
    """Classify a task failure into a bucket from score_detail."""
    detail = task.get("bitgn_score_detail", [])
    if not detail:
        outcome = task.get("last_outcome", "")
        if outcome == "OUTCOME_NONE_CLARIFICATION":
            return "agent_gave_up"
        if outcome == "OUTCOME_DENIED_SECURITY":
            return "false_security_refusal"
        if outcome == "OUTCOME_NONE_UNSUPPORTED":
            return "false_unsupported"
        return "unknown"

    first = detail[0].lower()
    if "expected outcome" in first:
        # Extract: "expected outcome X, got Y"
        return f"outcome_mismatch"
    if "frontmatter mismatch" in first:
        return "content_mismatch"
    if "invalid markdown frontmatter" in first:
        return "invalid_frontmatter"
    if "missing required reference" in first:
        return "missing_reference"
    if "answer is incorrect" in first:
        return "wrong_answer"
    return "other"


# ---------------------------------------------------------------------------
# Core reporting
# ---------------------------------------------------------------------------

def _collect_arch_rejects_per_task(trace_dir: Path) -> dict[str, list[str]]:
    """Return {task_id: [reject reason, ...]} from trace JSONL files.

    Used by --with-arch to aggregate validator REJECT reasons per intent.
    Lazy import so intent_report still runs when arch_constants is absent.
    """
    from bitgn_contest_agent.arch_constants import ArchCategory, ArchResult
    from bitgn_contest_agent.trace_schema import TraceArch, TraceMeta, load_jsonl

    out: dict[str, list[str]] = defaultdict(list)
    for jsonl in sorted(trace_dir.glob("t*.jsonl")):
        task_id: str | None = None
        try:
            for rec in load_jsonl(jsonl):
                if isinstance(rec, TraceMeta):
                    task_id = rec.task_id
                elif isinstance(rec, TraceArch):
                    if task_id and rec.result == ArchResult.REJECT:
                        for r in rec.reasons or []:
                            out[task_id].append(r)
        except Exception as exc:  # noqa: BLE001
            print(f"# arch read skipped for {jsonl.name}: {exc}",
                  file=sys.stderr)
    return dict(out)


def _normalise_reason(reason: str) -> str:
    """Collapse near-duplicate reasons into stable buckets for counting."""
    r = reason.lower()
    if "grounding_ref" in r and "never successfully read" in r:
        return "grounding_ref never read"
    if "mutation integrity" in r:
        return "mutation integrity mismatch"
    if "contradiction" in r:
        return "contradiction"
    if "dangerous" in r and "denied" in r:
        return "dangerous denied->ok"
    # fall back: keep the first 80 chars as a cluster key
    return reason[:80]


def build_intent_report(
    bench: dict,
    *,
    arch_rejects_by_task: dict[str, list[str]] | None = None,
) -> dict[str, dict]:
    """Build per-intent aggregation from a bench summary."""
    tasks = bench.get("tasks", {})
    report: dict[str, dict] = {}

    for intent, positions in INTENT_POSITIONS.items():
        intent_tasks = []
        for tid in positions:
            if tid not in tasks:
                continue
            t = tasks[tid]
            score = _task_score(t)
            if score is None:
                continue
            passed = score >= 0.999
            intent_tasks.append({
                "tid": tid,
                "passed": passed,
                "score": score,
                "instruction": t.get("bitgn_instruction", ""),
                "outcome": t.get("last_outcome", ""),
                "score_detail": t.get("bitgn_score_detail", []),
                "failure_type": _classify_failure(t) if not passed else "",
                "steps": t.get("median_steps", len(t.get("step_texts", []))),
                "latency_ms": t.get("last_latency_ms", 0),
            })

        if not intent_tasks:
            continue

        total = len(intent_tasks)
        passes = sum(1 for t in intent_tasks if t["passed"])
        fails = total - passes
        pass_rate = passes / total if total else 0.0
        baseline = HISTORICAL_BASELINE.get(intent, 0.0)

        reject_buckets: Counter = Counter()
        if arch_rejects_by_task is not None:
            for t in intent_tasks:
                for reason in arch_rejects_by_task.get(t["tid"], []):
                    reject_buckets[_normalise_reason(reason)] += 1

        report[intent] = {
            "total": total,
            "passes": passes,
            "fails": fails,
            "pass_rate": pass_rate,
            "baseline": baseline,
            "delta": pass_rate - baseline,
            "tasks": intent_tasks,
            "failure_types": defaultdict(int),
            "reject_buckets": dict(reject_buckets),
        }

        for t in intent_tasks:
            if not t["passed"]:
                report[intent]["failure_types"][t["failure_type"]] += 1

        # Convert defaultdict to regular dict for JSON serialisation
        report[intent]["failure_types"] = dict(report[intent]["failure_types"])

    return report


def print_report(
    report: dict[str, dict],
    *,
    failures_only: bool = False,
    baseline_report: dict[str, dict] | None = None,
) -> None:
    """Print a human-readable intent report to stdout."""
    print("=" * 78)
    print("INTENT-BASED BENCHMARK REPORT")
    print("=" * 78)

    # Summary table sorted by pass rate (worst first)
    sorted_intents = sorted(report.keys(), key=lambda i: report[i]["pass_rate"])

    # Overall stats
    total_tasks = sum(r["total"] for r in report.values())
    total_passes = sum(r["passes"] for r in report.values())
    total_rate = total_passes / total_tasks if total_tasks else 0.0
    print(f"\nOverall: {total_passes}/{total_tasks} ({total_rate:.0%})")
    print()

    # Table header
    hdr = f"  {'Intent':<25} {'Pass':>6} {'Total':>6} {'Rate':>7} {'Baseline':>9} {'Delta':>7}"
    if baseline_report:
        hdr += f" {'Prev':>7} {'Shift':>7}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for intent in sorted_intents:
        r = report[intent]
        if failures_only and r["fails"] == 0:
            continue

        delta_str = f"{r['delta']:+.0%}" if r["delta"] != 0 else "  =="
        line = (
            f"  {intent:<25} {r['passes']:>5}/{r['total']:<5} {r['pass_rate']:>6.0%}"
            f"  {r['baseline']:>7.0%}  {delta_str:>7}"
        )

        if baseline_report and intent in baseline_report:
            prev = baseline_report[intent]
            shift = r["pass_rate"] - prev["pass_rate"]
            shift_str = f"{shift:+.0%}" if shift != 0 else "  =="
            line += f"  {prev['pass_rate']:>6.0%}  {shift_str:>7}"

        # Colour markers
        if r["pass_rate"] < 0.5:
            line += "  !!!"
        elif r["pass_rate"] < 0.8:
            line += "  !"

        print(line)

    # Failure details
    print()
    print("=" * 78)
    print("FAILURE DETAILS (by intent)")
    print("=" * 78)

    for intent in sorted_intents:
        r = report[intent]
        if r["fails"] == 0:
            continue

        print(f"\n--- {intent} ({r['fails']} failures) ---")

        # Failure type summary
        if r["failure_types"]:
            print("  Failure types:")
            for ft, count in sorted(r["failure_types"].items(),
                                     key=lambda x: -x[1]):
                print(f"    {ft}: {count}")

        # Arch validator REJECT summary (if --with-arch was provided)
        rb = r.get("reject_buckets") or {}
        if rb:
            print("  Validator REJECT reasons (all tasks in intent):")
            for reason, count in sorted(rb.items(), key=lambda x: -x[1]):
                print(f"    {reason}: {count}")

        # Individual failures
        for t in r["tasks"]:
            if t["passed"]:
                continue
            instr = t["instruction"][:80] or "(no instruction recorded)"
            detail = ""
            if t["score_detail"]:
                detail = t["score_detail"][0][:100]
            print(f"  {t['tid']} [{t['failure_type']}] {instr}")
            if detail:
                print(f"       detail: {detail}")


def build_json_output(
    report: dict[str, dict],
    bench_path: str,
    baseline_report: dict[str, dict] | None = None,
) -> dict:
    """Build JSON-serialisable output."""
    summary = {}
    for intent, r in report.items():
        entry = {
            "pass_rate": round(r["pass_rate"], 4),
            "passes": r["passes"],
            "total": r["total"],
            "baseline": r["baseline"],
            "delta_vs_baseline": round(r["delta"], 4),
            "failure_types": r["failure_types"],
        }
        if baseline_report and intent in baseline_report:
            entry["prev_pass_rate"] = round(
                baseline_report[intent]["pass_rate"], 4
            )
            entry["delta_vs_prev"] = round(
                r["pass_rate"] - baseline_report[intent]["pass_rate"], 4
            )
        if r.get("reject_buckets"):
            entry["reject_buckets"] = r["reject_buckets"]
        # Failed task details
        entry["failures"] = [
            {
                "tid": t["tid"],
                "instruction": t["instruction"][:200],
                "failure_type": t["failure_type"],
                "score_detail": t["score_detail"],
                "outcome": t["outcome"],
            }
            for t in r["tasks"]
            if not t["passed"]
        ]
        summary[intent] = entry

    return {
        "bench_file": bench_path,
        "intents": summary,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("bench", type=Path, help="Bench summary JSON (post-ingest)")
    p.add_argument("--baseline", type=Path, default=None,
                   help="Previous bench JSON to compare against")
    p.add_argument("--failures-only", action="store_true",
                   help="Show only intents with failures")
    p.add_argument("--intent", action="append", dest="intents", default=None,
                   help="Filter to specific intents (repeatable)")
    p.add_argument("--json", action="store_true",
                   help="Output JSON instead of human-readable text")
    p.add_argument("--with-arch", action="store_true",
                   help="Join arch REJECT reasons per intent from trace_dir "
                        "(overall.trace_dir or --trace-dir)")
    p.add_argument("--trace-dir", type=Path, default=None,
                   help="Override trace dir used by --with-arch")
    args = p.parse_args()

    bench = _load(args.bench)

    arch_rejects: dict[str, list[str]] | None = None
    if args.with_arch:
        td = args.trace_dir
        if td is None:
            td_str = bench.get("overall", {}).get("trace_dir")
            td = Path(td_str) if td_str else None
        if td is None or not td.is_dir():
            print("error: --with-arch needs a valid trace dir", file=sys.stderr)
            return 2
        arch_rejects = _collect_arch_rejects_per_task(td)

    report = build_intent_report(bench, arch_rejects_by_task=arch_rejects)

    # Filter intents if requested
    if args.intents:
        unknown = set(args.intents) - set(report.keys())
        if unknown:
            print(f"Warning: unknown intents: {unknown}", file=sys.stderr)
            print(f"Available: {sorted(report.keys())}", file=sys.stderr)
        report = {k: v for k, v in report.items() if k in args.intents}

    baseline_report = None
    if args.baseline:
        baseline_bench = _load(args.baseline)
        baseline_report = build_intent_report(baseline_bench)  # no arch join for baseline

    if args.json:
        output = build_json_output(
            report, str(args.bench), baseline_report
        )
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print_report(
            report,
            failures_only=args.failures_only,
            baseline_report=baseline_report,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
