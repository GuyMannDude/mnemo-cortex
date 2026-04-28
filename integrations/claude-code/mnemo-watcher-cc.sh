#!/usr/bin/env bash
# ⚠️ DEPRECATED — see mnemo-cc-artforge-sync.py instead.
#
# This legacy watcher writes raw session messages to a LOCAL SQLite at
# ~/.mnemo-v2/mnemo.sqlite3 using the older mnemo-cortex-v2 codebase. That
# DB is not what the central Mnemo Cortex API serves, so messages ingested
# here are NOT recallable across agents in the v2.6+ architecture.
#
# The supported path is `mnemo-cc-artforge-sync.py` + `*-loop.sh`, which
# POST structured memories directly to Mnemo Cortex's /writeback endpoint.
# See README.md → "Automatic Mode (Session Sync)".
#
# Migration:
#   systemctl --user disable --now mnemo-watcher-cc.service
#   # follow the systemd setup in README.md for mnemo-cc-artforge-sync
#
# This file is preserved for users still running v2 setups. It will be
# removed in a future release.
#
# ---------- Original watcher follows ----------
# Mnemo v2 Session Watcher for Claude Code
# Watches the newest session file and ingests messages into Mnemo Cortex.
#
# Usage: Edit the variables below, then run directly or install as a systemd service.

# --- Configuration ---
# Claude Code stores session files in ~/.claude/projects/<project-hash>/
# Find yours with: ls -t ~/.claude/projects/*//*.jsonl | head -1
SESSIONS_DIR="${MNEMO_CC_SESSIONS_DIR:-$HOME/.claude/projects}"
DB="${MNEMO_CC_DB:-$HOME/.mnemo-v2/mnemo.sqlite3}"
CHECKPOINT="${MNEMO_CC_CHECKPOINT:-$HOME/.mnemo-v2/watcher-cc.offset}"
AGENT_ID="${MNEMO_AGENT_ID:-cc}"
INTERVAL="${MNEMO_CC_INTERVAL:-2}"
MNEMO_CORTEX_DIR="${MNEMO_CORTEX_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

cd "$MNEMO_CORTEX_DIR"
source .venv/bin/activate

mkdir -p "$(dirname "$CHECKPOINT")"

LAST_FILE=""

while true; do
    # Find the newest session file across all project dirs
    NEWEST=$(find "$SESSIONS_DIR" -name "*.jsonl" -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

    if [[ -z "$NEWEST" ]]; then
        sleep "$INTERVAL"
        continue
    fi

    # If session file changed, reset checkpoint
    if [[ "$NEWEST" != "$LAST_FILE" ]]; then
        SESSION_ID=$(basename "$NEWEST" .jsonl)
        echo "0" > "$CHECKPOINT"
        LAST_FILE="$NEWEST"
        echo "[mnemo-watcher-cc] Tracking session: $SESSION_ID"
    fi

    python3 -c "
from mnemo_v2.watch.session_watcher import SessionWatcher
w = SessionWatcher(\"$DB\", \"$NEWEST\", \"$CHECKPOINT\")
n = w.poll_once(agent_id=\"$AGENT_ID\", session_id=\"$SESSION_ID\")
if n > 0:
    print(f\"[mnemo-watcher-cc] Ingested {n} messages\")
"
    sleep "$INTERVAL"
done
