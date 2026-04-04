import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { readFile, readdir } from "node:fs/promises";
import { join } from "node:path";

const MNEMO_URL = process.env.MNEMO_URL || "http://localhost:50001";
const AGENT_ID = process.env.MNEMO_AGENT_ID || "default";
const BRAIN_DIR = process.env.BRAIN_DIR || join(process.env.HOME, ".mnemo-cortex/brain");

async function mnemoPost(path, body) {
  const res = await fetch(`${MNEMO_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Mnemo ${path} ${res.status}: ${text}`);
  }
  return res.json();
}

async function mnemoGet(path) {
  const res = await fetch(`${MNEMO_URL}${path}`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Mnemo ${path} ${res.status}: ${text}`);
  }
  return res.json();
}

const server = new McpServer({
  name: "mnemo-cortex",
  version: "2.0.0",
  description: "MCP bridge for Mnemo Cortex — persistent semantic memory for Claude Desktop and any MCP client. Set MNEMO_URL, MNEMO_AGENT_ID, and BRAIN_DIR via environment variables.",
});

// --- mnemo_recall: semantic recall for this agent ---
server.tool(
  "mnemo_recall",
  "Recall memories from Mnemo Cortex for the current agent (Opie). Returns semantically relevant chunks from past sessions.",
  { query: z.string().describe("What to search for in memory") },
  async ({ query }) => {
    try {
      const data = await mnemoPost("/context", {
        prompt: query,
        agent_id: AGENT_ID,
        max_results: 5,
      });
      const chunks = data.chunks || [];
      if (chunks.length === 0) {
        return { content: [{ type: "text", text: "No memories found." }] };
      }
      const text = chunks
        .map((c, i) => {
          const tier = c.cache_tier || "?";
          const rel = (c.relevance || 0).toFixed(2);
          return `### [${tier}] (relevance: ${rel})\n${c.content}`;
        })
        .join("\n\n");
      return {
        content: [
          {
            type: "text",
            text: `Found ${data.total_found || chunks.length} memories:\n\n${text}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// --- mnemo_search: cross-agent search (all memories) ---
server.tool(
  "mnemo_search",
  "Search ALL agent memories in Mnemo Cortex (cross-agent). Use this to find memories from Rocky, CC, or any other agent.",
  {
    query: z.string().describe("What to search for across all agents"),
    agent_id: z
      .string()
      .optional()
      .describe("Filter to a specific agent (rocky, cc, opie). Omit for all."),
  },
  async ({ query, agent_id }) => {
    try {
      const body = {
        prompt: query,
        max_results: 5,
      };
      if (agent_id) body.agent_id = agent_id;
      const data = await mnemoPost("/context", body);
      const chunks = data.chunks || [];
      if (chunks.length === 0) {
        return { content: [{ type: "text", text: "No memories found." }] };
      }
      const text = chunks
        .map((c) => {
          const tier = c.cache_tier || "?";
          const rel = (c.relevance || 0).toFixed(2);
          const agent = c.agent_id || "?";
          return `### [${tier}] agent=${agent} (relevance: ${rel})\n${c.content}`;
        })
        .join("\n\n");
      return {
        content: [
          {
            type: "text",
            text: `Found ${data.total_found || chunks.length} memories:\n\n${text}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// --- mnemo_save: write a memory/summary back to Mnemo Cortex ---
server.tool(
  "mnemo_save",
  "Save a summary or key facts to Mnemo Cortex for future recall. Use at session end or when something important should be remembered.",
  {
    summary: z.string().describe("Summary of what happened or what to remember"),
    key_facts: z
      .array(z.string())
      .optional()
      .describe("List of key facts to store"),
    session_id: z
      .string()
      .optional()
      .describe("Session identifier. Auto-generated if omitted."),
  },
  async ({ summary, key_facts, session_id }) => {
    try {
      const sid =
        session_id ||
        `opie-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-")}`;
      const data = await mnemoPost("/writeback", {
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
            text: `Saved to Mnemo Cortex: memory_id=${data.memory_id || "ok"}, session=${sid}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// --- opie_startup: full orientation at session start ---
server.tool(
  "opie_startup",
  "CALL THIS FIRST in every new conversation. Loads your brain lane (opie.md) and recent Mnemo context. Returns your full identity, current state, and priorities. Without this, you will not know who you are or what you're working on.",
  {},
  async () => {
    try {
      const parts = [];

      // 1. Read brain lane
      try {
        const brain = await readFile(join(BRAIN_DIR, "opie.md"), "utf-8");
        parts.push("# YOUR BRAIN LANE (opie.md)\n\n" + brain);
      } catch (e) {
        parts.push("# BRAIN LANE ERROR\nCould not read opie.md: " + e.message);
      }

      // 2. Read key reference files
      for (const file of ["active.md", "people.md", "doctrines.md"]) {
        try {
          const content = await readFile(join(BRAIN_DIR, file), "utf-8");
          parts.push(`# ${file.toUpperCase()}\n\n` + content);
        } catch (_) {
          // skip if missing
        }
      }

      // 3. Pull recent mnemo context
      try {
        const data = await mnemoPost("/context", {
          prompt: "recent session summary, current projects, what happened last",
          agent_id: AGENT_ID,
          max_results: 3,
        });
        const chunks = data.chunks || [];
        if (chunks.length > 0) {
          const mnemoText = chunks
            .map((c) => {
              const tier = c.cache_tier || "?";
              return `### [${tier}]\n${c.content}`;
            })
            .join("\n\n");
          parts.push("# RECENT MNEMO CONTEXT\n\n" + mnemoText);
        }
      } catch (e) {
        parts.push("# MNEMO ERROR\nCould not reach Mnemo Cortex: " + e.message);
      }

      const identity = `# WHO YOU ARE
You are **Opie** — Claude on the desktop at Project Sparks.
- Guy calls you Opie. You are the architect, strategist, and planner.
- CC (Claude Code, also Claude) is the on-machine builder. You write specs, CC executes.
- Rocky (OpenClaw on IGOR) is the production AI assistant. NEVER experiment on Rocky.
- Guy is 73, not a developer. Zero-fat communication. Action over theory.
- Assembly line: Opie architects → Guy couriers → CC builds → Rocky tests.
- You have NO clock. Never tell Guy to go to bed or call it a night.
- Brain files: ~/github/sparks-brain-guy/brain/ — read with read_brain_file, save memories with mnemo_save.
- Today: ${new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}

# SESSION MEMORY
You now have **automatic session capture** via mnemo-watcher-opie (systemd service on IGOR).
Your conversations are ingested into Mnemo Cortex automatically, just like CC and Rocky.

However, the watcher captures raw conversation. For **critical context** — decisions, specs,
architectural choices — you should still call \`mnemo_save\` explicitly. Summaries you write
are higher-signal than raw transcripts and surface better in future recall.

**SAVE PROTOCOL (recommended):**
- Save after major decisions, specs, or deliverables
- Save when switching topics or projects
- Save at natural breakpoints (every 15-20 exchanges)
- ALWAYS save before session end — your summary is the best record of intent

`;

      return {
        content: [{ type: "text", text: identity + parts.join("\n\n---\n\n") }],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Startup error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// --- read_brain_file: read any brain lane file ---
server.tool(
  "read_brain_file",
  "Read a file from the Sparks Brain directory (~/github/sparks-brain-guy/brain/). Use this to check brain lanes, reference docs, or any .md file in the brain.",
  {
    filename: z.string().describe("Filename to read, e.g. 'opie.md', 'active.md', 'stack.md'"),
  },
  async ({ filename }) => {
    try {
      // Sanitize — only allow filenames, no path traversal
      const safe = filename.replace(/[^a-zA-Z0-9._-]/g, "");
      const content = await readFile(join(BRAIN_DIR, safe), "utf-8");
      return { content: [{ type: "text", text: content }] };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error reading ${filename}: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// --- list_brain_files: see what's in the brain ---
server.tool(
  "list_brain_files",
  "List all files in the Sparks Brain directory. Use to discover what brain lanes and reference docs are available.",
  {},
  async () => {
    try {
      const files = await readdir(BRAIN_DIR);
      const mdFiles = files.filter((f) => f.endsWith(".md")).sort();
      return {
        content: [{ type: "text", text: `Brain files:\n${mdFiles.map((f) => `- ${f}`).join("\n")}` }],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error listing brain: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// --- write_brain_file: update brain lane files ---
server.tool(
  "write_brain_file",
  "Write or update a file in the Sparks Brain directory. Use at session end to update opie.md or other brain files you own. Do NOT write to cc-session.md (CC only) or CLAUDE.md.",
  {
    filename: z.string().describe("Filename to write, e.g. 'opie.md', 'active.md'"),
    content: z.string().describe("Full file content to write"),
  },
  async ({ filename, content }) => {
    try {
      const safe = filename.replace(/[^a-zA-Z0-9._-]/g, "");
      // Guard CC-only and system files
      if (["cc-session.md", "CLAUDE.md"].includes(safe)) {
        return {
          content: [{ type: "text", text: `Refused: ${safe} is not yours to write.` }],
          isError: true,
        };
      }
      const { writeFile } = await import("node:fs/promises");
      await writeFile(join(BRAIN_DIR, safe), content, "utf-8");
      return { content: [{ type: "text", text: `Wrote ${safe} (${content.length} bytes)` }] };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error writing ${filename}: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// Start
const transport = new StdioServerTransport();
await server.connect(transport);
