#!/usr/bin/env bash
# mnemo-startup.sh — Pull context from Mnemo Cortex at Claude Code session start
# Part of: https://github.com/GuyMannDude/mnemo-cortex
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

# Health check — fail silently so CC still works without memory
health=$(curl -sf --max-time 5 "${MNEMO_URL}/health" 2>/dev/null) || {
    echo "[mnemo] Mnemo Cortex unreachable at ${MNEMO_URL} — skipping memory load"
    exit 0
}

status=$(echo "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")
if [ "$status" = "down" ]; then
    echo "[mnemo] Mnemo Cortex is down — skipping memory load"
    exit 0
fi

echo "=== MNEMO CORTEX — SESSION MEMORY ==="
echo "Status: ${status} | Agent: ${AGENT_ID}"
echo ""

# Pull recent session context
recent=$(curl -sf --max-time 10 "${MNEMO_URL}/sessions/recent?agent_id=${AGENT_ID}&n=20" 2>/dev/null) || true
if [ -n "$recent" ]; then
    context=$(echo "$recent" | python3 -c "import sys,json; print(json.load(sys.stdin).get('context',''))" 2>/dev/null || true)
    if [ -n "$context" ] && [ "$context" != "None" ] && [ "$context" != "" ]; then
        echo "## Recent Activity"
        echo "$context"
        echo ""
    fi
fi

# Pull semantic context — what's been happening lately
ctx=$(curl -sf --max-time 15 "${MNEMO_URL}/context" \
    -H 'Content-Type: application/json' \
    -d "{\"prompt\": \"current priorities, recent work, active tasks, and important decisions\", \"agent_id\": \"${AGENT_ID}\", \"max_results\": 5}" 2>/dev/null) || true

if [ -n "$ctx" ]; then
    total=$(echo "$ctx" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_found',0))" 2>/dev/null || echo "0")
    if [ "$total" != "0" ] && [ -n "$total" ]; then
        echo "## Relevant Memory (${total} chunks)"
        echo "$ctx" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for chunk in data.get('chunks', []):
    tier = chunk.get('cache_tier', '?')
    rel = chunk.get('relevance', 0)
    print(f'### [{tier}] (relevance: {rel:.2f})')
    print(chunk.get('content', ''))
    print()
" 2>/dev/null || true
    fi
fi

echo "=== END MNEMO CONTEXT ==="
