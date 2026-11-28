"""Memory Ledger — a tamper-evident chain over what the store was told.

v4.20.0. Every memory record has two halves. The TESTIMONY is what the
person or agent said: the summary, the key facts, the decisions, who said
it and when. The FILING is what the store did with it afterwards: category,
tags, superseded_by, reclassification marks. The filing is allowed to
change — the dreamer reclassifies, a later write supersedes. The testimony
is not. Once a memory is saved, its words must never silently become other
words.

The ledger seals the testimony. One append-only ``ledger.jsonl`` per tenant,
next to ``memory/``. Each entry carries the SHA-256 of the sealed fields,
the hash of the entry before it, and its own hash over the whole entry.
Change one record on disk and ``verify`` reports it ``altered``. Edit or
drop a ledger line and the chain reports ``broken`` at that sequence.
There is no way to rewrite the past without the rewrite showing.

Sealing is honest about its limits. It is local evidence, not third-party
proof: whoever can write the memory files can also rewrite the ledger from
scratch. What it catches is the ordinary disaster — a bad migration, a
half-finished script, a sync that mangled a file, a stick that carried
something the origin never wrote — and it catches it every time.

States a record can be in:

- ``sealed``   — on disk and its testimony matches its latest ledger entry
- ``altered``  — on disk, but the testimony differs from what was sealed
- ``missing``  — sealed, but the file is gone (memories are never deleted:
                 demotion keeps the JSON — a missing sealed file is news)
- ``unsealed`` — on disk with no ledger entry: pre-ledger stores, records
                 carried in by a Cortex Stick, or written by a tool that
                 bypassed the server. ``seal_unsealed`` adopts them.
- ``disputed`` — (broken chain only) on disk and matching an entry the
                 chain cannot vouch for. A hash chain cannot say WHICH of
                 two neighbours was rewritten, only that the link between
                 them broke — so everything from the break onward is
                 evidence, not proof. Matches → disputed; differs →
                 altered.

Chain states: ``intact`` / ``broken`` / ``empty``.

The idea is borrowed with credit from MAYA Memory Lane's sealed blocks
(SHA-256 fingerprints folding into a chain). Built here from zero to fit
Mnemo's per-record JSON store.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

LEDGER_FILENAME = "ledger.jsonl"
GENESIS = "0" * 64

# The testimony: the fields a save promises never to change. Everything
# else on a record is filing and may legitimately move after the fact.
SEALED_FIELDS = (
    "id", "session_id", "agent_id", "summary", "key_facts",
    "decisions_made", "projects_referenced", "timestamp",
)


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def content_hash(record: dict) -> str:
    """SHA-256 over the testimony fields of a memory record, in canonical
    form. Filing fields do not participate, so a reclassification or a
    supersede mark leaves the hash unchanged."""
    return hashlib.sha256(
        _canonical({k: record.get(k) for k in SEALED_FIELDS})).hexdigest()


def entry_hash(entry: dict) -> str:
    """Hash of an entry over every field except its own ``hash``."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canonical(body)).hexdigest()


@dataclass
class VerifyReport:
    chain: str                       # intact | broken | empty
    entries: int = 0
    broken_at: Optional[int] = None  # seq of the first bad entry
    reason: Optional[str] = None     # why the chain is broken
    sealed: int = 0
    altered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unsealed: list[str] = field(default_factory=list)
    disputed: list[str] = field(default_factory=list)  # only ever non-empty on a broken chain
    # Item 17 (c): ids the trusted chain sealed more than once with DIFFERENT
    # testimony (any SEALED_FIELDS value, timestamp included). Report-only: a fixed-id writer (an archived session re-summarised,
    # a re-send whose sealed fields outside the id changed) did it in the open,
    # through seal(), so ok does not change. [{memory_id, seqs, distinct}]
    resealed: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.chain in ("intact", "empty") and not self.altered and not self.missing

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "chain": self.chain,
            "entries": self.entries,
            "broken_at": self.broken_at,
            "reason": self.reason,
            "sealed": self.sealed,
            "altered": sorted(self.altered),
            "missing": sorted(self.missing),
            "unsealed": sorted(self.unsealed),
            "disputed": sorted(self.disputed),
            "resealed": sorted(self.resealed, key=lambda r: r["memory_id"]),
        }


class LedgerBroken(RuntimeError):
    """The chain does not verify; adopting records onto it is refused."""

    def __init__(self, report: "VerifyReport"):
        super().__init__(f"ledger chain {report.chain} at seq {report.broken_at}: {report.reason}")
        self.report = report


