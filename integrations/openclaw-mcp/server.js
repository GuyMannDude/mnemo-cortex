import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

// ── Configuration ──────────────────────────────────────────────
// MNEMO_URL: where your Mnemo Cortex API lives
// MNEMO_AGENT_ID: who this agent is in the memory system
// MNEMO_SHARE: cross-agent sharing mode (separate|always|never)
//
// OpenClaw users set these via env vars in their MCP config.
// Defaults point to a local Mnemo Cortex instance.

const MNEMO_URL = process.env.MNEMO_URL || "http://localhost:50001";
const AGENT_ID = process.env.MNEMO_AGENT_ID || "openclaw";

const SHARE_MODES = ["separate", "always", "never"];
const shareMode = SHARE_MODES.includes(process.env.MNEMO_SHARE)
  ? process.env.MNEMO_SHARE
  : "separate";
let sessionShareActive = shareMode === "always";

const FETCH_TIMEOUT_MS = 10_000;
const MAX_RESPONSE_CHARS = 16_000;

// ── Mnemo API client ───────────────────────────────────────────
// Two methods: POST for writes/searches, GET for reads.
// 10-second timeout on all requests. Errors surface as tool
// errors — the agent sees a clean message, not a stack trace.

async function mnemoRequest(method, path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
    signal: controller.signal,
  };
  if (body) opts.body = JSON.stringify(body);

  let res;
  try {
    res = await fetch(`${MNEMO_URL}${path}`, opts);
  } catch (err) {
    clearTimeout(timer);
    if (err.name === "AbortError") {
      process.stderr.write(
        `[mnemo-mcp] Request timed out: ${method} ${path} (${FETCH_TIMEOUT_MS}ms) to ${MNEMO_URL}\n`
      );
      throw new Error(
        "Mnemo Cortex request timed out. The server may be overloaded or unreachable."
      );
    }
    process.stderr.write(
      `[mnemo-mcp] Connection failed: ${method} ${path} to ${MNEMO_URL} — ${err.message}\n`
    );
    throw new Error(
      "Cannot reach Mnemo Cortex. Is it running?"
    );
  }
  clearTimeout(timer);

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    process.stderr.write(
      `[mnemo-mcp] HTTP error: ${method} ${path} → ${res.status}: ${text}\n`
    );
    throw new Error(`Mnemo Cortex returned ${res.status}: ${text}`);
  }

  let data;
  try {
    data = await res.json();
  } catch {
    process.stderr.write(
      `[mnemo-mcp] Invalid JSON response: ${method} ${path}\n`
    );
    throw new Error("Mnemo Cortex returned an invalid response.");
  }

  return data;
}

// ── Health check on startup ────────────────────────────────────
// Verify Mnemo Cortex is reachable. If healthy, tools work
// immediately. If not, tools will attempt to connect on each
// call and return clear errors if Mnemo is still down.

let mnemoHealthy = false;

async function checkHealth() {
  try {
    const h = await mnemoRequest("GET", "/health");
    if (h.status === "ok") {
      mnemoHealthy = true;
      process.stderr.write(
        `[mnemo-mcp] Connected to Mnemo Cortex (${h.memory_entries} memories, share: ${shareMode})\n`
      );
    }
  } catch {
    process.stderr.write(
      `[mnemo-mcp] WARNING: Mnemo Cortex not reachable. Tools will retry on each call.\n`
    );
  }
}

async function ensureHealth() {
  if (!mnemoHealthy) {
    // Mnemo was down at startup — try once more before failing
    try {
      const h = await mnemoRequest("GET", "/health");
      if (h.status === "ok") {
        mnemoHealthy = true;
        process.stderr.write(
          `[mnemo-mcp] Mnemo Cortex reconnected (${h.memory_entries} memories)\n`
        );
      }
    } catch {
      throw new Error(
        "Mnemo Cortex is not connected. It may be down or unreachable."
      );
    }
  }
}

// ── Format memory chunks for display ───────────────────────────

function formatChunks(chunks, showAgent) {
  if (!chunks || chunks.length === 0) return "No memories found.";

  const parts = [];
  let chars = 0;
  let included = 0;

  for (const c of chunks) {
    const rel = (c.relevance || 0).toFixed(2);
    const tier = c.cache_tier || "?";
    const header = showAgent
      ? `[${tier}] agent=${c.agent_id || "?"} (relevance: ${rel})`
      : `[${tier}] (relevance: ${rel})`;
    const block = `### ${header}\n${c.content}`;

    if (chars + block.length > MAX_RESPONSE_CHARS && included > 0) {
      const remaining = chunks.length - included;
      parts.push(
        `[Results capped — ${remaining} more memories matched. Narrow your query for more detail.]`
      );
      break;
    }

    parts.push(block);
    chars += block.length;
    included++;
  }

  return parts.join("\n\n");
}

