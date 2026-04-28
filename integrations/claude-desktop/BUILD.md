# Building `mnemo-cortex.mcpb`

The bundle is the canonical Claude Desktop install path. Rebuild it whenever the openclaw-mcp bridge or its deps change.

## Prerequisites

```bash
sudo npm install -g @anthropic-ai/mcpb
```

## Build steps

```bash
cd mnemo-cortex
BUILD=integrations/mcpb-build

# Fresh stage from the canonical bridge
rm -rf "$BUILD"
mkdir -p "$BUILD/server"
cp integrations/openclaw-mcp/server.js     "$BUILD/server/"
cp -r integrations/openclaw-mcp/node_modules "$BUILD/server/"

# Icon (downscaled hero card)
convert docs/mnemo-cortex-card-v1.png -resize 512x512 "$BUILD/icon.png"

# Manifest — copy from previous bundle and update version + tool list as needed
cp integrations/claude-desktop/manifest.json "$BUILD/manifest.json"
# (or hand-edit "$BUILD/manifest.json")

# Validate then pack
cd "$BUILD"
mcpb validate manifest.json
mcpb pack . ../claude-desktop/mnemo-cortex.mcpb

# Cleanup
cd -
rm -rf "$BUILD"
```

## Versioning

The `manifest.json` `version` field should match the bridge's `package.json` version. Bump both together.

## Testing the bundle

Unzip the `.mcpb` to a tmp dir and try to launch the server with the env vars Claude Desktop will inject:

```bash
mkdir -p /tmp/mcpb-test && unzip -q integrations/claude-desktop/mnemo-cortex.mcpb -d /tmp/mcpb-test
cd /tmp/mcpb-test
MNEMO_URL=http://localhost:50001 MNEMO_AGENT_ID=test \
  timeout 3 node server/server.js < /dev/null
# expect: [mnemo-mcp] Connected to Mnemo Cortex (...)
```

## Notes

- The `.mcpb` is a zip archive. Anything in the bundle dir gets included unless `.mcpbignore` excludes it.
- `mcpb pack` automatically prunes obvious dev-only files (test dirs, source maps, `.md` files inside `node_modules`, etc.) — that's why a 26 MB staging dir packs to ~3.6 MB.
- `manifest_version` is on the [MCPB spec](https://github.com/modelcontextprotocol/mcpb). Bump when Anthropic releases a new schema version.
