"""Cortex Stick — USB courier sync between two full Mnemo installations.

The stick is NOT a server. Both machines run full Mnemo; the stick carries
the delta between them: memory JSONs, trajectory JSONLs, the brain git repo,
and a free-form project pad. Plug in → sync; pull out → carry → plug in →
the other machine catches up. No cloud, no VPN.

Constitutional principles (brain/cortex-stick-spec.md, carried over):
  P2 — sync the facts, not the geometry. Only truth files cross the stick.
       Vec indexes, caches, and sidecars are derived and rebuilt per-host.
  F-4 — data files → manifest LAST. The manifest write is the commit point;
        a torn generation is detected on mount and refused loudly.
  F-2/F-5 — per-host base inventories make every sync a 3-way merge; no
        silent overwrite, and "safe to remove" fires only after readback-
        verified hashes.
  #1121 — conflicts resolve deterministically, but the LOSER is preserved
        under state/conflicts/ on the stick, never silently destroyed.

Design note: this is a STATE-BASED sync. The stick keeps, per host, the
inventory both sides agreed on at last sync (relpath → sha256 + version).
Deletes are detected against that base — no tombstones, no record-schema
changes, works against any existing install.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agentb.fsutil import atomic_write_bytes, atomic_write_text

log = logging.getLogger("agentb.stick")

STICK_DIRNAME = "cortex"          # <mount>/cortex/ — human-findable by eye
STICK_SCHEMA_VERSION = 1
MASS_DELETE_FRACTION = 0.25       # refuse if a sync would delete more than this
MASS_DELETE_MIN_FILES = 8         # ...but only guard channels at least this big
FREE_SPACE_MARGIN = 4 * 1024 * 1024  # keep 4MB headroom on the stick

# Truth patterns per channel. Everything else in those dirs (traj_index.sqlite,
# recall_stats.json, vec dbs, caches) is derived geometry and never crosses.
MEMORY_GLOB = "*.json"
TRAJECTORY_GLOB = "*.jsonl"

DEFAULT_MOUNT_ROOTS = [
    "/media/{user}", "/run/media/{user}",   # Linux automounts
    "/Volumes",                              # macOS
]


# ── small helpers ──────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        raise StickError(f"Unreadable JSON at {path}: {e}") from e


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(obj, indent=1, sort_keys=True) + "\n")


def default_host_id() -> str:
    """Lowercase hostname, exFAT-safe (used in on-stick filenames)."""
    raw = socket.gethostname().split(".")[0].lower()
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in raw) or "host"


class StickError(RuntimeError):
    """Loud failure — sync refuses rather than guessing."""


def _rmtree_writable(path: Path) -> None:
    """Remove a tree even when Windows Git objects are read-only."""
    def _clear_readonly_and_retry(func, failed_path, exc_info):
        exc = exc_info[1]
        if not isinstance(exc, PermissionError):
            raise exc
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)

    # onerror works on every supported Python (3.10+); onexc arrived in 3.12.
    shutil.rmtree(path, onerror=_clear_readonly_and_retry)


# ── encryption (v1.1) ──────────────────────────────────────────────────────
#
# A lost stick must not be a lost fleet memory. Encryption is IN-TOOL, not
# volume-level: AES-256-SIV file crypto works identically on every OS Python
# runs on, keeps the stick FAT32/exFAT, needs no admin rights or drivers,
# and `stick watch` stays unattended. (BitLocker To Go can't be read on
# Linux, LUKS can't be read on Windows, VeraCrypt needs installs + manual
# mounts on both ends.)
#
# SIV mode is DETERMINISTIC: same key + same plaintext → same ciphertext.
# That is a deliberate trade — it leaks content *equality* (an acceptable
# leak next to the visible filenames), and in exchange the whole 3-way merge
# engine keeps working unchanged in ciphertext space: the host side hashes
# encrypt(plaintext) during scan, so host↔stick comparison, base inventories,
# the manifest, torn-generation detection, and `stick repair` all operate on
# ciphertext hashes. Repair and verify never need the key.
#
# The key is scrypt-derived from a passphrase and stored per-host under
# {data_dir}/stick-keys/ (0600) — NEVER on the stick. The stick's passport
# carries only the KDF salt/params and a key-check fingerprint so a wrong
# passphrase fails loud instead of writing garbage.

ENC_MAGIC = b"CSTK\x01"           # header on every encrypted stick file
ENC_OVERHEAD = len(ENC_MAGIC) + 16  # magic + SIV synthetic-IV tag
SCRYPT_PARAMS = {"name": "scrypt", "n": 1 << 15, "r": 8, "p": 1}


class PlainCodec:
    """v1 behavior: bytes pass through untouched."""
    encrypted = False

    def encode(self, data: bytes) -> bytes:
        return data

    def decode(self, data: bytes) -> bytes:
        return data


class SivCodec:
    """AES-256-SIV, deterministic AEAD (RFC 5297) via `cryptography`."""
    encrypted = True

    def __init__(self, key: bytes):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESSIV
        except ImportError as e:
            raise StickError(
                "Encrypted stick support needs the 'cryptography' package — "
                "pip install 'mnemo-cortex[stick-crypto]' (or: pip install "
                "cryptography)."
            ) from e
        self._siv = AESSIV(key)

    def encode(self, data: bytes) -> bytes:
        return ENC_MAGIC + self._siv.encrypt(data, None)

    def decode(self, data: bytes) -> bytes:
        if not data.startswith(ENC_MAGIC):
            raise StickError(
                "Stick file is not encrypted (missing CSTK header) on an "
                "encrypted stick — was a plaintext file dropped in by hand? "
                "Run `stick encrypt` again to finish an interrupted migration."
            )
        from cryptography.exceptions import InvalidTag
        try:
            return self._siv.decrypt(data[len(ENC_MAGIC):], None)
        except InvalidTag as e:
            raise StickError(
                "DECRYPT FAILED — wrong key or tampered stick file. "
                "Nothing was written."
            ) from e


def _derive_key(passphrase: str, kdf: dict) -> bytes:
    if kdf.get("name") != "scrypt":
        raise StickError(f"Unsupported stick KDF: {kdf.get('name')!r} — "
                         "update mnemo-cortex on this machine.")
    return hashlib.scrypt(
        passphrase.encode(), salt=bytes.fromhex(kdf["salt"]),
        n=kdf["n"], r=kdf["r"], p=kdf["p"], dklen=64,
        maxmem=256 * 1024 * 1024,
    )


def _key_check(key: bytes) -> str:
    """Fingerprint stored in the passport so a wrong passphrase/key fails
    loud before any decrypt is attempted. Domain-separated from the key."""
    return hashlib.sha256(b"cortex-stick-key-check:" + key).hexdigest()[:16]


def make_enc_block(passphrase: str, kdf_params: Optional[dict] = None) -> tuple[dict, bytes]:
    """Fresh enc block for a passport + the derived key. kdf_params override
    is for tests (small n) — production always uses SCRYPT_PARAMS."""
    kdf = dict(kdf_params or SCRYPT_PARAMS, salt=os.urandom(16).hex())
    key = _derive_key(passphrase, kdf)
    return ({"alg": "aes-256-siv", "state": "ready", "kdf": kdf,
             "key_check": _key_check(key)}, key)


def stick_key_path(data_dir: Path, stick_id: str) -> Path:
    return data_dir / "stick-keys" / f"{stick_id}.key"


def save_stick_key(data_dir: Path, stick_id: str, key: bytes) -> Path:
    p = stick_key_path(data_dir, stick_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT with 0600 from birth — create-then-chmod would leave a window
    # where the key sits umask-readable. (Mode is advisory on Windows.)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.hex() + "\n")
    return p


def load_stick_key(data_dir: Path, stick_id: str) -> Optional[bytes]:
    p = stick_key_path(data_dir, stick_id)
    try:
        return bytes.fromhex(p.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise StickError(f"Corrupt stick key file {p}: {e}") from e


def codec_for_stick(stick: Path, data_dir: Path):
    """Resolve the codec this host must use for this stick — loud when the
    stick is encrypted and this host holds no (or the wrong) key."""
    passport = _load_json(stick / "passport.json", {})
    enc = passport.get("enc")
    if not enc:
        return PlainCodec()
    if enc.get("alg") != "aes-256-siv":
        raise StickError(f"Unsupported stick encryption {enc.get('alg')!r} — "
                         "update mnemo-cortex on this machine.")
    if enc.get("state") != "ready":
        raise StickError(
            "Stick encryption migration was interrupted — run "
            "`mnemo-cortex stick encrypt` again (same passphrase) to finish "
            "it. Sync is refused until then."
        )
    key = load_stick_key(data_dir, passport.get("stick_id", ""))
    if key is None:
        raise StickError(
            "Stick is ENCRYPTED and this host holds no key for it — run "
            "`mnemo-cortex stick unlock` and enter the stick passphrase "
            "(one time per host)."
        )
    if _key_check(key) != enc.get("key_check"):
        raise StickError(
            "This host's stored key does not match the stick (key_check "
            "mismatch) — re-run `mnemo-cortex stick unlock`."
        )
    return SivCodec(key)


def unlock_stick(stick: Path, data_dir: Path, passphrase: str) -> Path:
    """Enroll this host on an encrypted stick: derive the key from the
    passphrase, verify it against the passport, persist it (0600)."""
    passport = _load_json(stick / "passport.json", None)
    if passport is None:
        raise StickError(f"No passport.json at {stick} — not a Cortex Stick.")
    enc = passport.get("enc")
    if not enc:
        raise StickError("This stick is not encrypted — nothing to unlock.")
    key = _derive_key(passphrase, enc["kdf"])
    if _key_check(key) != enc.get("key_check"):
        raise StickError("Wrong passphrase for this stick.")
    SivCodec(key)   # probe: surface a missing 'cryptography' now, not at sync
    return save_stick_key(data_dir, passport.get("stick_id", ""), key)


# ── stick discovery / provisioning ─────────────────────────────────────────

def candidate_mount_roots(extra: Optional[list[str]] = None) -> list[Path]:
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    roots = [Path(r.format(user=user)) for r in DEFAULT_MOUNT_ROOTS]
    # Windows drive letters — cheap probe, no psutil dependency.
    if os.name == "nt":
        roots += [Path(f"{c}:/") for c in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    for r in extra or []:
        roots.insert(0, Path(r).expanduser())
    return roots


def _is_file_quiet(p: Path) -> bool:
    """is_file() that treats an unreadable path as absent. A sibling mount
    owned by root (a drive mid-mount, an autofs stub) raises PermissionError
    from stat(); the watcher must skip it, not die."""
    try:
        return p.is_file()
    except OSError:
        return False


def _is_dir_quiet(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def find_stick(extra_roots: Optional[list[str]] = None) -> Optional[Path]:
    """Locate a provisioned stick: a cortex/passport.json under a mount root
    (or the root itself being the stick dir). Returns the cortex/ dir.
    Unreadable candidates are skipped — the stick is never the only thing
    plugged into a machine."""
    for root in candidate_mount_roots(extra_roots):
        if root.name == STICK_DIRNAME and _is_file_quiet(root / "passport.json"):
            return root
        if not _is_dir_quiet(root):
            continue
        direct = root / STICK_DIRNAME
        if _is_file_quiet(direct / "passport.json"):
            return direct
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for mount in children:
            cand = mount / STICK_DIRNAME
            if _is_file_quiet(cand / "passport.json"):
                return cand
    return None


def init_stick(mount: Path, name: str = "cortex-stick",
               passphrase: Optional[str] = None,
               kdf_params: Optional[dict] = None) -> Path:
    """Provision <mount>/cortex/. Idempotent-hostile on purpose: refuses to
    re-init an existing stick (that would orphan host inventories).

    With a passphrase the stick is born encrypted; enroll each host with
    unlock_stick (the caller does this for the initializing host too — the
    key itself never touches the stick)."""
    stick = mount / STICK_DIRNAME if mount.name != STICK_DIRNAME else mount
    if (stick / "passport.json").exists():
        raise StickError(f"Already a Cortex Stick: {stick}")
    for sub in ("memories", "brain", "pad", "state/conflicts"):
        (stick / sub).mkdir(parents=True, exist_ok=True)
    passport = {
        "stick_id": hashlib.sha256(
            f"{name}:{time.time_ns()}".encode()
        ).hexdigest()[:16],
        "name": name,
        "schema_version": STICK_SCHEMA_VERSION,
        "created_at": time.time(),
        "hosts": {},
    }
    if passphrase is not None:
        passport["enc"], key = make_enc_block(passphrase, kdf_params)
        SivCodec(key)   # probe: missing 'cryptography' must fail HERE, not
                        # at the first sync of an already-provisioned stick
    _write_json(stick / "passport.json", passport)
    _write_json(stick / "manifest.json",
                {"schema_version": STICK_SCHEMA_VERSION, "generation": 0,
                 "files": {}})
    return stick


# ── manifest (the commit point) ────────────────────────────────────────────

def verify_manifest(stick: Path) -> dict:
    """Check every manifest hash against the stick's actual files.

    A mismatch means a torn generation (yank mid-sync, bit rot, tampering) —
    refuse to sync rather than merge from a lying base. The manifest is
    written LAST during sync, so a clean manifest = a complete generation.
    """
    manifest = _load_json(stick / "manifest.json", None)
    if manifest is None:
        raise StickError(f"No manifest.json on stick {stick} — not provisioned?")
    if manifest.get("schema_version", 0) > STICK_SCHEMA_VERSION:
        raise StickError(
            "Stick schema is newer than this host's Cortex Stick tool — "
            "update mnemo-cortex on this machine before syncing."
        )
    bad = []
    for rel, meta in manifest.get("files", {}).items():
        sha = meta.get("sha256") if isinstance(meta, dict) else None
        p = stick / rel
        if sha is None:
            bad.append(f"malformed manifest entry: {rel}")
        elif not p.is_file():
            bad.append(f"missing: {rel}")
        elif sha256_file(p) != sha:
            bad.append(f"hash mismatch: {rel}")
    if bad:
        detail = "\n  ".join(bad[:10])
        more = f"\n  … and {len(bad) - 10} more" if len(bad) > 10 else ""
        raise StickError(
            f"TORN GENERATION on stick — manifest disagrees with contents:\n"
            f"  {detail}{more}\n"
            f"Sync refused. Run `mnemo-cortex stick repair` to accept the "
            f"stick's current contents as truth and rebuild the manifest — "
            f"the next sync then 3-way-merges from the repaired state."
        )
    return manifest


def repair_manifest(stick: Path) -> dict:
    """Rebuild manifest.json from what is actually on the stick.

    The escape hatch after a mid-write yank or bit rot: accepts the stick's
    current contents as truth and re-hashes everything. Per-host base
    inventories are kept — they still describe the last state each host
    agreed on, so the next sync 3-way-merges from the repaired state
    (partially-carried files simply look like "changed on the stick").
    Also clears a stale lock left by a killed sync."""
    manifest = _load_json(stick / "manifest.json", None)
    if manifest is None:
        raise StickError(f"No manifest.json at {stick} — not a Cortex Stick.")
    if manifest.get("schema_version", 0) > STICK_SCHEMA_VERSION:
        raise StickError(
            "Stick schema is newer than this host's Cortex Stick tool — "
            "update mnemo-cortex on this machine before repairing."
        )
    old_files = manifest.get("files", {})
    files: dict[str, dict] = {}

    def add(rel: str, sha: str) -> None:
        prev = old_files.get(rel)
        prev = prev if isinstance(prev, dict) else None
        if prev and prev.get("sha256") == sha:
            files[rel] = prev
        else:
            files[rel] = {"sha256": sha,
                          "version": (prev or {}).get("version", 0) + 1}

    mems = stick / "memories"
    if mems.is_dir():
        for tenant in sorted(d for d in mems.iterdir() if d.is_dir()):
            for sub, pat in (("memory", MEMORY_GLOB),
                             ("trajectories", TRAJECTORY_GLOB)):
                for rel, sha in _scan(tenant / sub, pat).items():
                    add(f"memories/{tenant.name}/{sub}/{rel}", sha)
    for rel, sha in _scan(stick / "pad", "**/*").items():
        add(f"pad/{rel}", sha)
    for rel, sha in _scan(stick / "facts", "**/*").items():
        add(f"facts/{rel}", sha)

    manifest["files"] = files
    manifest["generation"] = manifest.get("generation", 0) + 1
    _write_json(stick / "manifest.json", manifest)
    (stick / "state" / "lock").unlink(missing_ok=True)
    return manifest


def encrypt_stick(stick: Path, passphrase: str,
                  kdf_params: Optional[dict] = None) -> dict:
    """In-place upgrade of a PLAINTEXT stick to encrypted. Resumable.

    Ordering is the design: the enc block (salt + key_check, state
    'migrating') commits to the passport FIRST, so a crash mid-migration
    can never strand files encrypted under a key that was never recorded.
    Files already carrying the CSTK header are skipped, so re-running the
    same command (same passphrase) finishes an interrupted run. state flips
    to 'ready' LAST — sync refuses a 'migrating' stick.

    The plaintext bytes are REPLACED, not scrubbed: on flash media the old
    clusters may survive until overwritten (wear leveling makes true
    scrubbing unreliable) — the caller should zero-fill the stick's free
    space afterwards if the plaintext history matters.

    Dropping the base inventories also forgets any host delete that hadn't
    couriered yet — that file resurrects on the host's next sync. Under-
    delete is the courier's one permitted failure direction; migrate from
    a synced state to avoid even that."""
    passport = _load_json(stick / "passport.json", None)
    if passport is None:
        raise StickError(f"No passport.json at {stick} — not a Cortex Stick.")
    enc = passport.get("enc")
    if enc and enc.get("state") == "ready":
        raise StickError("Stick is already encrypted.")
    resuming = bool(enc)
    if resuming:                               # resume an interrupted run
        key = _derive_key(passphrase, enc["kdf"])
        if _key_check(key) != enc.get("key_check"):
            raise StickError(
                "Wrong passphrase for the interrupted migration on this "
                "stick — use the one the migration started with.")
    else:
        enc, key = make_enc_block(passphrase, kdf_params)
        enc["state"] = "migrating"
    # Codec BEFORE the passport write: a host without 'cryptography' must
    # refuse here, not strand the stick in 'migrating' (which locks out
    # syncs on BOTH machines until someone finishes the job).
    codec = SivCodec(key)
    if not resuming:
        passport["enc"] = enc
        _write_json(stick / "passport.json", passport)

    count = 0
    roots = (stick / "memories", stick / "pad", stick / "facts",
             stick / "state" / "conflicts")
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.endswith(".tmp"):
                continue
            raw = p.read_bytes()
            if raw.startswith(ENC_MAGIC):      # already done (resume)
                continue
            atomic_write_bytes(p, codec.encode(raw))
            count += 1

    # brain: bare repo → encrypted bundle. Bundling FROM the stick's bare
    # repo (not the host's clone) preserves commits a host pushed but the
    # other machine hasn't pulled yet.
    bare = stick / "brain" / "brain.git"
    if bare.is_dir():
        has_refs = _git(bare, "for-each-ref").stdout.strip()
        if has_refs:
            branch = _git(bare, "symbolic-ref", "--short", "HEAD").stdout.strip()
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "brain.bundle"
                r = _git(bare, "bundle", "create", str(out), branch or "--all")
                if r.returncode != 0:
                    raise StickError(
                        f"brain: bundle from stick bare repo failed: "
                        f"{r.stderr.strip()}")
                atomic_write_bytes(stick / "brain" / "brain.bundle.enc",
                                   codec.encode(out.read_bytes()))
        _rmtree_writable(bare)

    # Commit: manifest over the ciphertext, base inventories dropped (they
    # hold plaintext-space hashes; identical content re-agrees on first sync,
    # divergent files surface as conflicts — preserved, never lost), then
    # state=ready as the final word.
    manifest = repair_manifest(stick)
    for inv in (stick / "state").glob("inventory-*.json"):
        inv.unlink()
    passport = _load_json(stick / "passport.json", {})
    passport["enc"]["state"] = "ready"
    _write_json(stick / "passport.json", passport)
    return {"files_encrypted": count, "generation": manifest["generation"],
            "key": key}


# ── channels ───────────────────────────────────────────────────────────────

@dataclass
class Channel:
    """One synced surface. file-unit 3-way merge; 'jsonl' additionally
    union-merges concurrent edits line-by-line instead of conflicting."""
    name: str            # inventory namespace, e.g. "memories/cc/memory"
    host_dir: Path
    stick_dir: Path      # absolute, under the stick
    glob: str
    unit: str = "file"   # "file" | "jsonl"


@dataclass
class SyncReport:
    to_stick: list[str] = field(default_factory=list)
    to_host: list[str] = field(default_factory=list)
    deleted_on_stick: list[str] = field(default_factory=list)
    deleted_on_host: list[str] = field(default_factory=list)
    merged_jsonl: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    brain: str = "not configured"   # no brain_repo on this host
    facts_to_host: int = 0
    facts_to_stick: int = 0
    history_to_host: int = 0      # 4.21.2: origin fact_history rows carried
    history_to_stick: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.to_stick or self.to_host or self.deleted_on_stick
                    or self.deleted_on_host or self.merged_jsonl
                    or self.conflicts or self.brain in ("pushed", "merged")
                    or self.facts_to_host or self.facts_to_stick
                    or self.history_to_host or self.history_to_stick)


def _scan(root: Path, pattern: str, transform=None) -> dict[str, str]:
    """relpath → sha256 for truth files directly under root (non-recursive
    for globs; pad uses rglob via pattern '**/*').

    transform maps file bytes before hashing: on an encrypted stick the host
    side scans with codec.encode so host and stick compare in ciphertext
    space (SIV determinism is what makes this hash stable)."""
    if not root.is_dir():
        return {}
    out = {}
    it = root.rglob(pattern[3:]) if pattern.startswith("**/") else root.glob(pattern)
    for p in sorted(it):
        if p.is_file() and not p.name.endswith(".tmp"):
            rel = p.relative_to(root).as_posix()
            if transform is None:
                out[rel] = sha256_file(p)
            else:
                out[rel] = hashlib.sha256(transform(p.read_bytes())).hexdigest()
    return out


def _copy_verified(src: Path, dst: Path, expect_sha: str) -> None:
    """Copy + readback-verify. tmp+rename so a yank never leaves a torn file
    where a truth file used to be."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(src, tmp)
    if sha256_file(tmp) != expect_sha:
        tmp.unlink(missing_ok=True)
        raise StickError(f"Readback verify FAILED copying {src} → {dst}")
    os.replace(tmp, dst)


