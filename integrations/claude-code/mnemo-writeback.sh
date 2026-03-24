#!/usr/bin/env bash
# mnemo-writeback.sh — Archive session summary to Mnemo Cortex at Claude Code session end
# Part of: https://github.com/GuyMannDude/mnemo-cortex
#
# Usage:
#   mnemo-writeback.sh "summary of what happened" ["key fact 1" "key fact 2" ...]
#   echo "summary" | mnemo-writeback.sh --stdin
#
# Environment variables (set by install.sh in ~/.mnemo-cc/env):
#   MNEMO_URL      — Mnemo Cortex server URL (default: http://localhost:50001)
#   MNEMO_AGENT_ID — Your agent identifier (default: cc)
set -euo pipefail

# Load config
MNEMO_ENV="${HOME}/.mnemo-cc/env"
[ -f "$MNEMO_ENV" ] && source "$MNEMO_ENV"

MNEMO_URL="${MNEMO_URL:-http://localhost:50001}"
AGENT_ID="${MNEMO_AGENT_ID:-cc}"
SESSION_ID="${MNEMO_SESSION_ID:-${AGENT_ID}-$(date +%Y%m%d-%H%M%S)}"

# Parse input
SUMMARY=""
KEY_FACTS=()

if [ "${1:-}" = "--stdin" ]; then
    SUMMARY=$(cat)
elif [ $# -ge 1 ]; then
    SUMMARY="$1"
    shift
    KEY_FACTS=("$@")
fi

if [ -z "$SUMMARY" ]; then
    echo "[mnemo] ERROR: No summary provided"
    echo "Usage: mnemo-writeback.sh \"summary text\" [\"key fact 1\" ...]"
    exit 1
fi

# Health check — save locally if server unreachable
curl -sf --max-time 5 "${MNEMO_URL}/health" >/dev/null 2>&1 || {
    echo "[mnemo] Mnemo Cortex unreachable — saving to local queue"
    mkdir -p "${HOME}/.mnemo-cc/queue"
    python3 -c "
import json, sys
entry = {
    'session_id': sys.argv[1],
    'agent_id': sys.argv[2],
    'summary': sys.argv[3],
    'key_facts': sys.argv[4:]
}
with open(f'{sys.argv[5]}/{sys.argv[1]}.json', 'w') as f:
    json.dump(entry, f, indent=2)
" "$SESSION_ID" "$AGENT_ID" "$SUMMARY" "${KEY_FACTS[@]}" "${HOME}/.mnemo-cc/queue" 2>/dev/null || true
    echo "[mnemo] Saved to ~/.mnemo-cc/queue/${SESSION_ID}.json"
    exit 0
}

# Build JSON payload safely with python
PAYLOAD=$(python3 -c "
import json, sys
summary = sys.argv[1]
key_facts = sys.argv[2:]
print(json.dumps({
    'session_id': '$SESSION_ID',
    'summary': summary,
    'key_facts': key_facts,
    'projects_referenced': [],
    'decisions_made': [],
    'agent_id': '$AGENT_ID'
}))
" "$SUMMARY" "${KEY_FACTS[@]:+${KEY_FACTS[@]}}")

# Writeback
response=$(curl -sf --max-time 15 "${MNEMO_URL}/writeback" \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" 2>&1) || {
    echo "[mnemo] ERROR: Writeback failed"
    exit 1
}

mem_id=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('memory_id','?'))" 2>/dev/null || echo "?")
echo "[mnemo] Archived: session=${SESSION_ID} memory_id=${mem_id} agent=${AGENT_ID}"
