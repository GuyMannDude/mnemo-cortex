"""The harvest window's second gate — one home for both nightly harvesters.

A memory file can LAND inside the window (mtime) while being ABOUT a time
long before it: the 2026-09-12 dream brief specimen was 22 April sessions
archived that night and dreamed as the night's news. The dreamer grew a
second gate on the memory's own ``timestamp`` in 4.21.5; the wiki compiler
kept harvesting on mtime alone, so the two nightly harvests disagreed
(snag wiki-compile-mtime-only-harvest). Both scripts import from here now.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Writers batch (jsonl-sync flushes every 60 s), so a memory stamped just
# before the cutoff can land on disk just after it. One hour of grace keeps
# that memory; months-late archival is still months late.
LATE_ARRIVAL_GRACE = timedelta(hours=1)


def predates_window(timestamp, since: datetime) -> bool:
    """True when a memory's own timestamp is older than the window by more
    than the grace period. Missing or unparseable → False (mtime decides)."""
    if not timestamp:
        return False
    try:
        ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < since - LATE_ARRIVAL_GRACE
