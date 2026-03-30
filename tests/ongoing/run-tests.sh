#!/usr/bin/env bash
# run-tests.sh — Query Mnemo Cortex with test questions and score the answers
# Usage: ./run-tests.sh [YYYY-MM-DD] [agent_id]
# Defaults to yesterday's date and agent_id "sparky-test"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MNEMO_URL="${MNEMO_URL:-http://artforge:50001}"
DATE="${1:-$(date -d 'yesterday' +%Y-%m-%d 2>/dev/null || date -v-1d +%Y-%m-%d)}"
AGENT_ID="${2:-sparky-test}"
QUESTIONS_FILE="${SCRIPT_DIR}/test-questions.json"
RESULTS_FILE="${SCRIPT_DIR}/mnemo-test-results.md"
RESULTS_JSON="${SCRIPT_DIR}/test-results.jsonl"

echo "=== Mnemo Cortex Test Runner ==="
echo "Testing date: $DATE"
echo "Agent:        $AGENT_ID"
echo "Endpoint:     $MNEMO_URL"
echo ""

if [ ! -f "$QUESTIONS_FILE" ]; then
    echo "ERROR: No questions file found at $QUESTIONS_FILE"
    echo "Run daily-feed.sh first to generate questions."
    exit 1
fi

# Run the test via Python for JSON handling and scoring
export MNEMO_URL DATE AGENT_ID QUESTIONS_FILE RESULTS_FILE RESULTS_JSON
python3 << 'PYEOF'
import json
import os
import sys
import time
from datetime import datetime, timezone

MNEMO_URL = os.environ["MNEMO_URL"]
DATE = os.environ["DATE"]
AGENT_ID = os.environ["AGENT_ID"]
QUESTIONS_FILE = os.environ["QUESTIONS_FILE"]
RESULTS_FILE = os.environ["RESULTS_FILE"]
RESULTS_JSON = os.environ["RESULTS_JSON"]

with open(QUESTIONS_FILE) as f:
    bank = json.load(f)

if DATE not in bank.get("dates", {}):
    print(f"ERROR: No questions found for date {DATE}")
    print(f"Available dates: {list(bank.get('dates', {}).keys())}")
    sys.exit(1)

day = bank["dates"][DATE]
results = {
    "date": DATE,
    "tested_at": datetime.now(timezone.utc).isoformat(),
    "agent_id": AGENT_ID,
    "levels": {}
}

total_correct = 0
total_partial = 0
total_wrong = 0
total_notfound = 0