def _transfer(src: Path, dst: Path, expect_sha: str, codec,
              *, encrypting: bool) -> None:
    """Move a truth file across the host↔stick boundary through the codec.

    encrypting=True: host plaintext → stick ciphertext. expect_sha is the
    ciphertext hash (precomputable because SIV is deterministic).
    encrypting=False: stick ciphertext → host plaintext. src is verified
    against expect_sha, then the AEAD tag authenticates the decrypt.
    Plaintext codec degrades to the v1 copy+verify."""
    if not codec.encrypted:
        _copy_verified(src, dst, expect_sha)
        return
    raw = src.read_bytes()
    if encrypting:
        out = codec.encode(raw)
        if hashlib.sha256(out).hexdigest() != expect_sha:
            raise StickError(f"Encrypt-verify FAILED for {src} → {dst} "
                             "(file changed mid-sync?)")
    else:
        if hashlib.sha256(raw).hexdigest() != expect_sha:
            raise StickError(f"Source hash mismatch reading {src} — "
                             "changed mid-sync?")
        out = codec.decode(raw)
    out_sha = hashlib.sha256(out).hexdigest()
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_bytes(out)
    if sha256_file(tmp) != out_sha:
        tmp.unlink(missing_ok=True)
        raise StickError(f"Readback verify FAILED copying {src} → {dst}")
    os.replace(tmp, dst)
    try:   # copy2 semantics: keep source mtime (the conflict tie-break reads it)
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def _jsonl_union(host_file: Path, stick_file: Path, codec=None) -> str:
    """Union-merge two versions of an append-only JSONL by record id
    (content-hash for id-less lines). Order: stick lines first (they're the
    older courier state), then host-only appends. Returns merged text."""
    codec = codec or PlainCodec()

    def parse(path: Path, decode: bool = False) -> list[tuple[str, str]]:
        pairs = []
        if not path.is_file():
            return pairs
        data = path.read_bytes()
        if decode:
            data = codec.decode(data)
        for line in data.decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                key = json.loads(line).get("id") or hashlib.sha256(
                    line.encode()).hexdigest()
            except (json.JSONDecodeError, AttributeError):
                key = hashlib.sha256(line.encode()).hexdigest()
            pairs.append((str(key), line))
        return pairs

    seen: dict[str, str] = {}
    for key, line in parse(stick_file, decode=codec.encrypted) + parse(host_file):
        seen.setdefault(key, line)
    return "\n".join(seen.values()) + ("\n" if seen else "")


