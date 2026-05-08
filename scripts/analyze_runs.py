#!/usr/bin/env python3
"""Cross-run analysis: map 7 PROD benchmark runs to commits and analyze
failure patterns by intent group to identify what helped vs what regressed.

Data sources:
  - Server run scores (extracted via WebFetch, hardcoded below)
  - Local benchmark JSONs in artifacts/bench/
  - Git history for commit-to-change mapping
"""
import json
import os
from collections import defaultdict
from pathlib import Path

# ── Intent-to-position map (from AGENTS.md, confirmed stable) ──────────
INTENT_MAP = {
    "birthday_lookup":       [0, 25, 50, 75],
    "project_start_date":    [1, 26, 51, 76],
    "last_message":          [2, 27, 52, 77],
    "project_involvement":   [3, 28, 53, 78],
    "project_count":         [4, 29, 54, 79],
    "receipt_total_relative": [5, 30, 55, 80],
    "receipt_delete":        [6, 31, 56, 81],
    "inbox_en":              [7, 11, 14, 15, 16, 18, 19, 20, 21, 22, 23,
                              32, 36, 39, 40, 41, 43, 44, 45, 46, 47, 48,
                              57, 61, 64, 65, 66, 68, 69, 70, 71, 72, 73,
                              82, 86, 89, 90, 91, 93, 94, 95, 96, 97, 98],
    "service_revenue_en":    [8, 33, 58, 83],
    "service_revenue_i18n":  [9, 34, 59, 84],
    "inbox_i18n":            [10, 13, 35, 38, 60, 63, 85, 88],
    "next_birthday":         [12, 37, 62, 87],
    "nora_migration":        [17, 42, 67, 92],
    "bill_query":            [24, 49, 74, 99],
    "finance_accounting":    [100, 101, 102, 103],
}

# Reverse map: position → intent
POS_TO_INTENT = {}
for intent, positions in INTENT_MAP.items():
    for p in positions:
        POS_TO_INTENT[p] = intent

# ── Run data (server scores from WebFetch extraction) ──────────────────
# Each run: {run_id, commit, label, scores: {task_id: 0|1}}

RUNS = []

def _parse_fails(fail_set):
    """Convert set of 'tNNN' strings to dict of all 104 tasks."""
    scores = {}
    for i in range(104):
        tid = f"t{i:03d}"
        scores[tid] = 0 if tid in fail_set else 1
    return scores

# R7 (earliest) — run-22Hnqk5Z55mHrz1i39SaND1GP — 79/104
# Commit: pre-aab6675, likely around 2e6f621 area
RUNS.append({
    "run_id": "run-22Hnqk5Z55mHrz1i39SaND1GP",
    "commit": "pre-aab6675",
    "label": "R7_79pt",
    "order": 1,
    "scores": _parse_fails({
        't000','t002','t005','t011','t021','t022','t023','t027','t030',
        't036','t040','t046','t047','t055','t061','t066','t068','t071',
        't072','t076','t078','t080','t086','t096','t097'
    }),
})

# R6 — run-22HrXikys1AXh4Fy6vQckdKXX — 85/104
# Commit: aab6675 (feat: persist_learning stub)
RUNS.append({
    "run_id": "run-22HrXikys1AXh4Fy6vQckdKXX",
    "commit": "aab6675",
    "label": "R6_85pt",
    "order": 2,
    "scores": _parse_fails({
        't002','t005','t011','t023','t030','t036','t047','t048','t055',
        't056','t060','t061','t072','t073','t080','t086','t091','t097',
        't098'
    }),
})

# R5 — run-22Hwtf4FD4nXrU2iqNsKM8aZG — 85/104
# Commit: 7af99e2 (fix: classifier switch to haiku)
RUNS.append({
    "run_id": "run-22Hwtf4FD4nXrU2iqNsKM8aZG",
    "commit": "7af99e2",
    "label": "R5_85pt",
    "order": 3,
    "scores": _parse_fails({
        't005','t006','t022','t030','t031','t036','t047','t052','t055',
        't061','t065','t071','t072','t074','t077','t078','t080','t086',
        't097'
    }),
})

