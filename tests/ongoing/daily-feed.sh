#!/usr/bin/env bash
# daily-feed.sh — Generate a day log and feed it to Mnemo Cortex for indexing
# Usage: ./daily-feed.sh [YYYY-MM-DD] [agent_id]
# Defaults to today's date and agent_id "sparky-test"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MNEMO_URL="${MNEMO_URL:-http://artforge:50001}"
DATE="${1:-$(date +%Y-%m-%d)}"
AGENT_ID="${2:-sparky-test}"
LOG_FILE="${SCRIPT_DIR}/feed-log.jsonl"
QUESTIONS_FILE="${SCRIPT_DIR}/test-questions.json"

# Format date for display
DISPLAY_DATE="$(date -d "$DATE" '+%B %d, %Y' 2>/dev/null || echo "$DATE")"

echo "=== Mnemo Cortex Daily Feed ==="
echo "Date:     $DATE"
echo "Agent:    $AGENT_ID"
echo "Endpoint: $MNEMO_URL"
echo ""

# ── Generate day log ──
# In production, Rocky will generate these from actual daily activity.
# For now, this generates a structured test log with specific retrievable facts.

generate_day_log() {
    cat <<DAYLOG
$DISPLAY_DATE — Daily Operations Log

COMPLETED TASKS:
1) Deployed bdpage images — 50 Barbie doll cards updated with photos, 44 still have placeholders. Total cards: 97. Firebase deploy successful at projectsparks.ai/bdpage.
2) Fixed Sparks Router classifier — the text-blob free-hint check was matching "HEARTBEAT" in Rocky's prompt body, routing all heartbeat-triggered conversations to Nemotron free instead of the configured tier. Removed lines 111-112 of classifier.py.
3) Retired Alice Moltman — Alice's Telegram bot (@AliceMoltmanBot) deactivated. Her workspace on artforge archived. Gateway ws://127.0.0.1:18789 shut down.

COST TRACKING:
- Rocky spent \$0.47 on smart tier across 22 calls (Gemini 3.1 Pro)
- Rocky spent \$0.12 on utility tier across 5 calls (Gemini 2.5 Flash)
- Free tier: 73 calls, \$0.00 (Nemotron 120B)
- Total daily spend: \$0.59
- Budget cap utilization: smart 4.7%, utility 4.0%

AGENT ACTIVITY:
- BW (Bullwinkle) downloaded 56 Barbie doll images from eBay and collector sites
- BW work directory: ~/.bw/work_dir/barbie-images/
- Rocky processed 23 smart-tier reasoning calls, average 2,100 tokens each
- Sparky ran 0 test tasks (framework being set up today)

BUSINESS:
- Sheri paid \$100 for ClaudePilot setup assistance
- Half Moon Bay temperature: 62°F, partly cloudy
- April's Barbie collection page now has 53 cards with images out of 97 total

INFRASTRUCTURE:
- Sparks Router config.json updated: utility tier changed from gemini-2.5-flash to gemini-3.1-pro-preview
- Router restarted via systemd after classifier fix
- Submarine Console profile TWO confirmed active but was not properly deployed to config.json
- THE VAULT (artforge): Mnemo Cortex v1 healthy, Ollama connected, qwen2.5:32b-instruct reasoning model active
- Mnemo Cortex v2 watcher/refresher daemons running on THE VAULT

PEOPLE:
- Guy directed all operations from IGOR laptop
- Guy is 73, lives in Half Moon Bay, CA
- Project Sparks makes 3D printed seasonal collectibles
DAYLOG
}

