// Which brain files are THIS agent's lane.
//
// Default: named after the Mnemo tenant (`<agent>.md` / `<agent>-session.md`).
// MNEMO_LANE overrides that for an agent that shares a tenant with another
// agent but keeps its own lane — CC2 on IGOR-2 runs as MNEMO_AGENT_ID=cc
// (one memory store with CC, by design) and its lane is cc2-igor2.md.
// Without the override, agent_startup on IGOR-2 loads CC's lane, and the
// lane guard lets CC2 overwrite it.
//
// Used by agent_startup (which lane to load), session_end (which lane to
// nag/budget-check) and write_brain_file (which protected lane is yours).

export function laneCandidates(agentId, laneOverride) {
  const override = (laneOverride || "").trim();
  if (override) return [override.endsWith(".md") ? override : `${override}.md`];
  return [`${agentId}.md`, `${agentId}-session.md`];
}

// Session-id prefix. With MNEMO_LANE set, the lane's short name (the part
// before the first "-" or ".") follows the tenant: cc3-igor.md on tenant cc
// → "cc-cc3", so CC3's and CC2's sessions don't read as CC's. The tenant
// stays first, so "<agent>-" prefix readers keep working. No override, or a
// lane named after the tenant itself (cc-session.md) → just the tenant.
// Server-side session ids must match [A-Za-z0-9_-]; the tag is kept to that.
export function sessionPrefix(agentId, laneOverride) {
  const tag = (laneOverride || "").trim().split(/[-.]/)[0].replace(/[^A-Za-z0-9_]/g, "");
  return tag && tag !== agentId ? `${agentId}-${tag}` : agentId;
}
