# Memory Ledger — a tamper-evident chain over what the store was told

> Your AI remembers what you said. The ledger proves nobody changed it.

Every memory record has two halves. The **testimony** is what the person or
agent said: the summary, the key facts, the decisions, who said it and when.
The **filing** is what the store did with it afterwards: category, tags,
`superseded_by`, reclassification marks. The filing is allowed to change —
the dreamer reclassifies, a later write supersedes. The testimony is not.
Once a memory is saved, its words must never silently become other words.

The Memory Ledger seals the testimony.

## How it works

One append-only `ledger.jsonl` per tenant, next to `memory/`:

```
~/.agentb/agents/cc/
├── memory/           ← one JSON per memory (the truth files)
├── ledger.jsonl      ← the chain
└── vec_index.sqlite
```

Every save appends one line:

```json
{"seq": 4213, "ts": 1789851200.1, "op": "save", "memory_id": "9f1c…",
 "content_sha256": "…", "prev": "<hash of entry 4212>", "hash": "<hash of this entry>"}
```

- `content_sha256` — SHA-256 over the sealed fields of the record, in
  canonical JSON: `id`, `session_id`, `agent_id`, `summary`, `key_facts`,
  `decisions_made`, `projects_referenced`, `timestamp`.
- `prev` — the hash of the entry before it. The first entry points at all
  zeros.
- `hash` — SHA-256 over the whole entry minus itself.

Change one record's words on disk and `verify` reports it **altered**. Edit
or drop a ledger line and the chain reports **broken** at that sequence —
and the walk keeps going past the break, so a record forged *after* it is
still named `altered`, not quietly downgraded to `unsealed`. There is no
way to rewrite the past without the rewrite showing.

Every writer goes through the same seal: `/writeback`, the Analyst and Muse
notes, archived-session indexing. The record hits disk first, then the
ledger line is appended and fsynced. A crash between the two leaves an
**unsealed** record (which `verify` names) — never a sealed entry with no
record behind it.

A crash *during* the append leaves an unterminated fragment on the tail.
Until the next write, `verify` reports the chain broken at that line. The
next seal (or `ledger seal`) moves the fragment to `ledger.torn` beside the
chain, truncates back to the last complete line, and carries on; the
fragment's record reads `unsealed` and adoption picks it up, and the
server log carries a WARNING with the path and offset. Only an
unterminated tail that *does not parse* gets this treatment — a complete
final line that merely lost its newline (an editor, a rewrite) is kept,
and a bad line in the *middle* of the file is tampering or truncation,
and stays broken.

The chain is trusted up to the first break, and no further. A hash chain
cannot say *which* of two neighbours was rewritten, only that the link
between them broke — so at a break, the entry before it and everything
after are evidence, not proof. Rewrite a ledger line in place to match a
forged record, or append junk that re-claims an earlier id, and the chain
is broken *and* that record does not read `sealed`: it reads **disputed**
(matches an entry the chain cannot vouch for), or `altered` if its
trusted seal survives and disagrees. A trusted verdict is never overruled
by a later untrusted entry.

## Verify

```bash
mnemo-cortex ledger verify --agent cc     # one tenant
mnemo-cortex ledger verify --all          # every tenant on this host
mnemo-cortex ledger verify --all --json   # for scripts; exit 1 if anything is wrong
```

```
  cc: chain intact (4213 entries) · sealed 4213 · altered 0 · missing 0 · unsealed 0
```

Or over HTTP, from any client that can reach the server:

```
GET /ledger/verify?agent_id=cc
```

Per-record states:

| state | meaning |
|---|---|
| `sealed` | on disk, and the testimony matches its latest ledger entry |
| `altered` | on disk, but the words differ from what was sealed |
| `missing` | sealed, but the file is gone — Mnemo never deletes memories (demotion keeps the JSON), so this is news |
| `unsealed` | on disk with no ledger entry — pre-ledger stores, Cortex Stick carries, or a tool that wrote past the server |
| `disputed` | broken chain only: on disk and matching an entry past the break, which the chain cannot vouch for |

`resealed` lists every id the trusted chain sealed more than once with
**different testimony** (any sealed field: the words, and also `timestamp`,
`session_id`, `agent_id`): `{memory_id, seqs, distinct}`. A fixed-id writer does
this in the open (an archived session summarised again under the same id, or
a re-send whose `decisions_made` / `projects_referenced` changed: those are
sealed but not part of the id). The record reads `sealed` against its latest
seal; `resealed` says the earlier testimony existed. **Report-only**: it never
changes `ok`. The same testimony sealed twice is not a reseal; the same
text sealed again with a new timestamp is (an Analyst note re-written on a
later day stamps a new `timestamp` and `session_id`).

Chain states: `intact` / `broken` (with the sequence number and the reason)
/ `empty`.

`ok` is true when the chain is intact (or empty) and nothing is altered or
missing. **Unsealed is not a failure** — it is a to-do.

## Seal

Adopt every record the ledger has never seen:

```bash
mnemo-cortex ledger seal --agent cc
mnemo-cortex ledger seal --all
```

Run it once after upgrading to 4.20 (your existing memories are all
unsealed until you do), and after every Cortex Stick sync (carried records
arrive unsealed on the receiving host — each host keeps its own chain).

The CLI probes `/health` on the configured host and port first. If a server
answers, the seal goes through `POST /ledger/seal`, because the server
owns the chain's tail (a second writer on the same file would fork it).
If the port refuses the connection, the CLI writes the file directly. On
anything ambiguous — a timeout, a non-200 — it refuses and says so.

Two refusals are deliberate. An **altered** record is never re-sealed:
that would launder a tamper into a fresh seal. It stays named until a
human decides — fix the file, or accept the new words by saving them
properly. And a **broken chain is never adopted onto** (HTTP 409, CLI exit
1, report attached): with a ledger line deleted, a forged record's seal is
gone too, and "adopt the unsealed" would seal the forgery. Repair the
chain first — restore `ledger.jsonl` from backup, or archive it and start
a fresh chain knowingly.

## What this is, and what it is not

This is **local evidence**, not third-party proof. Whoever can write your
memory files can also rewrite the ledger from scratch. What the ledger
catches is the ordinary disaster — a bad migration, a half-finished script,
a sync that mangled a file, a stick that carried something the origin
never wrote — and it catches it every time, with zero dependencies beyond
Python's standard library.

Filing changes never trip it. Reclassify a memory, supersede it, tag it,
mark it for the dreamer: the seal holds, because the seal is over the
words, not the filing.

## Credit

The idea comes from [MAYA Memory Lane](https://github.com/MAYA-Platform/MAYA-Memory-Lane)'s
sealed session blocks (SHA-256 fingerprints folding into a chain). Built
here from zero to fit Mnemo's per-record JSON store and multi-tenant layout.