DAY_LOG="$(generate_day_log)"
TOKEN_ESTIMATE=$(( ${#DAY_LOG} / 4 ))

echo "Day log generated: ${#DAY_LOG} chars (~${TOKEN_ESTIMATE} tokens)"
echo ""

# ── Send to Mnemo Cortex via /writeback ──
# v1 API uses /writeback for curated session archiving into L1/L2 bundles.
# This creates searchable memory entries from structured facts.
echo "Sending to Mnemo Cortex via /writeback..."
START_TIME=$(date +%s%N)

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${MNEMO_URL}/writeback" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json, re

log = '''${DAY_LOG}'''

# Extract key facts from the log (lines with specific data points)
lines = [l.strip() for l in log.split('\n') if l.strip() and not l.strip().startswith('#')]
key_facts = [l.strip('- ') for l in lines if any(c.isdigit() for c in l) or '\$' in l][:15]

# Extract project names
projects = []
for kw in ['bdpage', 'Sparks Router', 'Alice', 'ClaudePilot', 'Barbie', 'Mnemo Cortex', 'Submarine Console', 'THE VAULT', 'OpenClaw']:
    if kw.lower() in log.lower():
        projects.append(kw)

# Extract decisions (lines with action verbs)
decisions = [l.strip('- ') for l in lines if any(v in l.lower() for v in ['changed', 'updated', 'fixed', 'removed', 'retired', 'deployed', 'configured'])][:10]

# Build summary from first section
summary_lines = [l for l in lines if l and not l.startswith('COMPLETED') and not l.startswith('COST') and not l.startswith('AGENT') and not l.startswith('BUSINESS') and not l.startswith('INFRASTRUCTURE') and not l.startswith('PEOPLE')]
summary = ' '.join(summary_lines[:3])[:500]

print(json.dumps({
    'session_id': 'daily-test-${DATE}',
    'summary': summary,
    'key_facts': key_facts,
    'projects_referenced': projects,
    'decisions_made': decisions,
    'agent_id': '${AGENT_ID}',
    'timestamp': '${DATE}T23:59:00Z'
}))
")")

END_TIME=$(date +%s%N)
ELAPSED_MS=$(( (END_TIME - START_TIME) / 1000000 ))

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)

echo "HTTP Status: $HTTP_CODE"
echo "Response: $BODY"
echo "Writeback time: ${ELAPSED_MS}ms"
echo ""

# ── Generate test questions for this date ──
echo "Generating test questions for $DATE..."

python3 << PYEOF
import json, os

date = "${DATE}"
display_date = "${DISPLAY_DATE}"
questions_file = "${QUESTIONS_FILE}"

# Load existing questions or start fresh
if os.path.exists(questions_file):
    with open(questions_file) as f:
        bank = json.load(f)
else:
    bank = {"generated": [], "dates": {}}

# Questions for this date
day_questions = {
    "date": date,
    "fed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "questions": {
        "needle": [
            {
                "q": f"What was task #2 on the completed list for {display_date}?",
                "a": "Fixed Sparks Router classifier",
                "detail": "text-blob free-hint check was matching HEARTBEAT in prompt body, removed lines 111-112 of classifier.py"
            },
            {
                "q": f"How much did Rocky spend on smart tier on {display_date}?",
                "a": "\$0.47",
                "detail": "across 22 calls using Gemini 3.1 Pro"
            },
            {
                "q": f"How many Barbie images did BW download on {display_date}?",
                "a": "56",
                "detail": "from eBay and collector sites"
            },
            {
                "q": f"What was the temperature in Half Moon Bay on {display_date}?",
                "a": "62°F",
                "detail": "partly cloudy"
            },
            {
                "q": f"How much did Sheri pay for ClaudePilot on {display_date}?",
                "a": "\$100",
                "detail": "ClaudePilot setup assistance"
            }
        ],
        "chain": [
            {
                "q": f"What was the Barbie image task about on {display_date} and who did the downloading?",
                "a": "BW (Bullwinkle) downloaded 56 Barbie doll images. 50 cards on bdpage were updated with photos, 44 still have placeholders.",
                "keywords": ["BW", "Bullwinkle", "56", "50", "bdpage"]
            },
            {
                "q": f"Why was the Sparks Router classifier fixed on {display_date}?",
                "a": "The text-blob free-hint check was matching the word HEARTBEAT in Rocky's prompt body, routing all heartbeat-triggered conversations to Nemotron free instead of the configured tier.",
                "keywords": ["HEARTBEAT", "free-hint", "Nemotron", "classifier"]
            },
            {
                "q": f"What happened with the Submarine Console on {display_date}?",
                "a": "Profile TWO was confirmed active in the dashboard but was not properly deployed to config.json. The utility tier was still set to gemini-2.5-flash instead of gemini-3.1-pro-preview.",
                "keywords": ["profile TWO", "config.json", "deployed", "utility"]
            }
        ],
        "general": [
            {
                "q": f"What did we do on {display_date}?",
                "a": "Deployed bdpage Barbie images, fixed Sparks Router classifier, retired Alice Moltman",
                "keywords": ["bdpage", "Barbie", "classifier", "Alice"]
            },
            {
                "q": f"Name three things accomplished on {display_date}.",
                "a": "1) Deployed bdpage images 2) Fixed Router classifier 3) Retired Alice",
                "keywords": ["bdpage", "classifier", "Alice"]
            },
            {
                "q": f"What happened with Alice on {display_date}?",
                "a": "Alice Moltman was retired. Telegram bot deactivated, workspace archived, gateway shut down.",
                "keywords": ["retired", "Telegram", "archived", "gateway"]
            }
        ]
    }
}

bank["dates"][date] = day_questions
bank["generated"].append({"date": date, "count": 11})

with open(questions_file, "w") as f:
    json.dump(bank, f, indent=2)

print(f"Wrote {len(day_questions['questions']['needle'])} needle, "
      f"{len(day_questions['questions']['chain'])} chain, "
      f"{len(day_questions['questions']['general'])} general questions")
PYEOF

# ── Log the feed ──
echo "" >> "$LOG_FILE"
echo "{\"date\":\"$DATE\",\"agent_id\":\"$AGENT_ID\",\"chars\":${#DAY_LOG},\"tokens_est\":$TOKEN_ESTIMATE,\"http_code\":$HTTP_CODE,\"ingest_ms\":$ELAPSED_MS,\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" >> "$LOG_FILE"

echo ""
echo "=== Feed complete ==="
echo "Log appended to: $LOG_FILE"
echo "Questions saved to: $QUESTIONS_FILE"
