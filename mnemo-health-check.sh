#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  mnemo-health-check.sh — Simple health check for monitoring
# ═══════════════════════════════════════════════════════════════════
#
#  Returns exit code 0 if healthy, 1 if not.
#  Works with any monitoring tool: CronAlarm, cron, Nagios, etc.
#
#  Usage:
#    mnemo-health-check.sh                        # auto-detect URL
#    mnemo-health-check.sh http://artforge:50001   # explicit URL
#
#  Checks:
#    1. Server reachable at /health
#    2. Status is "ok"
#    3. Reasoning model is healthy
#    4. Embedding model is healthy
#    5. Live /context query succeeds (not just /health)
#
#  Exit codes:
#    0 = healthy
#    1 = unhealthy (details on stdout)
#
# ═══════════════════════════════════════════════════════════════════

MNEMO_URL="${1:-${MNEMO_URL:-http://localhost:50001}}"
TIMEOUT=10

# ─── Check 1: Server reachable ───
HEALTH=$(curl -sf --max-time "$TIMEOUT" "$MNEMO_URL/health" 2>/dev/null) || {
    echo "FAIL: Cannot reach mnemo-cortex at $MNEMO_URL"
    exit 1
}

# ─── Check 2: Status OK ───
STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
if [ "$STATUS" != "ok" ]; then
    echo "FAIL: Server status is '$STATUS' (expected 'ok')"
    exit 1
fi

# ─── Check 3 & 4: Models healthy ───
MODELS_OK=$(echo "$HEALTH" | python3 -c "
import sys,json
h = json.load(sys.stdin)
r = h.get('reasoning',{}).get('healthy', False)
e = h.get('embedding',{}).get('healthy', False)
print('ok' if r and e else 'fail')
" 2>/dev/null)

if [ "$MODELS_OK" != "ok" ]; then
    echo "FAIL: One or more models unhealthy"
    exit 1
fi

# ─── Check 5: Real /context query ───
FIRST_AGENT=$(echo "$HEALTH" | python3 -c "
import sys,json
agents = json.load(sys.stdin).get('agents_configured', ['default'])
print(agents[0] if agents else 'default')
" 2>/dev/null)

CONTEXT=$(curl -sf --max-time "$TIMEOUT" \
    -X POST "$MNEMO_URL/context" \
    -H "Content-Type: application/json" \
    -d "{\"prompt\":\"health check\",\"agent_id\":\"$FIRST_AGENT\",\"max_results\":1}" 2>/dev/null) || {
    echo "FAIL: /context endpoint not responding"
    exit 1
}

# ─── All passed ───
VERSION=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','?'))" 2>/dev/null)
AGENTS=$(echo "$HEALTH" | python3 -c "import sys,json; print(','.join(json.load(sys.stdin).get('agents_configured',[])))" 2>/dev/null)
echo "OK: mnemo-cortex v$VERSION — agents: $AGENTS"
exit 0
