// Quick verification that the MCP server starts and Mnemo is reachable.
// Run: node test.js
// Requires MNEMO_URL to be set (or defaults to localhost:50001).

const MNEMO_URL = process.env.MNEMO_URL || "http://artforge:50001";

async function test(name, fn) {
  try {
    const result = await fn();
    console.log(`  PASS  ${name}`);
    return result;
  } catch (err) {
    console.log(`  FAIL  ${name}: ${err.message}`);
    process.exitCode = 1;
  }
}

console.log(`\nTesting against Mnemo Cortex at ${MNEMO_URL}\n`);

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

console.log("\nAll tests complete.\n");