for level in ["needle", "chain", "general"]:
    questions = day["questions"].get(level, [])
    if not questions:
        continue

    print(f"\n--- {level.upper()} ({len(questions)} questions) ---")
    level_results = []

    for i, q in enumerate(questions):
        question = q["q"]
        expected = q.get("a", "")
        keywords = q.get("keywords", [])
        detail = q.get("detail", "")

        # Query mnemo cortex /context endpoint
        import urllib.request
        req_data = json.dumps({
            "prompt": question,
            "agent_id": AGENT_ID,
            "persona": "strict",
            "max_results": 10
        }).encode()

        start = time.time()
        try:
            req = urllib.request.Request(
                f"{MNEMO_URL}/context",
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                response_data = json.loads(resp.read())
                elapsed_ms = int((time.time() - start) * 1000)
        except Exception as e:
            print(f"  Q{i+1}: ERROR - {e}")
            level_results.append({
                "question": question,
                "expected": expected,
                "actual": f"ERROR: {e}",
                "score": "error",
                "latency_ms": 0
            })
            total_wrong += 1
            continue

        # Extract the context text from response
        # v1 API returns {"chunks": [{"content": "...", "relevance": 0.5, ...}, ...]}
        context_text = ""
        if isinstance(response_data, dict):
            chunks = response_data.get("chunks", [])
            if chunks and isinstance(chunks, list):
                # Concatenate all chunk content, sorted by relevance
                parts = []
                for chunk in chunks:
                    if isinstance(chunk, dict) and chunk.get("content"):
                        parts.append(chunk["content"])
                context_text = "\n".join(parts)
            elif "context" in response_data:
                ctx = response_data["context"]
                context_text = ctx if isinstance(ctx, str) else json.dumps(ctx)
        elif isinstance(response_data, str):
            context_text = response_data
        else:
            context_text = str(response_data)

        context_lower = context_text.lower()

        # Score the response
        if level == "needle":
            # For needle: check if the exact answer appears in context
            answer_lower = expected.lower().replace("$", "").replace("°f", "")
            # Extract core number/fact from expected answer
            if answer_lower in context_lower:
                score = "correct"
                total_correct += 1
            elif any(word.lower() in context_lower for word in expected.split() if len(word) > 2):
                score = "partial"
                total_partial += 1
            elif context_text.strip():
                score = "wrong"
                total_wrong += 1
            else:
                score = "not_found"
                total_notfound += 1
        else:
            # For chain/general: check keyword coverage
            if keywords:
                hits = sum(1 for kw in keywords if kw.lower() in context_lower)
                ratio = hits / len(keywords)
                if ratio >= 0.8:
                    score = "correct"
                    total_correct += 1
                elif ratio >= 0.4:
                    score = "partial"
                    total_partial += 1
                elif context_text.strip():
                    score = "wrong"
                    total_wrong += 1
                else:
                    score = "not_found"
                    total_notfound += 1
            else:
                # Fallback: check if expected answer words appear
                words = [w for w in expected.lower().split() if len(w) > 3]
                hits = sum(1 for w in words if w in context_lower)
                ratio = hits / max(len(words), 1)
                if ratio >= 0.6:
                    score = "correct"
                    total_correct += 1
                elif ratio >= 0.3:
                    score = "partial"
                    total_partial += 1
                else:
                    score = "wrong"
                    total_wrong += 1

        icon = {"correct": "✓", "partial": "~", "wrong": "✗", "not_found": "∅", "error": "!"}
        print(f"  {icon.get(score, '?')} Q{i+1}: {question[:70]}...")
        print(f"    Expected: {expected[:80]}")
        print(f"    Score: {score} | {elapsed_ms}ms | Context: {len(context_text)} chars")

        level_results.append({
            "question": question,
            "expected": expected,
            "actual_context_chars": len(context_text),
            "actual_preview": context_text[:200],
            "score": score,
            "latency_ms": elapsed_ms
        })

    results["levels"][level] = level_results

# Summary
total = total_correct + total_partial + total_wrong + total_notfound
print(f"\n=== RESULTS for {DATE} ===")
print(f"Correct:  {total_correct}/{total}")
print(f"Partial:  {total_partial}/{total}")
print(f"Wrong:    {total_wrong}/{total}")
print(f"NotFound: {total_notfound}/{total}")
if total > 0:
    accuracy = (total_correct + total_partial * 0.5) / total * 100
    print(f"Accuracy: {accuracy:.1f}%")
    results["accuracy_pct"] = round(accuracy, 1)

results["summary"] = {
    "total": total,
    "correct": total_correct,
    "partial": total_partial,
    "wrong": total_wrong,
    "not_found": total_notfound
}

# Append to JSONL log
with open(RESULTS_JSON, "a") as f:
    f.write(json.dumps(results) + "\n")

# Update markdown results file
header_needed = not os.path.exists(RESULTS_FILE) or os.path.getsize(RESULTS_FILE) == 0

with open(RESULTS_FILE, "a") as f:
    if header_needed:
        f.write("# Mnemo Cortex — Ongoing Test Results\n\n")
        f.write("| Date | Level | Q# | Question | Expected | Score | Latency |\n")
        f.write("|------|-------|----|----------|----------|-------|---------|\n")

    for level, level_results in results["levels"].items():
        for i, r in enumerate(level_results):
            q_short = r["question"][:50].replace("|", "\\|")
            a_short = r["expected"][:40].replace("|", "\\|")
            f.write(f"| {DATE} | {level} | {i+1} | {q_short} | {a_short} | {r['score']} | {r['latency_ms']}ms |\n")

    f.write(f"\n**{DATE} Summary:** {total_correct} correct, {total_partial} partial, {total_wrong} wrong, {total_notfound} not found — **{results.get('accuracy_pct', 0)}%**\n\n")

print(f"\nResults saved to: {RESULTS_FILE}")
print(f"JSON log: {RESULTS_JSON}")
PYEOF