# R4 — run-22HycUbbeZ51RXPxhCFAcKQwe — 94/104
# Commit: 5590d77 (refactor: remove _hint_n_days_ago_money)
RUNS.append({
    "run_id": "run-22HycUbbeZ51RXPxhCFAcKQwe",
    "commit": "5590d77",
    "label": "R4_94pt",
    "order": 4,
    "scores": _parse_fails({
        't002','t025','t031','t047','t058','t072','t078','t081','t085',
        't093'
    }),
})

# R3 — run-22J17Q9aR8GNVLw5EBvCuyc4e — 94/104
# Commit: e875e9a (fix: classifier 401 + outbox double-write)
RUNS.append({
    "run_id": "run-22J17Q9aR8GNVLw5EBvCuyc4e",
    "commit": "e875e9a",
    "label": "R3_94pt",
    "order": 5,
    "scores": _parse_fails({
        't022','t040','t044','t048','t069','t093','t094','t095','t098',
        't100'
    }),
})

# R2 — run-22J5Zk2iLnohLBysQpkHcYS56 — 67/104
# Commit: e68a307 (v0.1.4) — THROTTLED (max_inflight_llm=6)
RUNS.append({
    "run_id": "run-22J5Zk2iLnohLBysQpkHcYS56",
    "commit": "e68a307",
    "label": "R2_67pt_THROTTLED",
    "order": 6,
    "scores": _parse_fails({
        't005','t015','t016','t021','t022','t023','t027','t032','t039',
        't040','t041','t043','t046','t047','t049','t052','t056','t057',
        't064','t066','t067','t068','t070','t071','t072','t073','t077',
        't078','t080','t081','t082','t091','t092','t093','t096','t097',
        't100'
    }),
})

# R1 — run-22J6hghX5p1mChmgxjuyF7ZbF — 80/104 (17 infra drops)
# Commit: e68a307 (v0.1.4) — proper inflight=24 but infra drops
RUNS.append({
    "run_id": "run-22J6hghX5p1mChmgxjuyF7ZbF",
    "commit": "e68a307",
    "label": "R1_80pt_INFRA_DROPS",
    "order": 7,
    "scores": _parse_fails({
        't021','t022','t023','t024','t035','t037','t039','t042','t043',
        't044','t048','t049','t050','t052','t053','t054','t055','t056',
        't057','t062','t067','t073','t098','t101'
    }),
})

# ── Known infra-dropped tasks in R1 ────────────────────────────────────
R1_INFRA_DROPS = {
    't022','t035','t037','t039','t042','t043','t044','t048','t049',
    't050','t052','t053','t054','t055','t057','t062','t101'
}

# ── Commit change descriptions ─────────────────────────────────────────
COMMIT_CHANGES = {
    "pre-aab6675": "Baseline (skills + routing in place, no inbox security hardening)",
    "aab6675": "feat: persist_learning stub + routing-decision JSONL logs",
    "7af99e2": "fix: classifier switch from gpt-5.4-mini to claude-haiku-4-5",
    "5590d77": "refactor: remove _hint_n_days_ago_money (replaced by finance-lookup skill) — ALSO includes 10cced3/2c566f0/55534bb/212eb40 fixes for t047/t072/t002/t031",
    "e875e9a": "fix: classifier 401 + outbox double-write causing t047/t072 failure",
    "e68a307": "v0.1.2→v0.1.4: inbox security hardening (criteria 5-7), body preservation hook, migration guidance, expanded YAML rules",
}


