#!/usr/bin/env bash
# daily-feed.sh — Generate a synthetic day log and feed it to Mnemo Cortex
# for indexing. Used to seed the index with structured retrievable facts so
# the test-questions harness can verify recall.
#
# Usage: ./daily-feed.sh [YYYY-MM-DD] [agent_id]
# Defaults to today's date and agent_id "test-agent"
#
# All names, numbers, and businesses below are fictional.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MNEMO_URL="${MNEMO_URL:-http://localhost:50001}"
DATE="${1:-$(date +%Y-%m-%d)}"
AGENT_ID="${2:-test-agent}"
LOG_FILE="${SCRIPT_DIR}/feed-log.jsonl"
QUESTIONS_FILE="${SCRIPT_DIR}/test-questions.json"

# Format date for display
DISPLAY_DATE="$(date -d "$DATE" '+%B %d, %Y' 2>/dev/null || echo "$DATE")"

echo "=== Mnemo Cortex Daily Feed ==="
echo "Date:     $DATE"
echo "Agent:    $AGENT_ID"
echo "Endpoint: $MNEMO_URL"
echo ""

# ── Generate synthetic day log ──
# This produces structured fictional content with specific retrievable facts
# so the test-questions harness can verify exact-recall and chain-recall.

generate_day_log() {
    cat <<DAYLOG
$DISPLAY_DATE — Daily Operations Log (synthetic test data)

COMPLETED TASKS:
1) Deployed catalog page — 50 product cards updated with photos, 44 still have placeholders. Total cards: 97. Static deploy successful at example.test/catalog.
2) Fixed router classifier — the text-blob hint check was matching "HEARTBEAT" in the agent prompt body, routing all heartbeat-triggered conversations to the free tier instead of the configured paid tier. Removed lines 111-112 of classifier.py.
3) Retired the legacy chat bot — Telegram integration (@LegacyBotExample) deactivated. Its workspace archived. Gateway ws://127.0.0.1:18789 shut down.

COST TRACKING:
- Smart tier: \$0.47 across 22 calls (test-model-large)
- Utility tier: \$0.12 across 5 calls (test-model-small)
- Free tier: 73 calls, \$0.00 (test-model-free)
- Total daily spend: \$0.59
- Budget cap utilization: smart 4.7%, utility 4.0%

AGENT ACTIVITY:
- Research agent downloaded 56 product reference images from public sources
- Research agent work directory: /var/test/research-images/
- Builder agent processed 23 smart-tier reasoning calls, average 2,100 tokens each
- Test agent ran 0 test tasks (framework being set up today)

BUSINESS:
- Test customer paid \$100 for setup assistance
- Office temperature: 62°F, partly cloudy
- The catalog page now has 53 cards with images out of 97 total

INFRASTRUCTURE:
- Router config.json updated: utility tier changed from test-model-small to test-model-medium
- Router restarted via systemd after classifier fix
- Test profile TWO confirmed active but was not properly deployed to config.json
- Memory store (primary host): Mnemo Cortex v1 healthy, local LLM connected, qwen2.5:32b-instruct reasoning model active
- Mnemo Cortex v2 watcher/refresher daemons running on the primary host

PEOPLE:
- The operator directed operations from a laptop terminal
- Test environment running on a development workstation
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
for kw in ['catalog', 'router', 'chat bot', 'Telegram', 'Mnemo Cortex', 'profile', 'memory store']:
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
                "a": "Fixed router classifier",
                "detail": "text-blob hint check was matching HEARTBEAT in prompt body, removed lines 111-112 of classifier.py"
            },
            {
                "q": f"How much was spent on smart tier on {display_date}?",
                "a": "\$0.47",
                "detail": "across 22 calls using test-model-large"
            },
            {
                "q": f"How many product reference images did the research agent download on {display_date}?",
                "a": "56",
                "detail": "from public sources"
            },
            {
                "q": f"What was the office temperature on {display_date}?",
                "a": "62°F",
                "detail": "partly cloudy"
            },
            {
                "q": f"How much did the test customer pay for setup assistance on {display_date}?",
                "a": "\$100",
                "detail": "setup assistance"
            }
        ],
        "chain": [
            {
                "q": f"What was the product image task about on {display_date} and who did the downloading?",
                "a": "The research agent downloaded 56 product reference images. 50 cards on the catalog page were updated with photos, 44 still have placeholders.",
                "keywords": ["research agent", "56", "50", "catalog"]
            },
            {
                "q": f"Why was the router classifier fixed on {display_date}?",
                "a": "The text-blob hint check was matching the word HEARTBEAT in the agent prompt body, routing all heartbeat-triggered conversations to the free tier instead of the configured paid tier.",
                "keywords": ["HEARTBEAT", "hint", "free tier", "classifier"]
            },
            {
                "q": f"What happened with profile TWO on {display_date}?",
                "a": "Profile TWO was confirmed active in the dashboard but was not properly deployed to config.json. The utility tier was still set to test-model-small instead of test-model-medium.",
                "keywords": ["profile TWO", "config.json", "deployed", "utility"]
            }
        ],
        "general": [
            {
                "q": f"What did we do on {display_date}?",
                "a": "Deployed catalog images, fixed router classifier, retired the legacy chat bot",
                "keywords": ["catalog", "classifier", "chat bot"]
            },
            {
                "q": f"Name three things accomplished on {display_date}.",
                "a": "1) Deployed catalog images 2) Fixed router classifier 3) Retired the legacy chat bot",
                "keywords": ["catalog", "classifier", "chat bot"]
            },
            {
                "q": f"What happened with the legacy chat bot on {display_date}?",
                "a": "The legacy chat bot was retired. Telegram integration deactivated, workspace archived, gateway shut down.",
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
