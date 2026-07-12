# Cortex Stick — sneakernet for AI memory

> Your AI doesn't know you. This does. And it fits in your pocket.

You work from two desks — home and the shop, home and the office. Both
machines run Mnemo, and they drift: the decision you saved at one desk
doesn't exist at the other. The usual fixes put your AI's working memory
on somebody else's wire (cloud sync) or need infrastructure you don't
want to run (VPN, tailnet).

The Cortex Stick is a USB stick that works as a **courier** between two
full Mnemo installations. It is not a server — there's no database engine,
no embedder, nothing running on it. It carries the delta:

- **memories** — the per-agent memory JSONs (the truth files)
- **trajectories** — the append-only "how we did it" recipe logs
- **brain** — optionally, your brain/notes git repo (the stick holds a
  bare repo both machines push/pull through)
- **pad** — a free-form folder for dragging in-flight work between desks

Plug it in, sync, pull it out, carry it, plug it in. The other machine
catches up. No cloud, no VPN, no account.

## Quick start

```bash
# once, on any machine, with the stick mounted — YOUR choice of mode:
mnemo-cortex stick init /media/you/USB             # plain: readable by eye
mnemo-cortex stick init --encrypt /media/you/USB   # encrypted: passphrase

# encrypted sticks only — once, on the OTHER machine, with the stick in:
mnemo-cortex stick unlock     # same passphrase — enrolls this host

# then, at each desk, whenever the stick is in:
mnemo-cortex stick sync

# or let it happen automatically while you work:
mnemo-cortex stick watch      # syncs on plug-in, re-syncs while present
```

Both modes are first-class and stay supported: plain sticks are
human-auditable and zero-dependency; encrypted sticks survive being lost.
A plain stick can upgrade later with `stick encrypt`; sticks provisioned
by v1.0 keep working unchanged.

`stick status` shows what would travel in each direction without changing
anything, plus when each machine last synced and whether the stick is
encrypted.

Encrypted sticks need one extra package on each host:
`pip install 'mnemo-cortex[stick-crypto]'`.

## Encryption

A plaintext stick is a notebook full of your AI's working memory — lose it
and whoever finds it reads everything. Encrypt it:

- **What's protected.** Every truth file on the stick — memories,
  trajectories, pad files, conflict backups — is AES-256-SIV ciphertext,
  and the brain travels as an encrypted git bundle instead of a readable
  repo. A found stick leaks *structure* (file names, sizes, counts, which
  agents exist) but never content.
- **Where the key lives.** On each host, at `{data_dir}/stick-keys/`
  (0600) — **never on the stick**. It derives from your passphrase
  (scrypt); the stick's passport holds only the salt and a fingerprint, so
  a wrong passphrase fails loud instead of writing garbage. Enroll each
  host once with `stick unlock`; after that sync and watch run unattended.
- **Losing the passphrase** loses the courier, not your data — both
  machines keep their full installs. Re-init a fresh stick.
- **Already have a plaintext stick?** `mnemo-cortex stick encrypt` upgrades
  it in place (resumable if interrupted — rerun with the same passphrase).
  One honest caveat: replaced plaintext can linger in the stick's *free
  space* (flash wear leveling defeats targeted scrubbing) — if the
  plaintext era matters, zero-fill the free space afterwards, e.g. fill the
  stick with a junk file and delete it.
- **Tampering** (or bit rot) is caught twice: the manifest hash check
  refuses the generation, and even after a `stick repair` the AEAD tag
  refuses the decrypt. Tampered content can never reach a host as truth.
- Repair works *without* the key — anyone can make a torn stick consistent
  again; only key-holders can read it.
- `stick brain-clone <dest>` bootstraps the brain repo on a new machine
  from the encrypted bundle.

Encryption is in-tool rather than volume-level (BitLocker/VeraCrypt/LUKS)
so it behaves identically on Windows, macOS, and Linux with no drivers, no
admin rights, and no per-plug-in mount step — but nothing stops you from
running the stick on an encrypted volume *as well*.

## How it stays safe

- **Only truth files cross.** Vector indexes, caches, and sidecar files are
  derived data — each machine rebuilds its own. New memories are recallable
  immediately via disk truth; embeddings catch up on the server's next
  backfill pass.
- **Every sync is a 3-way merge.** The stick remembers what each machine
  had at last sync, so it can tell "new on the other side" from "deleted
  here" — no silent overwrites, no resurrection of deleted memories.
- **Conflicts never destroy data.** If the same memory was edited at both
  desks, one version wins deterministically and the loser is preserved on
  the stick under `state/conflicts/`. If one desk edited what the other
  deleted, the edit wins. Trajectory logs union-merge — append-only truth
  never loses a row.
- **Nothing moves until every check passes.** A sync first *plans* the whole
  run — every guard fires before a single byte is copied — so a refusal
  really does mean nothing was changed, on either machine or the stick.
- **Yank-proof commits.** Files are hashed into a manifest that is written
  last; a stick yanked mid-sync fails verification on the next plug-in and
  the sync refuses, loudly, instead of merging from a torn state. "Safe to
  remove ✓" prints only after every written file is readback-verified. If a
  yank does tear a generation, `mnemo-cortex stick repair` rebuilds the
  manifest from the stick's contents and the next sync merges from there —
  no manual surgery, nothing deleted by the repair.
- **Massacre guard.** If a sync would delete more than a quarter of a
  store (a wiped or replaced machine), it refuses and explains, and only
  proceeds with `--force`.

## What it does not do (yet)

- **No session logs.** Raw session capture stays on each machine; the
  dreamer digests locally.
- **No facts table sync** (`facts.sqlite`) yet.
- **Two-desk, one-human.** Multi-user shared lanes are designed for
  (the merge machinery is already order-safe) but not shipped.

## Files on the stick

```
<mount>/cortex/
  passport.json        which stick this is, which machines it has met,
                       encryption salt + fingerprint (never the key)
  manifest.json        per-file SHA-256 of the current generation
  memories/<agent>/    memory JSONs + trajectory JSONLs, per agent
  brain/brain.git      bare git repo (plaintext stick)
  brain/brain.bundle.enc   encrypted git bundle (encrypted stick)
  pad/                 yours — drag anything
  state/               per-machine inventories, conflict archive, lock
```

Everything is a plain file. On a plaintext stick your data is sitting right
there, readable; on an encrypted stick the same files are there, dark
without the passphrase.