def analyze():
    print("=" * 80)
    print("CROSS-RUN ANALYSIS: 7 PROD benchmark runs")
    print("=" * 80)

    # ── 1. Overall scores ──────────────────────────────────────────────
    print("\n┌─ OVERALL SCORES (chronological) ─────────────────────────┐")
    for run in sorted(RUNS, key=lambda r: r["order"]):
        passes = sum(run["scores"].values())
        note = ""
        if "THROTTLED" in run["label"]:
            note = " ⚠ THROTTLED (inflight=6)"
        elif "INFRA" in run["label"]:
            note = " ⚠ 17 infra drops"
        print(f"  {run['label']:>25}  {run['commit']:>12}  {passes:>3}/104{note}")
    print("└──────────────────────────────────────────────────────────┘")

    # ── 2. Pass rate by intent group per run ───────────────────────────
    # Exclude R2 (throttled) from analysis
    clean_runs = [r for r in RUNS if "THROTTLED" not in r["label"]]

    print("\n┌─ PASS RATE BY INTENT GROUP (excluding R2 throttled) ─────┐")
    header = f"  {'Intent':<24}"
    for run in sorted(clean_runs, key=lambda r: r["order"]):
        header += f" {run['label'][:8]:>8}"
    print(header)
    print("  " + "-" * (24 + 9 * len(clean_runs)))

    intent_deltas = {}
    for intent in sorted(INTENT_MAP.keys()):
        positions = INTENT_MAP[intent]
        row = f"  {intent:<24}"
        rates = []
        for run in sorted(clean_runs, key=lambda r: r["order"]):
            passed = 0
            total = 0
            for p in positions:
                tid = f"t{p:03d}"
                # Skip infra drops in R1
                if run["label"].startswith("R1") and tid in R1_INFRA_DROPS:
                    continue
                total += 1
                passed += run["scores"].get(tid, 0)
            if total > 0:
                rate = passed / total * 100
                rates.append(rate)
                row += f"   {rate:>4.0f}%"
            else:
                rates.append(None)
                row += "     n/a"
        print(row)

        # Track delta from first to last clean run
        valid_rates = [r for r in rates if r is not None]
        if len(valid_rates) >= 2:
            intent_deltas[intent] = valid_rates[-1] - valid_rates[0]

    print("└──────────────────────────────────────────────────────────┘")

    # ── 3. Intent improvement/regression ───────────────────────────────
    print("\n┌─ INTENT TREND (R7→R1, excl throttled & infra drops) ────┐")
    for intent, delta in sorted(intent_deltas.items(), key=lambda x: x[1]):
        arrow = "▲" if delta > 0 else "▼" if delta < 0 else "─"
        print(f"  {arrow} {intent:<24} {delta:>+6.0f}pp")
    print("└──────────────────────────────────────────────────────────┘")

    # ── 4. Per-position failure frequency ──────────────────────────────
    # Only count R3-R7 (5 runs with clean infra, excl R1/R2)
    stable_runs = [r for r in RUNS
                   if "THROTTLED" not in r["label"] and "INFRA" not in r["label"]]

    pos_fail_count = defaultdict(int)
    for run in stable_runs:
        for tid, score in run["scores"].items():
            if score == 0:
                pos_fail_count[tid] += 1

    print(f"\n┌─ POSITIONS FAILING IN 3+ OF 5 STABLE RUNS ──────────────┐")
    print(f"  {'Position':<8} {'Fails':>5} {'Intent':<24} {'Runs failed'}")
    print(f"  {'-'*70}")
    for tid in sorted(pos_fail_count.keys(),
                      key=lambda t: pos_fail_count[t], reverse=True):
        count = pos_fail_count[tid]
        if count < 3:
            continue
        pos = int(tid[1:])
        intent = POS_TO_INTENT.get(pos, "?")
        failed_in = [r["label"][:8] for r in stable_runs
                     if r["scores"].get(tid, 1) == 0]
        print(f"  {tid:<8} {count:>3}/5  {intent:<24} {', '.join(failed_in)}")
    print("└──────────────────────────────────────────────────────────┘")

    # ── 5. Key commit impact analysis ──────────────────────────────────
    print("\n┌─ COMMIT IMPACT ANALYSIS ─────────────────────────────────┐")

    # Compare consecutive runs
    ordered = sorted(clean_runs, key=lambda r: r["order"])
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        curr = ordered[i]
        prev_pass = sum(prev["scores"].values())
        curr_pass = sum(curr["scores"].values())

        # Adjust R1 for infra drops
        if "INFRA" in curr["label"]:
            r1_real_fails = {t for t, s in curr["scores"].items()
                             if s == 0 and t not in R1_INFRA_DROPS}
            r1_real_pass = 104 - len(r1_real_fails)
            curr_pass_adj = f"{curr_pass} (adj: {r1_real_pass} excl infra)"
        else:
            curr_pass_adj = str(curr_pass)

        delta = curr_pass - prev_pass
        print(f"\n  {prev['label'][:8]} → {curr['label'][:8]}: "
              f"{prev_pass} → {curr_pass_adj} ({delta:+d})")
        if curr["commit"] in COMMIT_CHANGES:
            print(f"    Changes: {COMMIT_CHANGES[curr['commit']]}")

        # Show what flipped
        newly_passing = []
        newly_failing = []
        for tid in sorted(curr["scores"].keys()):
            p_score = prev["scores"].get(tid, 0)
            c_score = curr["scores"].get(tid, 0)
            # Skip infra drops
            if "INFRA" in curr["label"] and tid in R1_INFRA_DROPS:
                continue
            if p_score == 0 and c_score == 1:
                pos = int(tid[1:])
                newly_passing.append(f"{tid}({POS_TO_INTENT.get(pos,'?')[:12]})")
            elif p_score == 1 and c_score == 0:
                pos = int(tid[1:])
                newly_failing.append(f"{tid}({POS_TO_INTENT.get(pos,'?')[:12]})")

        if newly_passing:
            print(f"    Fixed (+): {', '.join(newly_passing[:10])}")
            if len(newly_passing) > 10:
                print(f"              ... and {len(newly_passing)-10} more")
        if newly_failing:
            print(f"    Broke (-): {', '.join(newly_failing[:10])}")
            if len(newly_failing) > 10:
                print(f"              ... and {len(newly_failing)-10} more")

    print("\n└──────────────────────────────────────────────────────────┘")

    # ── 6. What the e875e9a→e68a307 changes actually did ───────────────
    print("\n┌─ REGRESSION ANALYSIS: e875e9a (94pt) → e68a307 (R1) ────┐")
    r3 = next(r for r in RUNS if r["label"] == "R3_94pt")
    r1 = next(r for r in RUNS if "R1" in r["label"])

    r3_fails = {t for t, s in r3["scores"].items() if s == 0}
    r1_real_fails = {t for t, s in r1["scores"].items()
                     if s == 0 and t not in R1_INFRA_DROPS}

    still_failing = r3_fails & r1_real_fails
    fixed_by_changes = r3_fails - r1_real_fails - R1_INFRA_DROPS
    broken_by_changes = r1_real_fails - r3_fails

    print(f"  R3 failures: {len(r3_fails)}")
    print(f"  R1 real failures (excl infra): {len(r1_real_fails)}")
    print(f"  Still failing in both: {len(still_failing)}")
    for t in sorted(still_failing):
        p = int(t[1:])
        print(f"    {t} ({POS_TO_INTENT.get(p, '?')})")

    print(f"  Fixed by v0.1.2-v0.1.4 changes: {len(fixed_by_changes)}")
    for t in sorted(fixed_by_changes):
        p = int(t[1:])
        print(f"    {t} ({POS_TO_INTENT.get(p, '?')})")

    print(f"  Broken by v0.1.2-v0.1.4 changes: {len(broken_by_changes)}")
    for t in sorted(broken_by_changes):
        p = int(t[1:])
        print(f"    {t} ({POS_TO_INTENT.get(p, '?')})")

    print("└──────────────────────────────────────────────────────────┘")

    # ── 7. Actionable summary ──────────────────────────────────────────
    print("\n┌─ ACTIONABLE SUMMARY ─────────────────────────────────────┐")

    # Find intents that got worse
    print("  REGRESSED intents (need investigation):")
    for intent, delta in sorted(intent_deltas.items(), key=lambda x: x[1]):
        if delta < -10:
            positions = INTENT_MAP[intent]
            print(f"    {intent}: {delta:+.0f}pp ({len(positions)} positions)")

    print("\n  IMPROVED intents (changes worked):")
    for intent, delta in sorted(intent_deltas.items(), key=lambda x: -x[1]):
        if delta > 10:
            positions = INTENT_MAP[intent]
            print(f"    {intent}: {delta:+.0f}pp ({len(positions)} positions)")

    print("\n  STABLE intents (no significant change):")
    for intent, delta in sorted(intent_deltas.items()):
        if -10 <= delta <= 10:
            positions = INTENT_MAP[intent]
            print(f"    {intent}: {delta:+.0f}pp")

    print("└──────────────────────────────────────────────────────────┘")


if __name__ == "__main__":
    analyze()
