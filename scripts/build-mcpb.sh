#!/usr/bin/env bash
# Build the Claude Desktop one-click bundle from the canonical bridge, then
# prove the built bundle starts.
#
#   bash scripts/build-mcpb.sh [output.mcpb]
#   default output: integrations/claude-desktop/mnemo-cortex.mcpb
#
# A .mcpb is a ZIP: manifest.json + icon.png + server/. This script needs only
# node, npm, zip and unzip (no mcpb CLI).
#
# Why a script and not a recipe in BUILD.md: the recipe listed the bridge's
# files by name, and the list fell behind the bridge twice (a bundle that
# crashed on its first tool call in July; a bundle 23 versions stale by
# November). Here every runtime module is taken by glob, and the build fails
# unless the manifest version matches the bridge and the bundle answers an
# MCP handshake.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BRIDGE="$ROOT/integrations/mcp-bridge"
DESKTOP="$ROOT/integrations/claude-desktop"
OUT=${1:-"$DESKTOP/mnemo-cortex.mcpb"}
case "$OUT" in /*) ;; *) OUT="$PWD/$OUT" ;; esac

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

bridge_version=$(node -p "require('$BRIDGE/package.json').version")
manifest_version=$(node -p "require('$DESKTOP/manifest.json').version")
if [ "$bridge_version" != "$manifest_version" ]; then
    echo "build-mcpb: manifest.json says $manifest_version but the bridge is $bridge_version." >&2
    echo "            Bump integrations/claude-desktop/manifest.json to match." >&2
    exit 1
fi

# Every tool the manifest names must be one the bridge really has. The bridge
# may register MORE than the manifest lists (manifest sets tools_generated),
# and the brain/wiki tools only register when those dirs exist, so the check
# runs against the bridge source rather than a handshake's list.
missing=$(node -e '
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const names = JSON.parse(fs.readFileSync(process.argv[2], "utf8")).tools.map(t => t.name);
console.log(names.filter(n => !src.includes(`"${n}"`)).join(" "));
' "$BRIDGE/server.js" "$DESKTOP/manifest.json")
if [ -n "$missing" ]; then
    echo "build-mcpb: manifest.json lists tools the bridge does not define: $missing" >&2
    exit 1
fi

# Every runtime module, by glob. *.test.js and test.js are tests, not runtime.
mkdir -p "$STAGE/bundle/server"
for f in "$BRIDGE"/*.js; do
    case "$(basename "$f")" in
        *.test.js|test.js) ;;
        *) cp "$f" "$STAGE/bundle/server/" ;;
    esac
done
# server.js reads ./package.json for its version; npm ci needs the lockfile.
cp "$BRIDGE/package.json" "$BRIDGE/package-lock.json" "$STAGE/bundle/server/"
(cd "$STAGE/bundle/server" && npm ci --omit=dev --ignore-scripts --no-audit --no-fund --loglevel=error)

cp "$DESKTOP/manifest.json" "$DESKTOP/icon.png" "$STAGE/bundle/"

# Type declarations and source maps are never loaded at runtime; leaving them
# out takes the bundle from about 5.3 MB to about 4 MB.
(cd "$STAGE/bundle" && zip -q -9 -r -X "$STAGE/out.mcpb" . \
    -x '*.d.ts' '*.d.cts' '*.d.mts' '*.map')

# Smoke test the artifact itself, not the staging dir: unpack it somewhere
# else and start it the way a client would. $OUT is only replaced once that
# passes.
mkdir "$STAGE/unpacked"
unzip -q "$STAGE/out.mcpb" -d "$STAGE/unpacked"
node "$ROOT/scripts/mcp-handshake.mjs" "$STAGE/unpacked/server/server.js" "$bridge_version" > "$STAGE/tools.txt"
mv -f "$STAGE/out.mcpb" "$OUT"

echo "built $OUT"
echo "  bridge $bridge_version · $(wc -l < "$STAGE/tools.txt" | tr -d ' ') tools answered the handshake · $(du -h "$OUT" | cut -f1)"
