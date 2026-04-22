#!/usr/bin/env bash
# bus-pending.sh — Print unread bus messages for one agent at session start.
# Designed to drop into a Claude Code SessionStart hook (see README).
# Silent when there are no pending messages, so normal sessions stay quiet.
set -euo pipefail

BUS_DB="${BUS_DB_PATH:-${BUS_DB:-$HOME/.sparks/bus.sqlite}}"
AGENT="${BUS_AGENT:-CC}"

[ -f "$BUS_DB" ] || exit 0

pending=$(sqlite3 "$BUS_DB" \
  "SELECT COUNT(*) FROM messages WHERE to_agent='${AGENT}' AND read=0 AND delivery_failed_at IS NULL;" \
  2>/dev/null || echo 0)
[ "$pending" -gt 0 ] || exit 0

echo ""
echo "=== PENDING BUS MESSAGES FOR ${AGENT} (${pending}) ==="
sqlite3 -separator ' | ' "$BUS_DB" \
  "SELECT '#' || id, 'from ' || from_agent || ':', subject, '(' || COALESCE(tracking_id, 'bus-' || id) || ')', 'created ' || created_at
   FROM messages WHERE to_agent='${AGENT}' AND read=0 AND delivery_failed_at IS NULL ORDER BY created_at ASC;"
echo ""
echo "Recall the full payload with mnemo_recall using the tracking_id (full mode),"
echo "or read the body directly from the bus DB (standalone mode)."
echo "=== END PENDING ==="
echo ""
