#!/usr/bin/env bash
# mnemo-cc-sync-loop — periodic Claude Code → Mnemo Cortex sync.
#
# Calls mnemo-cc-sync.py every ${INTERVAL} seconds. Designed to run
# under systemd with Restart=on-failure so it self-heals from transient errors.
#
# Configuration (env vars, all optional — see mnemo-cc-sync.py):
#   MNEMO_URL              Mnemo Cortex base URL (default: http://localhost:50001)
#   MNEMO_AGENT_ID         Agent ID (default: cc)
#   MNEMO_CC_SESSIONS_DIR  Where Claude Code stores .jsonl files
#   MNEMO_CC_OFFSET_FILE   Sync offset state file
#   MNEMO_CC_SYNC_INTERVAL Seconds between syncs (default: 60)
#
# SIGTERM force-flushes pending messages before exit so nothing is stranded.

INTERVAL=${MNEMO_CC_SYNC_INTERVAL:-60}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC="$SCRIPT_DIR/mnemo-cc-sync.py"

trap 'python3 "$SYNC" --force; exit 0' TERM INT

while true; do
    python3 "$SYNC" || echo "[cc-sync-loop] sync failed (will retry)"
    sleep "$INTERVAL"
done
