#!/usr/bin/env node
// Start an MCP bridge over stdio the way a client does: initialize, then
// tools/list. Proves the server LOADS and answers; `node --check` only proves
// it parses. No Mnemo server is needed (the bridge registers its tools either
// way), so this runs in CI.
//
// Usage: node scripts/mcp-handshake.mjs <path/to/server.js> [expected-version]
// Prints one tool name per line on stdout. Exit 1 on any failure.
import { spawn } from "node:child_process";

const [entry, expectVersion] = process.argv.slice(2);
if (!entry) {
  console.error("usage: mcp-handshake.mjs <server.js> [expected-version]");
  process.exit(2);
}

const child = spawn(process.execPath, [entry], {
  env: {
    ...process.env,
    // A dead address: the handshake must not depend on a live Mnemo server.
    MNEMO_URL: process.env.MNEMO_URL || "http://127.0.0.1:9",
    MNEMO_AGENT_ID: process.env.MNEMO_AGENT_ID || "handshake",
  },
  stdio: ["pipe", "pipe", "pipe"],
});

let stderr = "";
child.stderr.on("data", (d) => { stderr += d; });

function fail(msg) {
  console.error(`HANDSHAKE FAILED: ${msg}`);
  if (stderr.trim()) console.error(`--- server stderr (tail) ---\n${stderr.slice(-2000)}`);
  child.kill();
  process.exit(1);
}

const timer = setTimeout(() => fail("no answer within 20s"), 20000);
child.on("exit", (code) => fail(`server exited early (code ${code})`));

const send = (msg) => child.stdin.write(JSON.stringify(msg) + "\n");

let buf = "";
child.stdout.on("data", (d) => {
  buf += d;
  let nl;
  while ((nl = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { fail(`non-JSON on stdout: ${line.slice(0, 200)}`); }
    if (msg.error) fail(`JSON-RPC error: ${JSON.stringify(msg.error)}`);
    if (msg.id === 1) {
      const got = msg.result?.serverInfo?.version;
      if (expectVersion && got !== expectVersion) {
        fail(`server reports version ${got}, expected ${expectVersion}`);
      }
      console.error(`handshake: ${msg.result?.serverInfo?.name} ${got}`);
      send({ jsonrpc: "2.0", method: "notifications/initialized" });
      send({ jsonrpc: "2.0", id: 2, method: "tools/list" });
    } else if (msg.id === 2) {
      const tools = msg.result?.tools || [];
      if (tools.length === 0) fail("tools/list returned no tools");
      for (const t of tools) console.log(t.name);
      console.error(`handshake: ${tools.length} tools`);
      clearTimeout(timer);
      child.removeAllListeners("exit");
      child.kill();
      process.exit(0);
    }
  }
});

send({
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "mcp-handshake", version: "1" },
  },
});
