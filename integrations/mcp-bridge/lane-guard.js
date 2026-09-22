// Write protection for write_brain_file.
//
// CLAUDE.md (the cross-agent operating doc) is refused for everyone —
// unconditionally, so a spoofy MNEMO_AGENT_ID=CLAUDE can't unlock it;
// edit it deliberately on disk instead.
//
// Lane-protected files may only be written by the agent whose lane they
// are. `ownedLanes` is the agent's lane-candidate list from
// lane-candidates.js (tenant-named, or the MNEMO_LANE override) — the same
// list agent_startup boots from, so an agent that shares a tenant but has
// its own lane (CC2 as MNEMO_AGENT_ID=cc) cannot write the tenant's lane.

const ALWAYS_REFUSED = ["CLAUDE.md"];
const LANE_PROTECTED = ["cc-session.md"];

export function refusesBrainWrite(filename, ownedLanes) {
  if (ALWAYS_REFUSED.includes(filename)) return true;
  if (!LANE_PROTECTED.includes(filename)) return false;
  return !ownedLanes.includes(filename);
}
