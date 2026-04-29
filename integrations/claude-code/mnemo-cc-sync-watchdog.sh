#!/usr/bin/env bash
# mnemo-cc-sync-watchdog — health check for mnemo-cc-sync.service.
#
# Verifies:
#   1. The systemd service is active
#   2. If the Claude Code session JSONL was written to in the last 30 min, the
#      sync has posted within the last 30 min too (no stuck-offset condition)
#
# Exits non-zero on failure so cron / scheduler can alert. Designed to plug
# into any monitoring tool that watches command exit codes — CronAlarm,
# systemd OnFailure=, healthchecks.io, or a plain cron + email.
#
# Configuration (env vars, all optional):
#   MNEMO_CC_SERVICE       systemd unit name (default: mnemo-cc-sync.service)
#   MNEMO_CC_OFFSET_FILE   Sync offset state file (default: ~/.mnemo-cc/cc-sync.offset.json)
#   MNEMO_CC_SESSIONS_DIR  Where Claude Code stores .jsonl files (default: ~/.claude/projects)
#   MNEMO_CC_STALE_S       Seconds after which sync is considered stale (default: 1800 = 30 min)

set -e

SERVICE=${MNEMO_CC_SERVICE:-mnemo-cc-sync.service}
OFFSET=${MNEMO_CC_OFFSET_FILE:-$HOME/.mnemo-cc/cc-sync.offset.json}
SESSIONS_DIR=${MNEMO_CC_SESSIONS_DIR:-$HOME/.claude/projects}
THRESHOLD_S=${MNEMO_CC_STALE_S:-1800}

# 1) Service must be active
if ! systemctl --user is-active --quiet "$SERVICE"; then
    echo "WATCHDOG FAIL: $SERVICE is not active"
    systemctl --user status "$SERVICE" --no-pager 2>&1 | head -10
    exit 1
fi

# 2) If no session JSONL exists, nothing to sync. Service-active alone passes.
LATEST_JSONL=$(find "$SESSIONS_DIR" -name "*.jsonl" -printf "%T@ %p\n" 2>/dev/null \
               | sort -rn | head -1 | cut -d' ' -f2-)
if [ -z "$LATEST_JSONL" ]; then
    echo "OK: $SERVICE active, no Claude Code sessions to sync"
    exit 0
fi

NOW=$(date +%s)
JSONL_MTIME=$(stat -c %Y "$LATEST_JSONL")
JSONL_AGE=$((NOW - JSONL_MTIME))

# Idle Claude Code — sync staleness doesn't matter
if [ "$JSONL_AGE" -gt "$THRESHOLD_S" ]; then
    echo "OK: $SERVICE active, Claude Code idle (no JSONL activity in last ${THRESHOLD_S}s)"
    exit 0
fi

# Active session — sync must also be recent
if [ ! -f "$OFFSET" ]; then
    echo "WATCHDOG FAIL: Claude Code active (JSONL touched ${JSONL_AGE}s ago) but no offset file — sync not running"
    exit 1
fi

LAST_POST=$(python3 -c "import json; print(json.load(open('$OFFSET')).get('last_post_at',''))")
if [ -z "$LAST_POST" ]; then
    echo "WATCHDOG FAIL: offset file has no last_post_at — sync hasn't completed a cycle yet"
    exit 1
fi

LAST_POST_EPOCH=$(date -d "$LAST_POST" +%s 2>/dev/null || echo 0)
POST_AGE=$((NOW - LAST_POST_EPOCH))

if [ "$POST_AGE" -gt "$THRESHOLD_S" ]; then
    echo "WATCHDOG FAIL: Claude Code session active (JSONL touched ${JSONL_AGE}s ago) but last sync was ${POST_AGE}s ago"
    echo "  service: $(systemctl --user is-active $SERVICE)"
    echo "  offset:  $OFFSET"
    echo "  last_post_at: $LAST_POST"
    exit 1
fi

echo "OK: $SERVICE active, last sync ${POST_AGE}s ago (Claude Code active ${JSONL_AGE}s ago)"
