// transcript-format.js — mnemo_transcript's pure parts (Mnemo 4.25 archive tier).
//
// The archive tier holds whole client transcripts; an MCP reply cannot. So
// every reply is budgeted in characters, and a cut is never silent: a turns
// page that runs out of budget names the seq to resume from.

export const TURNS_PAGE = 50;          // default turns requested per call
export const REPLY_BUDGET = 60_000;    // characters of turn text per reply
export const SESSION_KEY_RE =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(_agent-[A-Za-z0-9]{1,64})?$/;

export function searchPath(agentId, q, limit) {
  const p = new URLSearchParams({ agent_id: agentId });
  if (q) p.set("q", q);
  if (limit) p.set("limit", String(limit));
  return `/transcripts?${p}`;
}

export function getPath(agentId, sessionId, format, from, to) {
  if (format === "turns") {
    const p = new URLSearchParams({ agent_id: agentId, from: String(from ?? 0) });
    const span = to !== undefined && to !== null ? to - (from ?? 0) + 1 : TURNS_PAGE;
    if (to !== undefined && to !== null) p.set("to", String(to));
    p.set("limit", String(Math.max(1, Math.min(span, 2000))));
    return `/transcripts/${sessionId}/turns?${p}`;
  }
  const p = new URLSearchParams({ agent_id: agentId, format });
  return `/transcripts/${sessionId}?${p}`;
}

export function formatSearch(data) {
  if (Array.isArray(data.sessions)) {
    if (data.sessions.length === 0) return "No transcripts archived for this agent yet.";
    return (
      `${data.sessions.length} archived session(s), newest first:\n\n` +
      data.sessions
        .map(
          (s) =>
            `- ${s.session_id}  ${s.first_ts || "?"} → ${s.last_ts || "?"}  ` +
            `${s.user_turns} user / ${s.assistant_turns} assistant turns  ` +
            `host=${s.host || "?"}${s.title ? `  "${s.title}"` : ""}`
        )
        .join("\n")
    );
  }
  const hits = data.results || [];
  if (hits.length === 0) return `No archived turn matches "${data.q}".`;
  return (
    `${hits.length} match(es) for "${data.q}":\n\n` +
    hits
      .map(
        (h) =>
          `- ${h.session_id} #${h.seq} [${h.ts || "?"}] ${h.role}/${h.kind}` +
          `${h.tool_name ? `(${h.tool_name})` : ""}${h.title ? `  "${h.title}"` : ""}\n  ${h.snippet}`
      )
      .join("\n") +
    `\n\nRead one with mnemo_transcript action=get session_id=<id> format=turns from=<seq>.`
  );
}

export function formatManifest(m) {
  const lines = [
    `Transcript ${m.session_id}${m.title ? ` — "${m.title}"` : ""}`,
    `  host: ${m.host || "?"}   agent: ${m.agent_id}   client: ${m.client_version || "?"}`,
    `  span: ${m.first_ts || "?"} → ${m.last_ts || "?"}`,
    `  turns: ${m.user_turns} user, ${m.assistant_turns} assistant, ` +
      `${m.tool_use} tool calls, ${m.indexed_turns} indexed rows`,
    `  size: ${m.bytes} bytes raw, ${m.lines} lines (${m.bad_lines} unparseable)`,
    `  redactions: ${m.redactions_applied}   uploads: ${m.uploads}   sha256: ${m.sha256}`,
  ];
  if (m.parent_session_id) lines.push(`  subagent of: ${m.parent_session_id}`);
  if (m.cwd) lines.push(`  cwd: ${m.cwd}`);
  return lines.join("\n");
}

// Render a turns page inside the budget. Returns { text, nextFrom }.
export function formatTurns(data, budget = REPLY_BUDGET) {
  const turns = data.turns || [];
  if (turns.length === 0) return { text: "No turns in that range.", nextFrom: null };
  const out = [];
  let used = 0;
  let nextFrom = data.next_from ?? null;
  for (const t of turns) {
    const head = `#${t.seq} [${t.ts || "?"}] ${t.role}/${t.kind}${t.tool_name ? `(${t.tool_name})` : ""}`;
    const block = `${head}\n${t.text}\n`;
    if (out.length > 0 && used + block.length > budget) {
      nextFrom = t.seq;
      break;
    }
    // A single turn larger than the whole budget is cut, and says so.
    if (block.length > budget) {
      out.push(`${head}\n${t.text.slice(0, budget)}\n[... turn #${t.seq} cut at ${budget} of ${t.text.length} chars; ` +
        `GET /transcripts/${data.session_id}?format=raw has it whole]\n`);
      used = budget;
      nextFrom = t.seq + 1;
      break;
    }
    out.push(block);
    used += block.length;
  }
  let text = out.join("\n");
  if (nextFrom !== null) text += `\n[more: call again with from=${nextFrom}]`;
  return { text, nextFrom };
}
