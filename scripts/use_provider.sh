#!/usr/bin/env bash
# One-toggle switch between provider configs in .env.
#
# Usage:
#   scripts/use_provider.sh cliproxyapi
#   scripts/use_provider.sh closerouter
#   scripts/use_provider.sh           # show current active provider
#
# Mechanism: rewrites .env to comment-out one provider block and
# uncomment the other. Idempotent. After switching, probes the new
# provider once so a dead route is caught immediately rather than at
# the next bench.

set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "no .env at $ENV_FILE" >&2
  exit 1
fi

read_active() {
  # The active block is the one whose CLIPROXY_BASE_URL line is NOT
  # commented. Returns "closerouter", "cliproxyapi", or "unknown".
  local active
  active=$(grep -E '^CLIPROXY_BASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
  case "$active" in
    https://api.closerouter.dev/*) echo "closerouter" ;;
    http://127.0.0.1:8317/*)       echo "cliproxyapi" ;;
    *)                              echo "unknown:${active:-empty}" ;;
  esac
}

if [[ -z "${1:-}" ]]; then
  echo "active: $(read_active)"
  echo "configs in $ENV_FILE:"
  grep -E '^[# ]*CLIPROXY_BASE_URL=|^[# ]*CLIPROXY_API_KEY=|^[# ]*BITGN_CLASSIFIER_MODEL=' "$ENV_FILE" | sed 's/^/  /'
  exit 0
fi

target="$1"
case "$target" in
  cliproxyapi|closerouter) ;;
  *) echo "usage: $0 [cliproxyapi|closerouter]" >&2; exit 2 ;;
esac

current="$(read_active)"
if [[ "$current" == "$target" ]]; then
  echo "already on $target"
else
  python3 - "$ENV_FILE" "$target" <<'PY'
import sys, re
path, target = sys.argv[1], sys.argv[2]
src = open(path).read()
# The .env carries two named blocks: "closerouter" (URL contains
# api.closerouter.dev) and "cliproxyapi" (URL contains 127.0.0.1:8317).
# Three keys per block: CLIPROXY_BASE_URL, CLIPROXY_API_KEY,
# BITGN_CLASSIFIER_MODEL. We flip the comment state per-line so the
# target block ends up active and the other commented.
CR_KEYS  = {"closerouter":  ("api.closerouter.dev",    "closerouter_",        "anthropic/claude-haiku-4.5")}
CLP_KEYS = {"cliproxyapi":  ("127.0.0.1:8317",         "e7a6d7b34d",          "claude-haiku-4-5-20251001")}

def is_in_block(line: str, block: str) -> bool:
    if block == "closerouter":
        return ("api.closerouter.dev" in line
                or "closerouter_" in line
                or "anthropic/claude-haiku-4.5" in line)
    if block == "cliproxyapi":
        return ("127.0.0.1:8317" in line
                or ("e7a6d7b34d" in line and "CLIPROXY_API_KEY=" in line)
                or "claude-haiku-4-5-20251001" in line)
    return False

out = []
for line in src.splitlines():
    stripped = line.lstrip("# ").rstrip()
    # Look for one of the three known keys
    if any(stripped.startswith(k) for k in
           ("CLIPROXY_BASE_URL=", "CLIPROXY_API_KEY=", "BITGN_CLASSIFIER_MODEL=")):
        if is_in_block(stripped, target):
            # target block: ensure uncommented
            out.append(stripped)
        elif is_in_block(stripped, "closerouter" if target == "cliproxyapi" else "cliproxyapi"):
            # other block: ensure commented
            out.append("# " + stripped if not line.lstrip().startswith("#") else line)
        else:
            out.append(line)
    else:
        out.append(line)
open(path, "w").write("\n".join(out) + "\n")
PY
  echo "switched: $current → $target"
fi

# Probe the active provider once so a dead route is caught now, not
# at the next bench.
set -a
source "$ENV_FILE"
set +a

if [[ -z "${CLIPROXY_BASE_URL:-}" || -z "${CLIPROXY_API_KEY:-}" ]]; then
  echo "WARN: CLIPROXY_BASE_URL / CLIPROXY_API_KEY not set after switch" >&2
  exit 1
fi

probe_model="${AGENT_MODEL:-gpt-5.3-codex}"
echo "probing $probe_model via $CLIPROXY_BASE_URL ..."
resp=$(curl -s -m 25 -H "Authorization: Bearer $CLIPROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$probe_model\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"reasoning_effort\":\"low\",\"stream\":false}" \
  "$CLIPROXY_BASE_URL/chat/completions" 2>&1)
if echo "$resp" | grep -q '"content"'; then
  echo "OK — provider responsive"
else
  echo "WARN — provider did NOT return content:"
  echo "$resp" | head -c 300
  echo
fi
