#!/usr/bin/env bash
# mnemo-cc-sync-watchdog — health check for mnemo-cc-sync.service.
#
# Verifies:
#   1. The systemd service is active
#   2. No session JSONL inside the sync's active window carries unsynced
#      backlog older than the flush grace (byte_offset in the offset file
#      has kept up with each file on disk)
#
# Why backlog instead of mtime/last-post: an open-but-idle Claude Code
# terminal gets hourly housekeeping appends that bump the JSONL mtime without
# producing anything postable. The sync correctly consumes those lines and
# advances byte_offset WITHOUT posting, so "mtime fresh but no recent post"
# is normal, not a failure (2026-07-08: 13 false Discord pages in one day).
# A genuinely stuck sync shows up here as backlog bytes that survive past the
# idle-flush window — on ANY active file, not just the newest, since the sync
# tracks a per-file offset. Tradeoff: a sync that wedges mid-session isn't
# flagged until that file next goes quiet for FLUSH_GRACE_S — acceptable,
# sessions pause constantly and the old heuristic's false pages cost more
# than the shorter detection gap bought.
#
# Exits non-zero on failure so cron / scheduler can alert. Designed to plug
# into any monitoring tool that watches command exit codes — CronAlarm,
# systemd OnFailure=, healthchecks.io, or a plain cron + email. Every failure
# path prints a diagnostic — an exit 1 with no explanation is itself a bug.
#
# Configuration (env vars, all optional):
#   MNEMO_CC_SERVICE        systemd unit name (default: mnemo-cc-sync.service)
#   MNEMO_CC_OFFSET_FILE    Sync offset state file (default: ~/.mnemo-cc/cc-sync.offset.json)
#   MNEMO_CC_SESSIONS_DIR   Where Claude Code stores .jsonl files (default: ~/.claude/projects)
#   MNEMO_CC_ACTIVE_HOURS   Sync's active window — files idle longer are ignored,
#                           matching the sync's own scan cutoff (default: 24)
#   MNEMO_CC_FLUSH_GRACE_S  Seconds an idle JSONL may hold unsynced bytes before
#                           that counts as stuck (default: 600 = idle-flush 300s
#                           + several 60s sync cycles of margin)

SERVICE=${MNEMO_CC_SERVICE:-mnemo-cc-sync.service}

# 1) Service must be active
if ! systemctl --user is-active --quiet "$SERVICE"; then
    echo "WATCHDOG FAIL: $SERVICE is not active"
    systemctl --user status "$SERVICE" --no-pager 2>&1 | head -10
    exit 1
fi

# 2) Backlog evaluation — python owns the whole walk so path oddities,
#    malformed state, and races all fail with a printed diagnostic.
MNEMO_CC_SERVICE="$SERVICE" python3 - <<'PY'
import json, os, sys, time

service = os.environ["MNEMO_CC_SERVICE"]
home = os.path.expanduser("~")
offset_path = os.environ.get("MNEMO_CC_OFFSET_FILE",
                             os.path.join(home, ".mnemo-cc", "cc-sync.offset.json"))
sessions_dir = os.environ.get("MNEMO_CC_SESSIONS_DIR",
                              os.path.join(home, ".claude", "projects"))
active_hours = float(os.environ.get("MNEMO_CC_ACTIVE_HOURS", "24"))
grace_s = float(os.environ.get("MNEMO_CC_FLUSH_GRACE_S", "600"))

def fail(msg):
    print(f"WATCHDOG FAIL: {msg}")
    sys.exit(1)

now = time.time()
cutoff = now - active_hours * 3600

# Files the sync itself considers active (same cutoff rule as mnemo-cc-sync.py)
active = []
for root, _dirs, names in os.walk(sessions_dir):
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(root, name)
        try:
            st = os.stat(path)
        except OSError:
            continue  # vanished mid-walk — rotation race, not a failure
        if st.st_mtime >= cutoff:
            active.append((path, st))

if not active:
    print(f"OK: {service} active, no Claude Code sessions in the last {active_hours:g}h")
    sys.exit(0)

if not os.path.isfile(offset_path):
    fail(f"active sessions exist but no offset file at {offset_path} — sync has never completed a cycle")
try:
    with open(offset_path) as fh:
        files = json.load(fh).get("files", {})
except (json.JSONDecodeError, OSError) as e:
    fail(f"offset file {offset_path} unreadable ({e}) — sync state is corrupt")

stuck = []
pending = 0
for path, st in active:
    rel = os.path.relpath(path, sessions_dir).replace(os.sep, "/")
    age = now - st.st_mtime
    entry = files.get(rel)

    if entry is None:
        # The sync scans every cycle; an active file still unregistered
        # past the grace means the scanner isn't picking it up.
        if age > grace_s:
            stuck.append(f"{rel}: {age:.0f}s old, no offset entry — sync isn't scanning it")
        continue

    backlog = st.st_size - int(entry.get("byte_offset", 0))
    if backlog <= 0:
        continue
    if age <= grace_s:
        pending += 1  # accumulating between cycles / deferred batch — normal
        continue

    # Idle past grace with backlog. One legit case remains: a torn final
    # line (no trailing newline) that the sync correctly refuses to consume.
    try:
        with open(path, "rb") as fh:
            fh.seek(int(entry.get("byte_offset", 0)))
            tail = fh.read(backlog)
    except OSError:
        continue  # vanished mid-check — rotation race
    if b"\n" not in tail:
        continue  # torn line only — nothing consumable, not a wedge
    stuck.append(f"{rel}: {backlog}B unsynced despite {age:.0f}s of idle")

if stuck:
    print(f"WATCHDOG FAIL: sync is stuck on {len(stuck)} file(s):")
    for line in stuck:
        print(f"  {line}")
    print(f"  offset: {offset_path}")
    sys.exit(1)

note = f", {pending} file(s) accumulating within flush grace" if pending else ""
print(f"OK: {service} active, no unsynced backlog on {len(active)} active file(s){note}")
PY