def sync_channel(
    ch: Channel,
    base: dict[str, dict],
    manifest_files: dict[str, dict],
    report: SyncReport,
    *,
    codec=None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, dict]:
    """3-way merge of one channel. Returns the channel's new base inventory
    and updates manifest_files (keyed by stick-relative path) in place.

    On an encrypted stick every hash here — host scan, stick scan, base,
    manifest — is a CIPHERTEXT hash; the host scan encrypts in memory to
    compute its stick-shape hash (deterministic SIV makes that stable)."""
    codec = codec or PlainCodec()
    host = _scan(ch.host_dir, ch.glob,
                 transform=codec.encode if codec.encrypted else None)
    stick_root = ch.stick_dir
    stick = _scan(stick_root, ch.glob)

    # Mass-delete guard: count deletions this sync WOULD apply on each side.
    host_dels = [r for r in base if r not in host and r in stick
                 and stick[r] == base[r].get("sha256")]
    stick_dels = [r for r in base if r not in stick and r in host
                  and host[r] == base[r].get("sha256")]
    for side, dels, total in (("host", stick_dels, len(host)),
                              ("stick", host_dels, len(stick))):
        if (total >= MASS_DELETE_MIN_FILES and dels
                and len(dels) / max(total, 1) > MASS_DELETE_FRACTION
                and not force):
            raise StickError(
                f"MASS-DELETE GUARD [{ch.name}]: this sync would delete "
                f"{len(dels)}/{total} files on the {side}. If that is truly "
                f"intended, re-run with --force. Nothing was changed."
            )

    new_base: dict[str, dict] = {}
    for rel in sorted(set(host) | set(stick) | set(base)):
        h, s = host.get(rel), stick.get(rel)
        b = base.get(rel, {}).get("sha256")
        ver = base.get(rel, {}).get("version", 0)
        hp, sp = ch.host_dir / rel, stick_root / rel
        entry = None

        if h and s and h == s:                              # agree
            entry = {"sha256": h,
                     "version": manifest_files.get(_mkey(ch, rel), {}).get(
                         "version", max(ver, 1))}
        elif h == b and s and s != b:                       # stick changed → host
            if not dry_run:
                _transfer(sp, hp, s, codec, encrypting=False)
            report.to_host.append(f"{ch.name}/{rel}")
            entry = {"sha256": s,
                     "version": manifest_files.get(_mkey(ch, rel), {}).get("version", ver + 1)}
        elif s == b and h and h != b:                       # host changed → stick
            if not dry_run:
                _transfer(hp, sp, h, codec, encrypting=True)
            report.to_stick.append(f"{ch.name}/{rel}")
            entry = {"sha256": h, "version": ver + 1}
        elif h is None and s == b and b is not None:        # host deleted → stick
            if not dry_run:
                sp.unlink(missing_ok=True)
            report.deleted_on_stick.append(f"{ch.name}/{rel}")
        elif s is None and h == b and b is not None:        # stick deleted → host
            if not dry_run:
                hp.unlink(missing_ok=True)
            report.deleted_on_host.append(f"{ch.name}/{rel}")
        elif h is None and s is None:                       # gone both sides
            pass
        else:                                               # CONFLICT
            entry = _resolve_conflict(ch, rel, h, s, ver, manifest_files,
                                      report, codec=codec, dry_run=dry_run)

        if entry:
            new_base[rel] = entry
            manifest_files[_mkey(ch, rel)] = entry
        else:
            manifest_files.pop(_mkey(ch, rel), None)
    return new_base


