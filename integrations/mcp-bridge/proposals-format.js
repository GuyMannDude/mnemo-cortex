// mnemo_fact_proposals — the pure parts (2.33.0, server 4.26.0 envelope).
//
// kind defaults to "fact", and a fact listing goes to the same route with the
// same output as 2.32.0: an existing caller sees no change. Other kinds read
// GET /proposals?kind=… and show the envelope (target, quotes, score).

export const PROPOSAL_KINDS = ["fact", "memory_category", "memory_note", "all"];

export function listPath({ kind, status, limit } = {}) {
  const qs = new URLSearchParams();
  if (status) qs.set("status", status);
  if (limit) qs.set("limit", String(limit));
  if (!kind || kind === "fact") {
    const q = qs.toString();
    return `/facts/proposals${q ? "?" + q : ""}`;
  }
  qs.set("kind", kind);
  return `/proposals?${qs.toString()}`;
}

// Every kind resolves by id. The /facts route resolves every kind on a
// 4.26.0 server and is the only one a 4.25 server has — a bridge rolled out
// ahead of its server still resolves fact proposals.
export function resolvePath(id) {
  return `/facts/proposals/${id}/resolve`;
}

export function formatResolve(id, data) {
  const lines = [`Proposal #${id}: ${data.reason}`];
  if (!data.written) return lines.join("\n");
  if (data.authority === "n/a") {
    lines.push(`  memory category now = proposed value, was: ${data.previous_value} (first value kept as category_original)`);
  } else {
    lines.push(`  slot now = proposed value [verified], was: ${data.previous_value} [${data.previous_confidence}]; tier stays ${data.authority}`);
  }
  return lines.join("\n");
}

export function formatList(data) {
  const kind = data.kind && data.kind !== "fact" ? ` ${data.kind}` : "";
  if (!data.count) return `No ${data.status}${kind} proposals.`;
  const lines = [`${data.count} ${data.status}${kind} proposal(s):`];
  for (const p of data.proposals) {
    const ts = (p.seen_count > 1 && p.last_seen) ? p.last_seen : p.created_at;   // a repeat shows its latest date
    const when = ts ? new Date(ts * 1000).toISOString().slice(0, 16) : "?";
    const seen = p.seen_count > 1 ? ` x${p.seen_count}` : "";
    if (!p.target_kind || p.target_kind === "fact") {
      lines.push(`  #${p.id}${seen} [${p.status}] ${p.entity}.${p.attribute} (${p.authority}) ${when} by ${p.source_agent || "?"}`);
      lines.push(`      proposed: ${p.proposed_value}`);
      lines.push(`      current:  ${p.current_value}`);
      lines.push(`      evidence: ${p.evidence_source}`);
    } else {
      const score = p.confidence_score == null ? "" : ` confidence ${Number(p.confidence_score).toFixed(2)}`;
      lines.push(`  #${p.id}${seen} [${p.status}] ${p.target_kind} ${p.target_ref} ${when} by ${p.source_run || p.source_agent || "?"}${score}`);
      lines.push(`      proposed: ${p.proposed_value}`);
      lines.push(`      current:  ${p.current_value}`);
      for (const q of p.evidence_quotes || []) lines.push(`      quote:    ${q}`);
    }
    if (p.resolved_by) lines.push(`      resolved by ${p.resolved_by}: ${p.resolution_reason || ""}`);
  }
  return lines.join("\n");
}
