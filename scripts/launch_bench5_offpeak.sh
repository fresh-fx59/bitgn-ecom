#!/usr/bin/env bash
# Off-peak PROD bench launcher (cron-driven).
# Fires at 05:03 UTC; targets cd2d57b on feat/perf-speedups.
set -euo pipefail

REPO=/home/claude-developer/bitgn-contest-with-claude
LOGDIR=$REPO/logs/bench5_offpeak
ART=$REPO/artifacts/bench/cd2d57b_offpeak_05utc_p3i6_prod_runs1.json
RUNLOG=$REPO/logs/bench5_offpeak.cron.log

cd "$REPO"
mkdir -p "$LOGDIR" "$(dirname "$ART")"

{
  echo "=== bench5_offpeak start: $(date -Iseconds) ==="
  echo "branch=$(git rev-parse --abbrev-ref HEAD) commit=$(git rev-parse --short HEAD)"
  echo "expected: feat/perf-speedups @ cd2d57b"
} >> "$RUNLOG"

set -a
# shellcheck disable=SC1091
source "$REPO/.worktrees/plan-b/.env"
set +a

.venv/bin/python -m bitgn_contest_agent.cli run-benchmark \
  --benchmark bitgn/pac1-prod \
  --max-parallel 3 --max-inflight-llm 6 \
  --runs 1 \
  --output "$ART" \
  --log-dir "$LOGDIR" \
  >> "$RUNLOG" 2>&1

echo "=== bench5_offpeak done: $(date -Iseconds) ===" >> "$RUNLOG"
