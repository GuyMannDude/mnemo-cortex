# Sparks Bus ↔ A2A Mapping

Sparks Bus is data-shape compatible with [Google's A2A protocol](https://google.github.io/A2A/) today. Transport-compatible (HTTPS / JSON-RPC) is the v2 roadmap. This doc records the field-by-field mapping so external A2A tooling can interoperate now.

## Why now

External agents will increasingly speak A2A. Adopting the data shape early means:

- Agent identity ports straight over: each agent gets an A2A Agent Card in `agent-cards/`.
- The bus message → A2A Task transform is one function (`to_a2a_task` in the watcher) — no schema rewrite later.
- When an external HTTPS transport lands, the gateway just translates the same shapes back and forth.

What we are *not* doing yet: standing up a public A2A endpoint, registering with directories, doing capability negotiation. Internal agents already know each other.

## Agent Cards

Five cards in `agent-cards/`, one per agent:

| File | Agent | Notes |
|---|---|---|
| `cc.json` | CC | Builder on IGOR, claude-method delivery |
| `rocky.json` | Rocky | Production agent, Discord-method via #rocky-log |
| `opie.json` | Opie | Architect on Claude Desktop, queue-method (pull) |
| `bw.json` | BW | Research agent in Docker, Discord-method via #bw-research |
| `cliff.json` | Cliff | Parallel research agent in Docker, Discord-method via #dispatch |

Each card carries: `name`, `description`, `url`, `capabilities[]`, `inputModes[]`, `outputModes[]`, `protocol`, plus a Sparks-Bus-specific `delivery` block describing how the watcher wakes that agent. The `url` is forward-looking — the agent's HTTPS endpoint when transport ships. Today it serves as a stable identifier.

## Task object mapping

A2A Tasks are the unit of work. Sparks Bus rows already carry the same information; the mapping is direct.

| Sparks Bus column | A2A Task field | Notes |
|---|---|---|
| `tracking_id` | `id` | Globally unique. Format: `bus-{id}-{iso}` or `bus-reply-{id}-{iso}`. |
| `subject` | `name` | Short human-readable label. |
| `body` | `input` | JSON object or text; preserved verbatim. |
| `from_agent` | `metadata.from` | Sender's name. |
| `to_agent` | `metadata.to` | Recipient's name. |
| `reply_to` | `metadata.reply_to` | If non-null, this task is a reply. |
| `created_at` | `metadata.created_at` | ISO timestamp. |
| `mnemo_saved_at` | `artifact` (when set) | `{ type: "mnemo-session", session_id: tracking_id }`. |
| (lifecycle) | `state` | See next section. |
| (constant) | `protocol` | `"sparks-bus-a2a"`. |

The watcher exposes `to_a2a_task(msg_row, lifecycle_state)` — call this anywhere you need the A2A view of a row.

## Lifecycle state mapping

Sparks Bus has more granular states than A2A; we collapse them to the closest A2A `TaskState`:

| Sparks Bus state | A2A `TaskState` | When |
|---|---|---|
| CREATED | `submitted` | Row inserted; nothing observed yet. |
| DELIVERED (📬) | `submitted` | Watcher saw it, payload saved, receipt posted. Agent hasn't picked up. |
| PICKED UP (✅) | `working` | Recipient agent has read the message. |
| REPLIED (🔄) | `completed` | Recipient sent a reply (a new row with `reply_to` set). The original task is done. |
| DELIVERY FAILED (⚠️) | `failed` | Wake-up errored. One-shot alert; row excluded from retries. |
| STALE (⚠️) | `submitted` | Still waiting; never moved past submitted. (A2A has no "stalled" state.) |

The `to_a2a_task` helper accepts a lifecycle hint string and emits the right A2A state.

## Forward path: HTTPS / JSON-RPC transport (deferred)

When v2 ships an HTTPS transport:

1. Each agent's `url` becomes a real endpoint that accepts A2A tasks.
2. A gateway translates inbound A2A tasks → bus message rows (and back for results).
3. Existing Sparks Bus consumers see no change — the mapping is the same in both directions.
4. External agents discover Sparks agents via standard A2A directories (registration optional).

No changes to the bus database, no changes to the watcher's poll loop, no changes to agent code. The translation layer is additive.

## What this gives external tooling today

- A2A clients can read the agent cards and know what each Sparks agent does.
- Anything that consumes the watcher's logs or queries the bus DB can render rows as A2A Tasks via `to_a2a_task`.
- The schema is forward-compatible: when transport ships, the data already speaks the right language.
