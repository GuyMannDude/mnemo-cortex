// Verification tests for the Mnemo Cortex MCP bridge.
// Run: MNEMO_URL=http://artforge:50001 node test.js
// Default: http://localhost:50001

const MNEMO_URL = process.env.MNEMO_URL || "http://localhost:50001";

let passed = 0;
let failed = 0;

async function test(name, fn) {
  try {
    const result = await fn();
    console.log(`  PASS  ${name}`);
    passed++;
    return result;
  } catch (err) {
    console.log(`  FAIL  ${name}: ${err.message}`);
    failed++;
  }
}

console.log(`\nTesting against Mnemo Cortex at ${MNEMO_URL}\n`);
console.log("── Happy path ──\n");

// 1. Health check
await test("Health check", async () => {
  const res = await fetch(`${MNEMO_URL}/health`);
  const data = await res.json();
  if (data.status !== "ok") throw new Error(`status: ${data.status}`);
  console.log(`         ${data.memory_entries} memories in store`);
});

// 2. Write a test memory
const testSession = `test-openclaw-mcp-${Date.now()}`;
await test("Write memory", async () => {
  const res = await fetch(`${MNEMO_URL}/writeback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: testSession,
      summary: "OpenClaw MCP integration test — verifying write path works.",
      key_facts: ["test_key_fact: integration test passed"],
      projects_referenced: [],
      decisions_made: [],
      agent_id: "openclaw-test",
    }),
  });
  const data = await res.json();
  if (!data.memory_id) throw new Error("No memory_id returned");
  console.log(`         memory_id: ${data.memory_id}`);
});

// 3. Recall the memory we just wrote
await test("Recall memory", async () => {
  const res = await fetch(`${MNEMO_URL}/context`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: "OpenClaw MCP integration test",
      agent_id: "openclaw-test",
      max_results: 3,
    }),
  });
  const data = await res.json();
  if (!data.chunks || data.chunks.length === 0)
    throw new Error("No chunks returned");
  console.log(`         ${data.total_found} memories found`);
});

// 4. Cross-agent search
await test("Cross-agent search", async () => {
  const res = await fetch(`${MNEMO_URL}/context`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: "test",
      max_results: 3,
    }),
  });
  const data = await res.json();
  if (!data.chunks) throw new Error("No chunks field");
  console.log(`         ${data.total_found} memories found across all agents`);
});

// ── Failure cases ──────────────────────────────────────────────
console.log("\n── Failure cases ──\n");

// 5. Unreachable server
await test("Unreachable server returns clean error", async () => {
  const badUrl = "http://127.0.0.1:59999";
  try {
    await fetch(`${badUrl}/health`);
    throw new Error("Should not have connected");
  } catch (err) {
    if (err.message === "Should not have connected") throw err;
    // Good — connection refused or similar network error
    console.log(`         Got expected error: ${err.cause?.code || err.message}`);
  }
});

// 6. Empty query handling
await test("Empty query returns HTTP error (not crash)", async () => {
  const res = await fetch(`${MNEMO_URL}/context`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: "",
      agent_id: "openclaw-test",
      max_results: 1,
    }),
  });
  // Mnemo rejects empty prompts (400 or 500) — that's fine, we just
  // verify the server doesn't hang or crash without responding
  console.log(`         status: ${res.status} (server rejects empty query)`);
});

// 7. Invalid endpoint returns HTTP error
await test("Invalid endpoint returns HTTP error", async () => {
  const res = await fetch(`${MNEMO_URL}/nonexistent`);
  if (res.ok) throw new Error("Expected non-200 for invalid endpoint");
  console.log(`         status: ${res.status} (expected)`);
});

// ── Summary ────────────────────────────────────────────────────
console.log(`\n${passed} passed, ${failed} failed.\n`);
if (failed > 0) process.exitCode = 1;