def _mkey(ch: Channel, rel: str) -> str:
    """Manifest key: channel's stick dir relative to the cortex root."""
    return f"{ch.name}/{rel}"


def _resolve_conflict(
    ch: Channel, rel: str, h: Optional[str], s: Optional[str], base_ver: int,
    manifest_files: dict, report: SyncReport, *, codec=None, dry_run: bool,
) -> Optional[dict]:
    """Deterministic conflict resolution, loser preserved on the stick.

    Edit beats delete (under-delete is the only permitted failure mode).
    Both-edit: JSONL channels union-merge; file channels pick a winner by
    stick generation, then mtime, then hash — and the loser is copied to
    state/conflicts/ before being overwritten."""
    codec = codec or PlainCodec()
    hp, sp = ch.host_dir / rel, ch.stick_dir / rel
    tag = f"{ch.name}/{rel}"

    if h and s is None:            # host edited, stick (other machine) deleted
        if not dry_run:
            _transfer(hp, sp, h, codec, encrypting=True)
        report.conflicts.append(f"{tag}: edit-vs-delete — edit wins, restored")
        return {"sha256": h, "version": base_ver + 1}
    if s and h is None:            # other machine edited, this host deleted
        if not dry_run:
            _transfer(sp, hp, s, codec, encrypting=False)
        report.conflicts.append(f"{tag}: delete-vs-edit — edit wins, restored")
        return {"sha256": s, "version": base_ver + 1}

    # From here on it's both-edit: neither side is a delete.
    assert h is not None and s is not None

    if ch.unit == "jsonl":         # append-only truth: union, nobody loses
        merged = _jsonl_union(hp, sp, codec)
        stick_bytes = codec.encode(merged.encode())
        sha = hashlib.sha256(stick_bytes).hexdigest()
        if not dry_run:
            hp.parent.mkdir(parents=True, exist_ok=True)
            sp.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(hp, merged)
            atomic_write_bytes(sp, stick_bytes)
            if sha256_file(sp) != sha:
                raise StickError(f"Readback verify FAILED on merged {sp}")
        report.merged_jsonl.append(tag)
        return {"sha256": sha, "version": base_ver + 1}

    # Both-edit on a file channel.
    stick_ver = manifest_files.get(_mkey(ch, rel), {}).get("version", base_ver)
    if stick_ver > base_ver + 1:
        winner, w_sha, loser_path, loser_sha = "stick", s, hp, h
    elif stick_ver < base_ver + 1:
        winner, w_sha, loser_path, loser_sha = "host", h, sp, s
    else:  # tie → newer mtime, then lexicographic hash (fully deterministic)
        hm = hp.stat().st_mtime if hp.exists() else 0
        sm = sp.stat().st_mtime if sp.exists() else 0
        if hm != sm:
            winner = "host" if hm > sm else "stick"
        else:
            winner = "host" if (h or "") >= (s or "") else "stick"
        w_sha, loser_path, loser_sha = \
            (h, sp, s) if winner == "host" else (s, hp, h)

    if not dry_run:
        # The loser backup is the last copy of a losing edit — it gets the
        # same tmp+rename+readback guard as every truth write, and a hash
        # uniquifier so two conflicts in the same second can't clobber it.
        # It lives on the stick, so on an encrypted stick a losing HOST edit
        # is encrypted on its way in; a losing stick copy is ciphertext already.
        conflicts_dir = _conflicts_dir(ch)
        dst = conflicts_dir / (f"{int(time.time())}-{loser_sha[:8]}-"
                               f"{rel.replace('/', '__')}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if loser_path == hp:
            _transfer(hp, dst, loser_sha, codec, encrypting=True)
        else:
            _copy_verified(sp, dst, loser_sha)
        if winner == "host":
            _transfer(hp, sp, h, codec, encrypting=True)
        else:
            _transfer(sp, hp, s, codec, encrypting=False)
    report.conflicts.append(
        f"{tag}: both edited — {winner} wins, loser saved to state/conflicts/"
    )
    return {"sha256": w_sha, "version": base_ver + 2}


def _conflicts_dir(ch: Channel) -> Path:
    """state/conflicts/ at the stick root (cortex/)."""
    p = ch.stick_dir
    while p.name != STICK_DIRNAME and p.parent != p:
        p = p.parent
    return p / "state" / "conflicts"


# ── brain channel (git courier remote) ─────────────────────────────────────

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=120,
    )


