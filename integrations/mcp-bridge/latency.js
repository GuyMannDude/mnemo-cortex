// Latency log — slim, always-on record of every bridge tool call's
// wall-clock duration. Companion to dump.js: the Developer Dump captures
// params and responses and is too heavy to leave on, so a self-clearing
// hang between wedge-watch samples used to leave zero record
// (snag-mnemo-verbs-no-latency-record). One JSONL line per call —
// tool, ms, ok — measured bridge-side, so it includes the network time
// the server can never see. Opt out with MNEMO_LATENCY=off.

import { join } from "node:path";
import { homedir } from "node:os";
import { DumpWriter } from "./dump.js";

export function resolveLatencyConfig(env = process.env) {
  const home = env.HOME || homedir();
  const mode = String(env.MNEMO_LATENCY || "on").toLowerCase();
  return {
    enabled: mode !== "off",
    dir: env.MNEMO_LATENCY_DIR || join(home, ".mnemo-cortex/latency"),
    retentionDays: 0,
  };
}

export class LatencyWriter {
  constructor(agentId, config = resolveLatencyConfig()) {
    this.writer = new DumpWriter(agentId, config);
    this.enabled = config.enabled;
    this.dir = config.dir;
  }

  // Wrap an MCP tool handler so every invocation appends one timing line.
  // A handler that returns {isError:true} or throws is recorded ok:false.
  // A hang that never returns writes nothing — the target class is the
  // hang that clears on its own and would otherwise vanish.
  wrap(toolName, handler) {
    if (!this.enabled) return handler;
    const writer = this.writer;
    return async function timedHandler(...args) {
      const start = Date.now();
      let ok = true;
      try {
        const response = await handler.apply(this, args);
        if (response && response.isError) ok = false;
        return response;
      } catch (err) {
        ok = false;
        throw err;
      } finally {
        writer.write({
          ts: new Date().toISOString(),
          kind: "latency",
          tool: toolName,
          ms: Date.now() - start,
          ok,
        });
      }
    };
  }
}
