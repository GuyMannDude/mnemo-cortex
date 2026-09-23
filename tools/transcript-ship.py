#!/usr/bin/env python3
"""
transcript-ship.py -- ship client session transcripts to the Mnemo Cortex
archive tier (POST /transcripts, v4.25.0).

Walks every *.jsonl under each --root (a Claude Code projects dir, a Cowork
transcript import dir, a mirror of either), skips files unchanged since the
last run, and POSTs new or grown ones gzip-compressed. The server does the
parsing, redaction and indexing; it tolerates unknown line types and bad
lines, so this script never reads the content.

Session ids come from the file layout Claude Code writes:
  <root>/**/<uuid>.jsonl                          -> <uuid>
  <root>/**/<uuid>/subagents/agent-<id>.jsonl     -> <uuid>_agent-<id>
Any other *.jsonl is skipped and counted (it is not a session transcript).

State (--state, JSON): per file path, the mtime, size and sha256 last
shipped. A file whose mtime and size match is skipped without reading; one
whose bytes hash the same is skipped after hashing. A 409 (the server holds
a different file under that session id) is recorded with the sha, so the
same bytes are not re-posted every run; the file retries once it changes.

Environment:
  MNEMO_URL          default for --server (else http://127.0.0.1:50001)
  MNEMO_AUTH_TOKEN   sent as X-API-KEY when set

Exit codes:
  0  clean: every changed file shipped (or there was nothing to ship)
  1  partial: at least one file failed, conflicted, or hit a capture pause
  2  nothing shipped because the server is down or unreachable

Pure ASCII on purpose (Windows consoles, cp1252 logs).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
AGENT_STEM_RE = re.compile(r"agent-([A-Za-z0-9]{1,64})")

EXIT_CLEAN, EXIT_PARTIAL, EXIT_DOWN = 0, 1, 2


def session_key(path: Path) -> str | None:
    """The transcript session id for a file, or None if it is not one."""
    stem = path.stem
    if UUID_RE.fullmatch(stem):
        return stem
    m = AGENT_STEM_RE.fullmatch(stem)
    if m and path.parent.name == "subagents" and UUID_RE.fullmatch(path.parent.parent.name):
        return f"{path.parent.parent.name}_agent-{m.group(1)}"
    return None


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("files", {})
        return data
    except (OSError, ValueError) as e:
        # A corrupt state file costs one full re-ship (the server answers
        # "unchanged" for everything already archived); say so and go on.
        print(f"WARN state file unreadable ({e}); treating every file as new",
              file=sys.stderr)
        return {"files": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def ascii_only(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Ship Claude Code / Cowork session transcripts to Mnemo's archive tier.")
    ap.add_argument("--root", action="append", required=True,
                    help="directory to walk for *.jsonl (repeatable)")
    ap.add_argument("--agent", required=True, help="tenant (agent_id), e.g. cc, opie")
    ap.add_argument("--host", required=True, help="where the sessions ran, e.g. igor, igor-2, cowork")
    ap.add_argument("--server", default=os.environ.get("MNEMO_URL", "http://127.0.0.1:50001"))
    ap.add_argument("--state", default=None,
                    help="state JSON (default ~/.mnemo-transcript-ship-<agent>-<host>.json)")
    ap.add_argument("--timeout", type=float, default=300.0, help="per-request seconds")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would ship; send nothing, write no state")
    args = ap.parse_args(argv)

    state_path = Path(args.state) if args.state else (
        Path.home() / f".mnemo-transcript-ship-{args.agent}-{args.host}.json")
    state = load_state(state_path)
    files_state: dict = state["files"]

    candidates: list[tuple[Path, str]] = []
    skipped_unrecognized = 0
    for root in args.root:
        r = Path(root).expanduser()
        if not r.is_dir():
            print(f"WARN root not found: {r}", file=sys.stderr)
            continue
        for p in sorted(r.rglob("*.jsonl")):
            if not p.is_file():
                continue
            key = session_key(p)
            if key is None:
                skipped_unrecognized += 1
                continue
            candidates.append((p, key))

    counts = {"archived": 0, "replaced": 0, "unchanged": 0, "skipped_same": 0,
              "conflict": 0, "paused": 0, "failed": 0, "would_ship": 0}
    redactions = 0
    bad_lines = 0
    down = False

    headers = {"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"}
    token = os.environ.get("MNEMO_AUTH_TOKEN", "").strip()
    if token:
        headers["X-API-KEY"] = token

    client = None
    if not args.dry_run:
        client = httpx.Client(base_url=args.server.rstrip("/"), timeout=args.timeout)
        try:
            client.get("/health", timeout=15).raise_for_status()
        except httpx.HTTPError as e:
            print(f"DOWN {args.server}/health: {ascii_only(str(e))}", file=sys.stderr)
            client.close()
            print(f"shipped 0 of {len(candidates)} candidate file(s): server down")
            return EXIT_DOWN

    try:
        for path, key in candidates:
            spath = str(path)
            try:
                st = path.stat()
            except OSError as e:
                print(f"FAIL {key}: stat {ascii_only(str(e))}", file=sys.stderr)
                counts["failed"] += 1
                continue
            prev = files_state.get(spath)
            if prev and prev.get("mtime") == st.st_mtime and prev.get("size") == st.st_size:
                counts["skipped_same"] += 1
                continue
            try:
                raw = path.read_bytes()
            except OSError as e:
                print(f"FAIL {key}: read {ascii_only(str(e))}", file=sys.stderr)
                counts["failed"] += 1
                continue
            sha = hashlib.sha256(raw).hexdigest()
            if prev and prev.get("sha256") == sha:
                prev.update(mtime=st.st_mtime, size=st.st_size)
                if not args.dry_run:
                    save_state(state_path, state)
                counts["skipped_same"] += 1
                continue
            if args.dry_run:
                print(f"WOULD SHIP {key} {len(raw)} bytes {spath}")
                counts["would_ship"] += 1
                continue

            try:
                resp = client.post(
                    "/transcripts",
                    params={"agent_id": args.agent, "host": args.host, "session_id": key},
                    content=gzip.compress(raw), headers=headers)
            except httpx.TransportError as e:
                print(f"FAIL {key}: {ascii_only(str(e))}", file=sys.stderr)
                counts["failed"] += 1
                down = True
                continue

            if resp.status_code == 409:
                detail = resp.json().get("detail", {}) if resp.headers.get(
                    "content-type", "").startswith("application/json") else {}
                print(f"CONFLICT {key}: {ascii_only(str(detail.get('reason', resp.text)))} "
                      f"(server {detail.get('existing_sha256', '?')[:12]}, "
                      f"file {sha[:12]})", file=sys.stderr)
                counts["conflict"] += 1
                files_state[spath] = {"mtime": st.st_mtime, "size": st.st_size,
                                      "sha256": sha, "session_id": key, "status": "conflict",
                                      "at": datetime.now(timezone.utc).isoformat()}
                save_state(state_path, state)
                continue
            if resp.status_code != 200:
                print(f"FAIL {key}: HTTP {resp.status_code} {ascii_only(resp.text[:300])}",
                      file=sys.stderr)
                counts["failed"] += 1
                continue

            body = resp.json()
            status = body.get("status")
            if status == "paused":
                print(f"PAUSED {key}: server capture gate is paused; will retry next run",
                      file=sys.stderr)
                counts["paused"] += 1
                continue
            if status not in ("archived", "replaced", "unchanged"):
                print(f"FAIL {key}: unexpected status {status!r}", file=sys.stderr)
                counts["failed"] += 1
                continue
            manifest = body.get("manifest") or {}
            redactions += int(manifest.get("redactions_applied") or 0) if status != "unchanged" else 0
            bad_lines += int(manifest.get("bad_lines") or 0) if status != "unchanged" else 0
            counts[status] += 1
            files_state[spath] = {"mtime": st.st_mtime, "size": st.st_size, "sha256": sha,
                                  "session_id": key, "status": status,
                                  "at": datetime.now(timezone.utc).isoformat()}
            save_state(state_path, state)
            print(f"{status.upper()} {key} {len(raw)} bytes"
                  + (f" pointer={body.get('pointer')}" if body.get("pointer") else ""))
    finally:
        if client is not None:
            client.close()

    shipped = counts["archived"] + counts["replaced"] + counts["unchanged"]
    trouble = counts["failed"] + counts["conflict"] + counts["paused"]
    print("summary: " + " ".join(f"{k}={v}" for k, v in counts.items())
          + f" not_a_session={skipped_unrecognized} redactions={redactions}"
          + f" bad_lines={bad_lines} candidates={len(candidates)}")
    if down and shipped == 0:
        return EXIT_DOWN
    return EXIT_PARTIAL if trouble else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
