#!/usr/bin/env bash
# One-toggle switch between provider configs in .env.
#
# Usage:
#   scripts/use_provider.sh cliproxyapi
#   scripts/use_provider.sh closerouter
#   scripts/use_provider.sh linkapi
#   scripts/use_provider.sh           # show current active provider
#
# Mechanism: rewrites .env to comment-out one provider block and
# uncomment the target. Idempotent. After switching, probes the new
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
  local active
  active=$(grep -E '^CLIPROXY_BASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
  case "$active" in
    https://api.closerouter.dev/*) echo "closerouter" ;;
    http://127.0.0.1:8317/*)       echo "cliproxyapi" ;;
    https://api.linkapi.ai/*)      echo "linkapi" ;;
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
  cliproxyapi|closerouter|linkapi) ;;
  *) echo "usage: $0 [cliproxyapi|closerouter|linkapi]" >&2; exit 2 ;;
esac

current="$(read_active)"
if [[ "$current" == "$target" ]]; then
  echo "already on $target"
else
  python3 - "$ENV_FILE" "$target" <<'PY'
import sys, re
path, target = sys.argv[1], sys.argv[2]
src = open(path).read()

def block_of(line: str) -> str | None:
    """Return the provider this env line belongs to, or None for shared lines."""
    if "api.closerouter.dev" in line or "closerouter_" in line or "anthropic/claude-haiku-4.5" in line:
        return "closerouter"
    if "127.0.0.1:8317" in line or ("e7a6d7b34d" in line and "CLIPROXY_API_KEY=" in line):
        return "cliproxyapi"
    if "api.linkapi.ai" in line or ("sk-FtCY018D" in line and "CLIPROXY_API_KEY=" in line):
        return "linkapi"
    # CLASSIFIER_MODEL is ambiguous — disambiguate by the model value
    if "BITGN_CLASSIFIER_MODEL=" in line:
        val = line.split("=", 1)[1].strip()
        if val.startswith("anthropic/"):
            return "closerouter"
        if val.startswith("claude-"):
            # linkapi uses bare Haiku id; cliproxyapi uses same bare id too
            # — disambiguate by comment? Use heuristic: linkapi block
            # comes after the linkapi base_url.
            return "linkapi"  # we'll put cliproxyapi's classifier as gpt-5.4-mini
        if val.startswith("gpt-"):
            return "cliproxyapi"
    return None

out = []
saw_profile_var = False
for line in src.splitlines():
    stripped = line.lstrip("# ").rstrip()
    if stripped.startswith("BITGN_PROVIDER_PROFILE="):
        saw_profile_var = True
        out.append(f"BITGN_PROVIDER_PROFILE={target}")
        continue
    if any(stripped.startswith(k) for k in
           ("CLIPROXY_BASE_URL=", "CLIPROXY_API_KEY=", "BITGN_CLASSIFIER_MODEL=")):
        owner = block_of(stripped)
        if owner == target:
            out.append(stripped)
        elif owner in {"closerouter", "cliproxyapi", "linkapi"}:
            # not the target — comment it out
            out.append("# " + stripped if not line.lstrip().startswith("#") else line)
        else:
            out.append(line)
    else:
        out.append(line)

if not saw_profile_var:
    out.append(f"BITGN_PROVIDER_PROFILE={target}")

open(path, "w").write("\n".join(out) + "\n")
PY
  echo "switched: $current → $target"
fi

# Probe the active provider once so a dead route is caught now.
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
