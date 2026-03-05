"""
Mnemo Cortex Session Manager v0.4.0
The Live Wire — captures every prompt/response as it happens.
No manual save required. If the plug gets pulled, everything is already on disk.

Lifecycle:
  Hot (days 1-3):   Raw session logs, instantly searchable
  Warm (days 4-29): Summarized, embedded, indexed in L2. Raw logs compressed.
  Cold (day 30+):   Compressed archive, L3 scan only.
"""

import json
import gzip
import logging
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, field

log = logging.getLogger("agentb.sessions")


@dataclass
class SessionEntry:
    """A single prompt/response exchange."""
    timestamp: str
    prompt: str
    response: str
    metadata: dict = field(default_factory=dict)


@dataclass
class SessionConfig:
    """Session lifecycle configuration."""
    hot_days: int = 3            # days before archival
    warm_days: int = 30          # days before cold storage
    max_session_gap_minutes: int = 60  # new session after this gap
    auto_summarize: bool = True  # summarize on archival
    max_hot_entries: int = 500   # safety cap per session file


class SessionManager:
    """
    Manages live session capture and lifecycle.
    
    Directory structure per agent:
      sessions/
        hot/
          2026-03-05_session-abc123.jsonl     ← live, appending
          2026-03-05_session-def456.jsonl
        warm/
          2026-03-02_session-xyz789.json      ← summarized
          2026-03-02_session-xyz789.jsonl.gz  ← compressed raw
        cold/
          2026-02-01_session-old001.jsonl.gz  ← deep archive
    """

    def __init__(self, data_dir: Path, config: Optional[SessionConfig] = None):
        self.data_dir = data_dir
        self.config = config or SessionConfig()
        self.hot_dir = data_dir / "sessions" / "hot"
        self.warm_dir = data_dir / "sessions" / "warm"
        self.cold_dir = data_dir / "sessions" / "cold"

        for d in [self.hot_dir, self.warm_dir, self.cold_dir]:
            d.mkdir(parents=True, exist_ok=True)

        self._current_session_id: Optional[str] = None
        self._current_session_file: Optional[Path] = None
        self._last_ingest_time: float = 0
        self._entry_count: int = 0

    def _generate_session_id(self) -> str:
        """Generate a new session ID based on timestamp."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        suffix = hashlib.sha256(str(time.time()).encode()).hexdigest()[:6]
        return f"{ts}_{suffix}"

    def _should_start_new_session(self) -> bool:
        """Check if we need a new session (gap too long or no current session)."""
        if not self._current_session_id:
            return True
        if self._last_ingest_time == 0:
            return True
        gap = time.time() - self._last_ingest_time
        return gap > (self.config.max_session_gap_minutes * 60)

    def _start_session(self) -> str:
        """Start a new hot session file."""
        session_id = self._generate_session_id()
        self._current_session_id = session_id
        self._current_session_file = self.hot_dir / f"{session_id}.jsonl"
        self._entry_count = 0

        # Write session header
        header = {
            "_type": "session_start",
            "session_id": session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self._current_session_file, "a") as f:
            f.write(json.dumps(header) + "\n")

        log.info(f"Session started: {session_id}")
        return session_id

    def ingest(self, prompt: str, response: str, metadata: Optional[dict] = None) -> dict:
        """
        Ingest a prompt/response pair into the current session.
        This is the Live Wire — fast, append-only, crash-safe.
        Returns session info.
        """
        if self._should_start_new_session():
            self._start_session()

        # Safety cap
        if self._entry_count >= self.config.max_hot_entries:
            self._start_session()

        entry = {
            "_type": "exchange",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prompt": prompt,
            "response": response,
            "metadata": metadata or {},
        }

        # Append-only write — crash-safe (worst case: lose last partial line)
        with open(self._current_session_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()  # force to disk

        self._last_ingest_time = time.time()
        self._entry_count += 1

        return {
            "session_id": self._current_session_id,
            "entry_number": self._entry_count,
            "session_file": str(self._current_session_file.name),
        }

    def get_hot_sessions(self) -> list[dict]:
        """List all hot session files with stats."""
        sessions = []
        for f in sorted(self.hot_dir.glob("*.jsonl"), reverse=True):
            entries = self._count_entries(f)
            stat = f.stat()
            sessions.append({
                "session_id": f.stem,
                "file": f.name,
                "entries": entries,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "tier": "hot",
            })
        return sessions

    def get_warm_sessions(self) -> list[dict]:
        """List warm (summarized) sessions."""
        sessions = []
        for f in sorted(self.warm_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text())
                sessions.append({
                    "session_id": f.stem,
                    "summary": data.get("summary", "")[:100],
                    "key_facts": len(data.get("key_facts", [])),
                    "tier": "warm",
                })
            except Exception:
                pass
        return sessions

    def search_hot(self, query: str, max_results: int = 10) -> list[dict]:
        """
        Search hot session logs via simple text matching.
        Fast but not semantic — for hot data, speed > precision.
        """
        query_lower = query.lower()
        results = []

        for session_file in sorted(self.hot_dir.glob("*.jsonl"), reverse=True):
            try:
                with open(session_file) as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            if entry.get("_type") != "exchange":
                                continue
                            text = f"{entry.get('prompt', '')} {entry.get('response', '')}"
                            if query_lower in text.lower():
                                results.append({
                                    "session_id": session_file.stem,
                                    "timestamp": entry.get("timestamp", ""),
                                    "prompt": entry["prompt"][:200],
                                    "response": entry["response"][:200],
                                    "tier": "hot",
                                })
                                if len(results) >= max_results:
                                    return results
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                log.warning(f"Hot search error on {session_file}: {e}")

        return results

    def get_session_transcript(self, session_id: str) -> list[dict]:
        """Get full transcript of a session."""
        # Check hot
        hot_file = self.hot_dir / f"{session_id}.jsonl"
        if hot_file.exists():
            return self._read_jsonl(hot_file)

        # Check warm (compressed)
        warm_gz = self.warm_dir / f"{session_id}.jsonl.gz"
        if warm_gz.exists():
            return self._read_jsonl_gz(warm_gz)

        # Check cold
        cold_gz = self.cold_dir / f"{session_id}.jsonl.gz"
        if cold_gz.exists():
            return self._read_jsonl_gz(cold_gz)

        return []

    def archive_hot_sessions(self, summarize_fn=None) -> list[dict]:
        """
        Move expired hot sessions to warm storage.
        Optionally summarize them using the reasoning provider.
        Returns list of archived sessions.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.hot_days)
        archived = []

        for session_file in list(self.hot_dir.glob("*.jsonl")):
            # Skip current active session
            if self._current_session_file and session_file == self._current_session_file:
                continue

            stat = session_file.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if modified >= cutoff:
                continue

            session_id = session_file.stem
            entries = self._read_jsonl(session_file)
            exchanges = [e for e in entries if e.get("_type") == "exchange"]

            if not exchanges:
                session_file.unlink()
                continue

            # Compress raw log to warm
            gz_path = self.warm_dir / f"{session_id}.jsonl.gz"
            with gzip.open(gz_path, "wt", encoding="utf-8") as gz:
                for entry in entries:
                    gz.write(json.dumps(entry) + "\n")

            # Generate summary if function provided
            summary_data = {
                "session_id": session_id,
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "exchange_count": len(exchanges),
                "first_exchange": exchanges[0].get("timestamp", ""),
                "last_exchange": exchanges[-1].get("timestamp", ""),
                "summary": "",
                "key_facts": [],
            }

            if summarize_fn and self.config.auto_summarize:
                try:
                    transcript = "\n".join(
                        f"User: {e['prompt']}\nAssistant: {e['response']}"
                        for e in exchanges[-20:]  # last 20 exchanges max
                    )
                    summary_result = summarize_fn(transcript)
                    if isinstance(summary_result, dict):
                        summary_data["summary"] = summary_result.get("summary", "")
                        summary_data["key_facts"] = summary_result.get("key_facts", [])
                    else:
                        summary_data["summary"] = str(summary_result)
                except Exception as e:
                    log.warning(f"Summarization failed for {session_id}: {e}")
                    # Still archive, just without summary
                    summary_data["summary"] = f"[Auto-summary failed: {str(e)[:50]}]"

            # Save summary
            summary_path = self.warm_dir / f"{session_id}.json"
            summary_path.write_text(json.dumps(summary_data, indent=2))

            # Remove hot file
            session_file.unlink()
            archived.append(summary_data)
            log.info(f"Archived session {session_id}: {len(exchanges)} exchanges → warm")

        return archived

    def archive_warm_to_cold(self) -> list[str]:
        """Move expired warm sessions to cold storage."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.warm_days)
        moved = []

        for gz_file in list(self.warm_dir.glob("*.jsonl.gz")):
            stat = gz_file.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if modified >= cutoff:
                continue

            session_id = gz_file.stem.replace(".jsonl", "")
            dest = self.cold_dir / gz_file.name
            gz_file.rename(dest)

            # Move summary too if it exists
            summary = self.warm_dir / f"{session_id}.json"
            if summary.exists():
                summary.rename(self.cold_dir / summary.name)

            moved.append(session_id)
            log.info(f"Cold archived: {session_id}")

        return moved

    def get_recent_context(self, n_exchanges: int = 20) -> str:
        """
        Get the most recent N exchanges across hot sessions.
        Used for context injection on bootstrap.
        """
        all_exchanges = []

        for session_file in sorted(self.hot_dir.glob("*.jsonl"), reverse=True):
            entries = self._read_jsonl(session_file)
            exchanges = [e for e in entries if e.get("_type") == "exchange"]
            all_exchanges.extend(exchanges)
            if len(all_exchanges) >= n_exchanges:
                break

        # Sort by timestamp descending, take most recent
        all_exchanges.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        recent = all_exchanges[:n_exchanges]
        recent.reverse()  # chronological order

        if not recent:
            return ""

        lines = []
        for e in recent:
            ts = e.get("timestamp", "")[:16]
            lines.append(f"[{ts}] User: {e.get('prompt', '')[:300]}")
            lines.append(f"[{ts}] Agent: {e.get('response', '')[:300]}")
            lines.append("")

        return "\n".join(lines)

    @property
    def stats(self) -> dict:
        """Get session storage stats."""
        hot_files = list(self.hot_dir.glob("*.jsonl"))
        warm_files = list(self.warm_dir.glob("*.json"))
        cold_files = list(self.cold_dir.glob("*.jsonl.gz"))

        hot_size = sum(f.stat().st_size for f in hot_files)
        warm_size = sum(f.stat().st_size for f in list(self.warm_dir.glob("*")))
        cold_size = sum(f.stat().st_size for f in cold_files)

        return {
            "hot_sessions": len(hot_files),
            "warm_sessions": len(warm_files),
            "cold_sessions": len(cold_files),
            "hot_size_mb": round(hot_size / (1024 * 1024), 2),
            "warm_size_mb": round(warm_size / (1024 * 1024), 2),
            "cold_size_mb": round(cold_size / (1024 * 1024), 2),
            "current_session": self._current_session_id,
            "current_entries": self._entry_count,
        }

    # ── Internal helpers ──

    def _count_entries(self, path: Path) -> int:
        count = 0
        try:
            with open(path) as f:
                for line in f:
                    try:
                        if json.loads(line).get("_type") == "exchange":
                            count += 1
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
        return count

    def _read_jsonl(self, path: Path) -> list[dict]:
        entries = []
        try:
            with open(path) as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
        return entries

    def _read_jsonl_gz(self, path: Path) -> list[dict]:
        entries = []
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
        return entries