// ── MCP Server ─────────────────────────────────────────────────

const server = new McpServer({
  name: "mnemo-cortex",
  version: "2.0.1",
});

// ── Tool: mnemo_recall ─────────────────────────────────────────
// Semantic recall within this agent's own memories.
// Use this when the agent needs to remember something from
// its own past sessions. Not affected by share mode.

server.tool(
  "mnemo_recall",
  `Recall memories from Mnemo Cortex for the current agent (${AGENT_ID}). Returns semantically relevant chunks from past sessions.`,
  {
    query: z
      .string()
      .max(10000)
      .describe("What to search for in memory"),
    max_results: z
      .number()
      .int()
      .min(1)
      .max(20)
      .optional()
      .describe("Maximum number of memories to return (default: 3)"),
  },
  async ({ query, max_results }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/context", {
        prompt: query,
        agent_id: AGENT_ID,
        max_results: max_results || 3,
      });

      const chunks = data.chunks || [];
      const text = formatChunks(chunks, false);
      const count = data.total_found || chunks.length;

      return {
        content: [
          {
            type: "text",
            text: count > 0 ? `Found ${count} memories:\n\n${text}` : text,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Recall error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: mnemo_search ─────────────────────────────────────────
// Cross-agent search. Gated by share mode:
// - separate (default): restricted to own agent_id
// - always: cross-agent search enabled
// - never: permanently restricted, toggle blocked
// - session toggle via mnemo_share tool

server.tool(
  "mnemo_search",
  "Search memories in Mnemo Cortex. By default, searches only your own memories. Use mnemo_share to enable cross-agent search for this session.",
  {
    query: z
      .string()
      .max(10000)
      .describe("What to search for"),
    agent_id: z
      .string()
      .optional()
      .describe(
        "Filter to a specific agent (rocky, cc, opie). Only works when cross-agent sharing is enabled. Omit for all."
      ),
    max_results: z
      .number()
      .int()
      .min(1)
      .max(20)
      .optional()
      .describe("Maximum number of memories to return (default: 3)"),
  },
  async ({ query, agent_id, max_results }) => {
    try {
      await ensureHealth();
      const body = {
        prompt: query,
        max_results: max_results || 3,
      };

      if (sessionShareActive) {
        // Cross-agent search allowed
        if (agent_id) body.agent_id = agent_id;
        // If no agent_id, search all agents (no filter)
      } else {
        // Restricted — force filter to own agent
        body.agent_id = AGENT_ID;
      }

      const data = await mnemoRequest("POST", "/context", body);
      const chunks = data.chunks || [];
      const text = formatChunks(chunks, sessionShareActive);
      const count = data.total_found || chunks.length;

      let prefix = "";
      if (!sessionShareActive) {
        prefix =
          "(Restricted to your own memories. Use mnemo_share to enable cross-agent search.)\n\n";
      }

      return {
        content: [
          {
            type: "text",
            text:
              count > 0
                ? `${prefix}Found ${count} memories:\n\n${text}`
                : `${prefix}${text}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Search error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: mnemo_save ───────────────────────────────────────────
// Write a memory to Mnemo Cortex. Use at session end or when
// something important happens that should be remembered.
// Always writes to this agent's own memory slot.

server.tool(
  "mnemo_save",
  "Save a summary or key facts to Mnemo Cortex for future recall. Use at session end or when something important should be remembered.",
  {
    summary: z
      .string()
      .max(10000)
      .describe("Summary of what happened or what to remember"),
    key_facts: z
      .array(z.string().max(1000))
      .optional()
      .describe("List of key facts to store (one fact per item)"),
    session_id: z
      .string()
      .optional()
      .describe("Session identifier. Auto-generated if omitted."),
  },
  async ({ summary, key_facts, session_id }) => {
    try {
      await ensureHealth();
      const sid =
        session_id ||
        `${AGENT_ID}-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-")}`;

      const data = await mnemoRequest("POST", "/writeback", {
        session_id: sid,
        summary,
        key_facts: key_facts || [],
        projects_referenced: [],
        decisions_made: [],
        agent_id: AGENT_ID,
      });

      return {
        content: [
          {
            type: "text",
            text: `Saved to Mnemo Cortex.\n  memory_id: ${data.memory_id || "ok"}\n  session: ${sid}\n  agent: ${AGENT_ID}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Save error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: mnemo_share ──────────────────────────────────────────
// Toggle cross-agent memory sharing for this session.
// Respects the MNEMO_SHARE config: never blocks, always is a
// no-op, separate allows per-session toggle.

server.tool(
  "mnemo_share",
  "Toggle cross-agent memory sharing for this session. When on, mnemo_search can read memories from all agents. When off, search is limited to this agent only.",
  {},
  async () => {
    if (shareMode === "never") {
      return {
        content: [
          {
            type: "text",
            text: "Cross-agent sharing is disabled for this agent. This cannot be overridden.",
          },
        ],
      };
    }
    if (shareMode === "always") {
      return {
        content: [
          {
            type: "text",
            text: "Cross-agent sharing is always on for this agent. Toggle not needed.",
          },
        ],
      };
    }
    sessionShareActive = !sessionShareActive;
    return {
      content: [
        {
          type: "text",
          text: `Cross-agent sharing is now ${sessionShareActive ? "ON" : "OFF"} for this session.`,
        },
      ],
    };
  }
);

// ── Tool: passport_get_user_context ────────────────────────────
// Read the user's portable working-style passport. Returns both
// a structured claims list AND a prompt-ready text block ready
// to drop into Custom Instructions or a system prompt. Calibrates
// tone, workflow defaults, and negative constraints at session start.

server.tool(
  "passport_get_user_context",
  "Read the user's portable working-style passport. Returns a prompt-ready text block plus structured claims. Call at session start to calibrate tone, workflow defaults, and negative constraints.",
  {
    scopes: z
      .array(z.string())
      .optional()
      .describe("Filter by scope tags (general, build_mode, debug_mode, research_mode, public_facing). Omit for all."),
    platform: z
      .string()
      .optional()
      .describe("Platform hint (chatgpt, claude, gemini). Reserved for Phase 2 adapter layer."),
    max_claims: z
      .number()
      .int()
      .min(1)
      .max(100)
      .optional()
      .describe("Cap the number of claims returned (default: 20)"),
  },
  async ({ scopes, platform, max_claims }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/context", {
        scopes: scopes || null,
        platform: platform || null,
        max_claims: max_claims || 20,
      });
      const n = data.claims?.length || 0;
      const o = data.overlays?.length || 0;
      return {
        content: [
          {
            type: "text",
            text: `${data.prompt_block}\n\n---\n*Structured: ${n} claim(s), ${o} overlay(s), passport v${data.passport_version}*`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Passport context error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: passport_observe_behavior ────────────────────────────
// Record a candidate observation about the user's working style.
// Requires 2+ evidence turn refs. Observations land in the pending
// queue — they are NOT promoted into the stable passport
// automatically. Never write credentials, project secrets, or
// client data. Bad claim: "User is awesome." Good claim:
// "Prefers direct answers with minimal fluff."

server.tool(
  "passport_observe_behavior",
  "Record a candidate observation about the user's working style. REQUIRES 2+ evidence turn refs (minimum). Lands in pending queue; does NOT promote automatically. Never include credentials, project secrets, or client data.",
  {
    proposed_claim: z
      .string()
      .max(180)
      .describe("Atomic, testable claim (≤180 chars). E.g. 'Prefers direct answers with minimal fluff.'"),
    type: z
      .enum([
        "preference",
        "workflow_default",
        "negative_constraint",
        "style_default",
        "decision_pattern",
        "mode_trait",
      ])
      .describe("Claim type."),
    scope: z
      .array(z.string())
      .optional()
      .describe("Scope tags: general, build_mode, debug_mode, research_mode, public_facing, personal, professional."),
    confidence: z
      .number()
      .min(0)
      .max(1)
      .describe("Self-assessment 0.0–1.0."),
    proposed_target_section: z
      .string()
      .describe("Dotted path for promotion. E.g. 'stable_core.communication', 'stable_core.workflow', 'negative_constraints'."),
    source_platform: z
      .string()
      .describe("Where the interaction happened (chatgpt, claude, cc, opie, rocky)."),
    source_session_id: z
      .string()
      .describe("Session identifier (free-form)."),
    evidence: z
      .array(
        z.object({
          turn_ref: z.string().describe("Turn identifier, e.g. 'u12-a12'."),
          excerpt: z.string().max(400).describe("Short verbatim excerpt (≤400 chars). Never a full transcript."),
        })
      )
      .min(2)
      .describe("MINIMUM 2 evidence items. Fewer = rejected."),
  },
  async (args) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/observe", args);
      if (data.status === "rejected") {
        const dup = data.duplicate_of ? ` (duplicate of ${data.duplicate_of})` : "";
        return {
          content: [
            {
              type: "text",
              text: `Observation rejected: ${data.rejection_reason}${dup}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Pending: ${data.observation_id}\nCommit: ${data.commit_sha?.slice(0, 7) || "—"}\nAwaiting passport_promote_observation to land in the stable passport.`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Passport observe error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: passport_list_pending_observations ───────────────────
// List candidate observations waiting in the pending queue.
// Use before promoting to see what's staged.

server.tool(
  "passport_list_pending_observations",
  "List candidate observations waiting in the pending queue. Filter by status (pending|promoted).",
  {
    status: z
      .enum(["pending", "promoted"])
      .optional()
      .describe("Filter by status (default: pending)"),
    limit: z
      .number()
      .int()
      .min(1)
      .max(200)
      .optional()
      .describe("Cap items returned (default: 25)"),
  },
  async ({ status, limit }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/pending", {
        status: status || "pending",
        limit: limit || 25,
      });
      const items = data.items || [];
      if (items.length === 0) {
        return { content: [{ type: "text", text: "No pending observations." }] };
      }
      const lines = items.map(
        (o) =>
          `- ${o.observation_id} [${o.type}] conf=${o.confidence} → ${o.proposed_target_section}\n  "${o.proposed_claim}"`
      );
      return {
        content: [{ type: "text", text: `${items.length} pending:\n\n${lines.join("\n\n")}` }],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Passport list error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: passport_promote_observation ─────────────────────────
// Move a pending observation into the stable passport. This is
// the gate between candidate and canonical — only promote claims
// you're confident are accurate and evidence-backed.

server.tool(
  "passport_promote_observation",
  "Move a pending observation into the stable passport. Only promote claims you're confident in — this is the gate between candidate and canonical.",
  {
    observation_id: z
      .string()
      .describe("The obs_NNN id from passport_list_pending_observations."),
    target_section: z
      .string()
      .optional()
      .describe("Override the observation's proposed target. Dotted path, e.g. 'stable_core.communication'."),
    actor: z
      .string()
      .optional()
      .describe("Who is promoting (user, opie, cc, system). Default: system."),
  },
  async ({ observation_id, target_section, actor }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/promote", {
        observation_id,
        target_section: target_section || null,
        actor: actor || "system",
      });
      if (!data.promoted) {
        return {
          content: [{ type: "text", text: `Promotion failed: ${data.reason}` }],
          isError: true,
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Promoted ${observation_id} → ${data.claim_id} (${data.target_section})\nCommit: ${data.commit_sha?.slice(0, 7) || "—"}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Passport promote error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: passport_forget_or_override ──────────────────────────
// Deprecate, forget, or replace an existing stable claim.
// - override: replace wording and preserve lineage via supersedes
// - forget:   remove claim entirely (audit keeps the snapshot)
// - deprecate: retire without replacement

server.tool(
  "passport_forget_or_override",
  "Deprecate, forget, or replace an existing stable claim. Use override (with replacement_claim) to correct wording while preserving lineage. Use forget to remove a claim entirely. Use deprecate to retire without replacement.",
  {
    action: z
      .enum(["deprecate", "forget", "override", "replace"])
      .describe("deprecate=retire; forget=remove; override/replace=new wording with supersedes link."),
    target_claim_id: z
      .string()
      .describe("The claim_id to act on, e.g. 'pref_prefers_001'."),
    replacement_claim: z
      .string()
      .max(180)
      .optional()
      .describe("Required for action=override/replace. The corrected claim text."),
    reason: z
      .string()
      .optional()
      .describe("Free-text reason (lands in the audit log)."),
    actor: z
      .string()
      .optional()
      .describe("user | opie | cc | system. Default: user."),
  },
  async ({ action, target_claim_id, replacement_claim, reason, actor }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/override", {
        action,
        target_claim_id,
        replacement_claim: replacement_claim || null,
        reason: reason || null,
        actor: actor || "user",
      });
      if (!data.success) {
        return {
          content: [{ type: "text", text: `Action failed: ${data.reason}` }],
          isError: true,
        };
      }
      const line = data.new_claim_id
        ? `${action} ${target_claim_id} → ${data.new_claim_id}`
        : `${action} ${target_claim_id}`;
      return {
        content: [
          {
            type: "text",
            text: `${line}\nAudit: ${data.override_id}\nCommit: ${data.commit_sha?.slice(0, 7) || "—"}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Passport override error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Start ──────────────────────────────────────────────────────

await checkHealth();
const transport = new StdioServerTransport();
await server.connect(transport);
