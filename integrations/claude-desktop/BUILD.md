# Building `mnemo-cortex.mcpb`

The bundle is the canonical Claude Desktop install path. Rebuild it at every release, and whenever the bridge or its dependencies change.

## Build

```bash
bash scripts/build-mcpb.sh
```

Needs `node`, `npm`, `zip` and `unzip`. No `mcpb` CLI: a `.mcpb` is a ZIP holding `manifest.json`, `icon.png` and `server/`.

The script:

1. Refuses to build unless `manifest.json`'s `version` equals the bridge's `package.json` version, and every tool the manifest lists exists in the bridge.
2. Stages every runtime module of `integrations/mcp-bridge/` by glob (tests left out), plus `package.json` and the lockfile, then installs production dependencies with `npm ci --omit=dev`.
3. Zips the stage, leaving out type declarations and source maps.
4. Unpacks the finished bundle somewhere else and starts it over MCP stdio (`scripts/mcp-handshake.mjs`: `initialize` → `tools/list`). The output file is replaced only if that passes.

CI runs the same script on every push (`desktop-bundle` job), so the recipe cannot fall behind the bridge.

To write the bundle somewhere else: `bash scripts/build-mcpb.sh /tmp/test.mcpb`.

## Versioning

`manifest.json`'s `version` must match the bridge's `package.json` version; the build fails if it does not. When the bridge gains or loses a tool, update the `tools` list and the counts in `long_description`.

## Testing against a real server

The handshake proves the bundle loads. It does not prove auth: `/health` is unauthenticated, so the bridge prints `Connected to Mnemo Cortex` even with a broken or missing key. Before a release, unzip the bundle and make a real tool call over MCP stdio (`initialize` → `tools/call` `mnemo_recall`) against an auth-enabled server, twice:

- with `MNEMO_AUTH_TOKEN` set: expect recalled memories;
- without it (and no `~/.mnemo-auth-token`): expect a loud `401: Unauthorized` error.

Then install the bundle in Claude Desktop and run one recall from a chat. Every extension tool starts as "ask"; a dismissed permission prompt looks exactly like broken memory, so check the extension's permissions panel before debugging the bridge.

## Notes

- `icon.png` (512×512) lives beside the manifest and is copied in as is.
- `manifest_version` follows the [MCPB spec](https://github.com/modelcontextprotocol/mcpb). 0.3 covers Node servers; bump it only when a newer schema is needed.