class Ledger:
    """One append-only chain. Use :func:`get_ledger` rather than
    constructing directly — two instances on one file would each hold
    their own idea of the tail and fork the chain."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._seq = 0
        self._tail = GENESIS
        self._load_tail()

    # ── reading ──

    def _load_tail(self) -> None:
        """Adopt the last WELL-FORMED line as the tail. A torn final line
        (crash mid-append) is treated as absent; verify() still reports it."""
        self._seq, self._tail = 0, GENESIS
        for entry in self.entries():
            if entry is None:
                continue
            self._seq = int(entry.get("seq", self._seq))
            self._tail = str(entry.get("hash", self._tail))

    def entries(self) -> Iterator[Optional[dict]]:
        """Yield each line parsed, or ``None`` for a line that is not JSON."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    yield None
                    continue
                yield obj if isinstance(obj, dict) else None

    def latest_by_memory(self) -> dict[str, dict]:
        """memory_id → its most recent entry (any op)."""
        out: dict[str, dict] = {}
        for e in self.entries():
            if e and e.get("memory_id"):
                out[e["memory_id"]] = e
        return out

    # ── writing ──

    def seal(self, record: dict, op: str = "save") -> dict:
        """Append one entry sealing ``record``'s testimony. Durable before it
        returns (flush + fsync). Thread-safe within the process."""
        memory_id = record.get("id")
        if not memory_id:
            raise ValueError("record has no id")
        with self._lock:
            entry = {
                "seq": self._seq + 1,
                "ts": time.time(),
                "op": op,
                "memory_id": memory_id,
                "content_sha256": content_hash(record),
                "prev": self._tail,
            }
            entry["hash"] = entry_hash(entry)
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._quarantine_torn_tail():
                # The tail we were chaining from may have been the fragment.
                self._load_tail()
                entry["seq"], entry["prev"] = self._seq + 1, self._tail
                entry["hash"] = entry_hash(entry)
                line = json.dumps(entry, ensure_ascii=False) + "\n"
            # A complete final line with no terminator (an editor, a
            # "\n".join rewrite) is NOT a crash fragment: keep it, and
            # start this entry on its own line.
            _, tail = self._torn_tail()
            with self.path.open("a", encoding="utf-8") as fh:
                if tail:
                    fh.write("\n")
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._seq, self._tail = entry["seq"], entry["hash"]
            return entry

    def _torn_tail(self) -> tuple[int, bytes]:
        """(offset, fragment) of an unterminated final line — the one shape
        a crash mid-append can leave — or (size, b"") when the file is
        clean. O(1): reads backwards from the end, never the whole file."""
        if not self.path.exists():
            return 0, b""
        size = self.path.stat().st_size
        if size == 0:
            return 0, b""
        with self.path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) == b"\n":
                return size, b""
            # Walk back in blocks to the last newline.
            pos, chunk = size, b""
            while pos > 0:
                step = min(4096, pos)
                pos -= step
                fh.seek(pos)
                chunk = fh.read(step) + chunk
                nl = chunk.rfind(b"\n")
                if nl != -1:
                    return pos + nl + 1, chunk[nl + 1:]
            return 0, chunk

    def _quarantine_torn_tail(self) -> bool:
        """Move a crash fragment out of the chain into ``ledger.torn`` and
        truncate the file back to its last complete line. The fragment was
        never a valid entry (no hash, so nothing verifiable is lost) and
        its record, if any, simply reads unsealed until adopted. Only an
        UNTERMINATED tail is touched: a bad line in the middle of the file
        is tampering or truncation, and stays broken. Caller holds the
        lock. Returns True when something was quarantined. A complete
        entry that merely lacks its newline is left alone."""
        offset, fragment = self._torn_tail()
        if not fragment:
            return False
        try:
            if isinstance(json.loads(fragment), dict):
                return False       # complete entry, just unterminated — not a crash
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        torn = self.path.with_suffix(".torn")
        log.warning(f"Ledger {self.path}: quarantining a torn tail "
                    f"({len(fragment)} bytes at offset {offset}) to {torn}")
        with torn.open("ab") as fh:
            fh.write(f"# quarantined {time.time():.3f} from offset {offset}\n".encode())
            fh.write(fragment + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        with self.path.open("r+b") as fh:
            fh.truncate(offset)
            fh.flush()
            os.fsync(fh.fileno())
        return True

    def seal_unsealed(self, memory_dir: Path) -> list[str]:
        """Adopt every record in ``memory_dir`` the ledger has never seen.
        Returns the ids sealed. Altered records are NOT re-sealed — that
        would launder a tamper into a fresh seal; verify() keeps naming
        them until a human decides. Refused (LedgerBroken) while the chain
        does not verify: with a line deleted, a forged record's seal is
        gone too, and "adopt the unsealed" would seal the forgery. A crash
        fragment on the tail is not that — it is quarantined first, so one
        crash never bricks a tenant's adoption."""
        with self._lock:
            self._quarantine_torn_tail()
            self._load_tail()
        report = self.verify(memory_dir)
        if report.chain == "broken":
            raise LedgerBroken(report)
        known = self.latest_by_memory()
        sealed: list[str] = []
        for path in sorted(Path(memory_dir).glob("*.json")):
            record = _read_record(path)
            if record is None or not record.get("id"):
                continue
            if record["id"] in known:
                continue
            self.seal(record, op="adopt")
            sealed.append(record["id"])
        return sealed

    # ── verifying ──

    def verify(self, memory_dir: Path) -> VerifyReport:
        """Walk the chain, then compare every record on disk against it.

        The chain is trusted up to the first break. A hash chain cannot say
        which of two neighbours was rewritten — only that the link between
        them broke — so a prev-mismatch at seq k puts BOTH k-1 and k on the
        untrusted side. Entries on the trusted side give verdicts
        (sealed / altered / missing); entries on the untrusted side can
        only dispute (disputed / altered / missing), and never overrule a
        trusted verdict for the same record."""
        report = VerifyReport(chain="empty")
        trusted: dict[str, dict] = {}
        untrusted: dict[str, dict] = {}
        prev, expect_seq = GENESIS, 1
        seen: list[dict] = []                # parseable entries, in FILE order
        n_trusted: Optional[int] = None      # how many LEADING entries the chain vouches for

        def _break(at, reason, demote_prev=False):
            nonlocal n_trusted
            if report.chain != "broken":
                report.chain, report.broken_at, report.reason = "broken", at, reason
                # Positional, never by seq value: an entry appended after
                # the break with a replayed low seq must not walk back into
                # the prefix. `seen` holds exactly the validated entries here.
                n_trusted = len(seen) - (1 if demote_prev else 0)

        for entry in self.entries():
            report.entries += 1
            if entry is None:
                _break(expect_seq, "unparseable line")
                continue
            seq = entry.get("seq")
            if seq != expect_seq:
                _break(expect_seq, f"sequence gap: expected {expect_seq}, found {seq}")
            elif entry.get("prev") != prev:
                # Either k-1 was rewritten (its hash changed) or k was:
                # both go to the untrusted side.
                _break(seq, "prev hash does not match the entry before it", demote_prev=True)
            elif entry.get("hash") != entry_hash(entry):
                _break(seq, "entry hash does not match its contents")
            if isinstance(seq, int):
                expect_seq = seq + 1
            prev = str(entry.get("hash", prev))
            seen.append(entry)
        if report.chain != "broken" and report.entries:
            report.chain = "intact"

        # The leading n_trusted entries passed every check at their
        # position (the break is the FIRST failure). Everything after is
        # untrusted; last-wins within a side.
        cut = len(seen) if n_trusted is None else max(0, n_trusted)
        sealings: dict[str, list[tuple[int, str]]] = {}
        for entry in seen[:cut]:
            if entry.get("memory_id") and entry.get("content_sha256"):
                sealings.setdefault(entry["memory_id"], []).append(
                    (entry.get("seq"), entry["content_sha256"]))
        for rid, seals in sealings.items():
            distinct = len({h for _, h in seals})
            if distinct > 1:
                report.resealed.append(
                    {"memory_id": rid, "seqs": [q for q, _ in seals], "distinct": distinct})
        for i, entry in enumerate(seen):
            if not (entry.get("memory_id") and entry.get("content_sha256")):
                continue
            (trusted if i < cut else untrusted)[entry["memory_id"]] = entry

        memory_dir = Path(memory_dir)
        on_disk: dict[str, Optional[dict]] = {}
        for path in memory_dir.glob("*.json"):
            record = _read_record(path)
            rid = (record or {}).get("id") or path.stem
            on_disk[rid] = record

        def _matches(rid, entry) -> bool:
            record = on_disk.get(rid)
            return record is not None and content_hash(record) == entry.get("content_sha256")

        for rid, entry in trusted.items():
            if rid not in on_disk:
                report.missing.append(rid)
            elif _matches(rid, entry):
                report.sealed += 1
            else:
                report.altered.append(rid)
        for rid, entry in untrusted.items():
            if rid in trusted:
                continue                       # a trusted verdict stands
            if rid not in on_disk:
                report.missing.append(rid)
            elif _matches(rid, entry):
                report.disputed.append(rid)
            else:
                report.altered.append(rid)
        for rid in on_disk:
            if rid not in trusted and rid not in untrusted:
                report.unsealed.append(rid)
        return report


def _read_record(path: Path) -> Optional[dict]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


# One instance per file, process-wide. Every writer (writeback, analyst,
# archived-session indexing) shares the tail, so the chain never forks.
_REGISTRY: dict[Path, Ledger] = {}
_REGISTRY_LOCK = threading.Lock()


def ledger_path_for(memory_dir: Path) -> Path:
    return Path(memory_dir).resolve().parent / LEDGER_FILENAME


def get_ledger(memory_dir: Path) -> Ledger:
    path = ledger_path_for(memory_dir)
    with _REGISTRY_LOCK:
        ledger = _REGISTRY.get(path)
        if ledger is None:
            ledger = _REGISTRY[path] = Ledger(path)
        return ledger


def forget_ledger(memory_dir: Path) -> None:
    """Drop a ledger from the registry. Only for a tenant whose folder is
    about to vanish (the server's throwaway read tenant); a live tenant's
    ledger must stay registered, or two instances fork its chain."""
    with _REGISTRY_LOCK:
        _REGISTRY.pop(ledger_path_for(memory_dir), None)