def sync_brain(repo: Path, stick: Path, report: SyncReport, *,
               dry_run: bool = False) -> None:
    """Sync the brain git repo through a bare repo on the stick.

    fetch → ff/merge → push. A merge that conflicts is aborted and reported;
    the human resolves — the courier never writes conflict markers into a
    brain file behind anyone's back. A dirty working tree skips the channel."""
    if not (repo / ".git").exists():
        report.warnings.append(f"brain: {repo} is not a git repo — skipped")
        report.brain = "skipped"
        return
    if _git(repo, "status", "--porcelain").stdout.strip():
        report.warnings.append(
            "brain: working tree dirty — commit first, brain channel skipped")
        report.brain = "skipped-dirty"
        return
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    bare = stick / "brain" / "brain.git"
    if dry_run:
        report.brain = "dry-run"
        return
    if not bare.exists():
        r = subprocess.run(["git", "init", "--bare", str(bare)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise StickError(f"git init --bare failed on stick: {r.stderr.strip()}")
        # git init --bare defaults HEAD to master; if the couriered branch is
        # named anything else, a later clone from the stick checks out nothing
        # ("remote HEAD refers to nonexistent ref"). Point HEAD at the branch
        # we actually carry.
        subprocess.run(
            ["git", "-C", str(bare), "symbolic-ref", "HEAD",
             f"refs/heads/{branch}"],
            capture_output=True, text=True,
        )

    fetch = _git(repo, "fetch", str(bare), branch)
    have_remote = fetch.returncode == 0
    if have_remote:
        counts = _git(repo, "rev-list", "--left-right", "--count",
                      f"HEAD...FETCH_HEAD").stdout.split()
        ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (1, 0)
    else:
        ahead, behind = 1, 0  # empty bare repo: nothing to merge, just push

    if behind:
        merge = _git(repo, "merge", "--no-edit", "FETCH_HEAD")
        if merge.returncode != 0:
            _git(repo, "merge", "--abort")
            report.brain = "CONFLICT"
            report.conflicts.append(
                "brain: git merge conflict — resolve by hand "
                f"(git fetch {bare} {branch} && git merge FETCH_HEAD)")
            return
        report.brain = "merged"
    push = _git(repo, "push", str(bare), f"{branch}:{branch}")
    if push.returncode != 0:
        raise StickError(f"brain: push to stick failed: {push.stderr.strip()}")
    if ahead and report.brain != "merged":
        report.brain = "pushed"
    elif not ahead and not behind:
        report.brain = "clean"


def sync_brain_bundle(repo: Path, stick: Path, codec, report: SyncReport, *,
                      dry_run: bool = False) -> None:
    """Encrypted-stick brain channel: the courier is a full git bundle,
    encrypted like every other stick file. (A bare repo would leak the whole
    brain as readable zlib on a plaintext volume.)

    decrypt bundle → fetch → ff/merge → re-bundle → encrypt. Full history
    each time — brains are small; correctness over cleverness. Same failure
    domain as v1: brain.bundle.enc is NOT manifest-covered, so its failures
    can't tear a generation. Merge conflicts abort and report, human resolves.
    The plaintext bundle only ever exists in a host tempdir."""
    if not (repo / ".git").exists():
        report.warnings.append(f"brain: {repo} is not a git repo — skipped")
        report.brain = "skipped"
        return
    if _git(repo, "status", "--porcelain").stdout.strip():
        report.warnings.append(
            "brain: working tree dirty — commit first, brain channel skipped")
        report.brain = "skipped-dirty"
        return
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    enc_path = stick / "brain" / "brain.bundle.enc"
    if dry_run:
        report.brain = "dry-run"
        return

    with tempfile.TemporaryDirectory() as td:
        ahead, behind = 1, 0
        if enc_path.is_file():
            plain = Path(td) / "in.bundle"
            plain.write_bytes(codec.decode(enc_path.read_bytes()))
            fetch = _git(repo, "fetch", str(plain), branch)
            if fetch.returncode != 0:
                raise StickError(
                    f"brain: fetch from stick bundle failed (branch "
                    f"{branch!r}): {fetch.stderr.strip()}")
            counts = _git(repo, "rev-list", "--left-right", "--count",
                          "HEAD...FETCH_HEAD").stdout.split()
            ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (1, 0)
        if behind:
            merge = _git(repo, "merge", "--no-edit", "FETCH_HEAD")
            if merge.returncode != 0:
                _git(repo, "merge", "--abort")
                report.brain = "CONFLICT"
                report.conflicts.append(
                    "brain: git merge conflict — resolve by hand "
                    "(mnemo-cortex stick brain-clone a copy, or decrypt the "
                    "bundle via a second sync after committing)")
                return
            report.brain = "merged"
            ahead = 1   # the merge commit itself must travel
        if ahead:
            out = Path(td) / "out.bundle"
            r = _git(repo, "bundle", "create", str(out), branch)
            if r.returncode != 0:
                raise StickError(
                    f"brain: bundle create failed: {r.stderr.strip()}")
            atomic_write_bytes(enc_path, codec.encode(out.read_bytes()))
            if report.brain != "merged":
                report.brain = "pushed"
        else:
            report.brain = "clean"


def clone_brain_from_stick(stick: Path, data_dir: Path, dest: Path) -> Path:
    """Bootstrap the brain repo on a second machine from an encrypted stick
    (the encrypted twin of `git clone <stick>/brain/brain.git`)."""
    codec = codec_for_stick(stick, data_dir)
    if dest.exists() and any(dest.iterdir()):
        raise StickError(f"Refusing to clone into non-empty {dest}")
    bare = stick / "brain" / "brain.git"
    if not codec.encrypted:
        src = bare if bare.is_dir() else None
        if src is None:
            raise StickError("No brain on this stick (brain/brain.git missing).")
        r = subprocess.run(["git", "clone", str(src), str(dest)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise StickError(f"brain clone failed: {r.stderr.strip()}")
        return dest
    enc_path = stick / "brain" / "brain.bundle.enc"
    if not enc_path.is_file():
        raise StickError("No brain on this stick (brain/brain.bundle.enc "
                         "missing) — sync from the machine that has the repo first.")
    with tempfile.TemporaryDirectory() as td:
        plain = Path(td) / "brain.bundle"
        plain.write_bytes(codec.decode(enc_path.read_bytes()))
        # A bundle carries no HEAD — name the branch explicitly or the clone
        # checks out nothing ("remote HEAD refers to nonexistent ref").
        heads = subprocess.run(
            ["git", "bundle", "list-heads", str(plain)],
            capture_output=True, text=True, timeout=60,
        ).stdout.split()
        branches = [h.removeprefix("refs/heads/") for h in heads
                    if h.startswith("refs/heads/")]
        if not branches:
            raise StickError("brain bundle on stick carries no branches.")
        r = subprocess.run(
            ["git", "clone", "-b", branches[0], str(plain), str(dest)],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise StickError(f"brain clone from bundle failed: {r.stderr.strip()}")
    return dest


# ── host config + the full sync ────────────────────────────────────────────

def load_host_config(data_dir: Path) -> dict:
    """{data_dir}/stick.json: host_id, brain_repo, tenants, mount_roots, pad.

    host_id is generated once and PERSISTED — identity belongs to the DATA
    STORE, not the machine name. A bare hostname is a footgun: two desks named
    "ubuntu" (or one reinstalled machine) would share a base inventory, and
    the second store's missing files would read as deletions — the courier
    would carry a massacre. The random suffix makes every store a distinct
    sync peer; a fresh install gets a fresh id and merges instead of deleting."""
    path = data_dir / "stick.json"
    cfg = _load_json(path, {})
    if not cfg.get("host_id"):
        cfg["host_id"] = f"{default_host_id()}-{os.urandom(3).hex()}"
        _write_json(path, cfg)
    cfg.setdefault("brain_repo", None)
    cfg.setdefault("tenants", None)       # None = every agent dir with memory/
    cfg.setdefault("mount_roots", [])
    cfg.setdefault("pad", True)
    cfg.setdefault("facts", True)
    return cfg


def discover_tenants(data_dir: Path, explicit: Optional[list[str]]) -> list[str]:
    """Tenant stores live at {data_dir}/agents/<id> (get_agent_data_dir's
    layout) — NOT directly under data_dir. Archived agents (agents kept as
    <id>.archived-YYYYMMDD) are skipped: validate_agent_id would reject the
    dot anyway, and a courier must not resurrect an archived store."""
    if explicit:
        return list(explicit)
    agents_root = data_dir / "agents"
    if not agents_root.is_dir():
        return []
    return sorted(
        d.name for d in agents_root.iterdir()
        if d.is_dir() and "." not in d.name
        and ((d / "memory").is_dir() or (d / "trajectories").is_dir())
    )


def build_channels(data_dir: Path, stick: Path, tenants: list[str],
                   pad: bool) -> list[Channel]:
    chans = []
    for t in tenants:
        chans.append(Channel(
            name=f"memories/{t}/memory",
            host_dir=data_dir / "agents" / t / "memory",
            stick_dir=stick / "memories" / t / "memory",
            glob=MEMORY_GLOB,
        ))
        chans.append(Channel(
            name=f"memories/{t}/trajectories",
            host_dir=data_dir / "agents" / t / "trajectories",
            stick_dir=stick / "memories" / t / "trajectories",
            glob=TRAJECTORY_GLOB,
            unit="jsonl",
        ))
    if pad:
        chans.append(Channel(
            name="pad",
            host_dir=data_dir / "pad",
            stick_dir=stick / "pad",
            glob="**/*",
        ))
    return chans


def _plan_need_bytes(channels: list[Channel], plan: SyncReport,
                     encrypted: bool = False) -> int:
    """Bytes the apply pass will write to the stick, from the plan report:
    host→stick copies, union-merged JSONLs, and conflict-loser backups
    (counted at host-file size — conservative). Encrypted sticks add the
    per-file header+tag overhead."""
    by_name = sorted(channels, key=lambda c: len(c.name), reverse=True)
    need = 0
    entries = (plan.to_stick + plan.merged_jsonl
               + [c.split(":", 1)[0] for c in plan.conflicts])
    for entry in entries:
        for ch in by_name:   # longest-prefix match: names can nest
            if entry.startswith(ch.name + "/"):
                p = ch.host_dir / entry[len(ch.name) + 1:]
                try:
                    need += p.stat().st_size
                    if encrypted:
                        need += ENC_OVERHEAD
                except OSError:
                    pass
                break
    return need


def sync(
    data_dir: Path,
    stick: Path,
    *,
    host_id: str,
    tenants: Optional[list[str]] = None,
    brain_repo: Optional[Path] = None,
    pad: bool = True,
    facts: bool = True,
    force: bool = False,
    dry_run: bool = False,
    codec=None,
) -> SyncReport:
    """The courier sync.

    Order is the whole design:
      1. verify manifest (torn-generation gate)
      2. PLAN PASS — every channel runs dry against a scratch manifest, so
         every guard (mass-delete, conflicts) fires BEFORE a single byte
         moves. A refusal really does mean "nothing was changed"; without
         this, a guard tripping on the second tenant would abandon the first
         tenant's already-written files and tear the generation.
      3. brain git sync — brain.git is NOT manifest-covered, so its failures
         (push rejected, disk full from pack objects) can't tear the
         generation; running it before the channels also lets the free-space
         check see the space it consumed.
      4. free-space preflight, sized from the plan
      5. APPLY PASS — the channels, for real
      6. commit: fsync → inventory → manifest LAST → passport

    host_id must be the store-scoped id from load_host_config — a bare
    hostname is how two same-named machines end up sharing a base inventory
    and fabricating deletions.

    A yank during step 5 still tears the generation (USB media offers no
    transactions) — that's what `repair_manifest` is for; the next mount
    detects it and says so."""
    if not host_id:
        raise StickError("sync() requires a store-scoped host_id "
                         "(see load_host_config).")
    report = SyncReport()
    hid = host_id
    # Codec first: an encrypted stick this host can't unlock refuses before
    # anything else is even read.
    if codec is None:
        codec = codec_for_stick(stick, data_dir)
    manifest = verify_manifest(stick)

    lock = stick / "state" / "lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime < 3600:
        raise StickError(f"Another sync holds the stick lock ({lock}). "
                         "If that sync is dead, delete the lock file.")
    if not dry_run:
        lock.write_text(f"{hid} {int(time.time())}\n", encoding="utf-8")

    try:
        tenant_list = discover_tenants(data_dir, tenants)
        # A stick may know tenants this host doesn't have yet — adopt them,
        # that's the point of a courier.
        stick_tenants = sorted(
            d.name for d in (stick / "memories").iterdir() if d.is_dir()
        ) if (stick / "memories").is_dir() else []
        tenant_list = sorted(set(tenant_list) | set(stick_tenants))

        channels = build_channels(data_dir, stick, tenant_list, pad)
        inv_path = stick / "state" / f"inventory-{hid}.json"
        base_all: dict = _load_json(inv_path, {})
        manifest_files = manifest.get("files", {})

        # ── 2. PLAN PASS — mutates nothing, fires every guard ──
        plan = SyncReport()
        for ch in channels:
            sync_channel(ch, base_all.get(ch.name, {}), dict(manifest_files),
                         plan, codec=codec, force=force, dry_run=True)
        def _brain(rep: SyncReport, dry: bool = False) -> None:
            assert brain_repo is not None   # callers gate on `if brain_repo`
            if codec.encrypted:
                sync_brain_bundle(Path(brain_repo), stick, codec, rep,
                                  dry_run=dry)
            else:
                sync_brain(Path(brain_repo), stick, rep, dry_run=dry)

        facts_bytes = 0

        def _facts(rep: SyncReport, files: dict, dry: bool = False) -> None:
            nonlocal facts_bytes
            from agentb.stick_facts import sync_fact_history, sync_facts
            stick_id = _load_json(stick / "passport.json", {}).get("stick_id", "")
            db = data_dir / "facts.sqlite"
            rep.facts_to_host, rep.facts_to_stick, _, facts_bytes = sync_facts(
                db, stick, codec, stick_id, files, dry_run=dry)
            # history rides after the rows it describes (4.21.2)
            rep.history_to_host, rep.history_to_stick, _, hist_bytes = \
                sync_fact_history(db, stick, codec, files, dry_run=dry)
            facts_bytes += hist_bytes

        if facts:                        # plan the facts merge dry, like channels
            _facts(plan, dict(manifest_files), dry=True)
        if dry_run:
            if brain_repo:
                _brain(plan, dry=True)
            return plan

        # ── 3. brain (outside the manifest's failure domain) ──
        if brain_repo:
            _brain(report)

        # ── 4. free-space, sized from the plan (incl. the facts payload) ──
        need = _plan_need_bytes(channels, plan, encrypted=codec.encrypted)
        if facts and (plan.facts_to_stick or plan.facts_to_host
                      or plan.history_to_stick or plan.history_to_host):
            need += facts_bytes
        free = shutil.disk_usage(stick).free
        if need + FREE_SPACE_MARGIN > free:
            raise StickError(
                f"Stick too full: sync needs ~{need // 1024}KB, "
                f"only {free // 1024}KB free. No memory files were changed."
            )

        # ── 5. APPLY PASS ──
        new_base_all = {}
        for ch in channels:
            new_base_all[ch.name] = sync_channel(
                ch, base_all.get(ch.name, {}), manifest_files, report,
                codec=codec, force=force,
            )

        # facts ride inside the manifest commit like any truth file; the
        # row-wise ladder merge needs no base inventory (see stick_facts.py)
        if facts:
            _facts(report, manifest_files)

        # ── 6. commit: fsync data to media, then inventory, manifest LAST ──
        if hasattr(os, "sync"):
            os.sync()
        _write_json(inv_path, new_base_all)
        manifest["files"] = manifest_files
        manifest["generation"] = manifest.get("generation", 0) + 1
        _write_json(stick / "manifest.json", manifest)

        passport = _load_json(stick / "passport.json", {})
        passport.setdefault("hosts", {})[hid] = {
            "last_sync": time.time(),
            "generation": manifest["generation"],
        }
        _write_json(stick / "passport.json", passport)
        if hasattr(os, "sync"):
            os.sync()
    finally:
        if not dry_run:
            lock.unlink(missing_ok=True)
    return report
