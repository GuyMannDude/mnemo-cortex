#!/usr/bin/env bash
# Mnemo Cortex — Hermes Agent Integration Installer
# Wires Mnemo Cortex into your Hermes Agent's MCP config so Hermes gets
# persistent semantic memory across sessions, plus optional brain-lane,
# wiki, and Passport tools.
# https://github.com/GuyMannDude/mnemo-cortex
set -euo pipefail

# ─────────────────────────────────────────────
# Colors
# ─────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}▸${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; exit 1; }

# ─────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}Mnemo Cortex — Hermes Agent Integration${NC}"
echo "Persistent memory for Hermes. Tools auto-discovered at startup."
echo ""

# ─────────────────────────────────────────────
# Prerequisites
# ─────────────────────────────────────────────
command -v hermes >/dev/null 2>&1 || fail "hermes CLI not found. Install Hermes Agent first: https://hermes-agent.nousresearch.com/docs/getting-started/quickstart"
command -v node >/dev/null 2>&1 || fail "node is required but not installed (need Node.js 18+)"
command -v npm >/dev/null 2>&1 || fail "npm is required but not installed"

NODE_MAJOR=$(node -v | sed 's/v\([0-9]*\).*/\1/')
[[ "$NODE_MAJOR" -ge 18 ]] || fail "Node.js 18+ required (have $(node -v))"

# ─────────────────────────────────────────────
# Locate the mnemo-cortex repo (the bridge lives inside it)
# ─────────────────────────────────────────────
# When running from inside the repo, the bridge is at ../openclaw-mcp/server.js
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "${SCRIPT_DIR}/../openclaw-mcp" 2>/dev/null && pwd || echo "")"

if [[ -z "$BRIDGE_DIR" || ! -f "$BRIDGE_DIR/server.js" ]]; then
    warn "Bridge not found relative to this script."
    echo ""
    echo -e "${BOLD}Where is your mnemo-cortex checkout?${NC}"
    echo "  If you don't have one yet: git clone https://github.com/GuyMannDude/mnemo-cortex"
    echo ""
    read -rp "Path to mnemo-cortex repo: " REPO_PATH
    REPO_PATH="${REPO_PATH%/}"
    BRIDGE_DIR="${REPO_PATH}/integrations/openclaw-mcp"
    [[ -f "$BRIDGE_DIR/server.js" ]] || fail "No server.js at $BRIDGE_DIR"
fi

ok "Bridge found: $BRIDGE_DIR/server.js"

# ─────────────────────────────────────────────
# Bridge npm deps
# ─────────────────────────────────────────────
if [[ ! -d "$BRIDGE_DIR/node_modules" ]]; then
    info "Installing bridge dependencies (npm install in $BRIDGE_DIR)..."
    (cd "$BRIDGE_DIR" && npm install --no-audit --no-fund)
    ok "Bridge dependencies installed"
else
    ok "Bridge dependencies already installed"
fi

# ─────────────────────────────────────────────
# Step 1: Mnemo Cortex URL
# ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}Step 1: Where is Mnemo Cortex running?${NC}"
echo ""
echo "  Same machine as Hermes:  http://localhost:50001  (default)"
echo "  Different machine:        http://hostname:50001"
echo ""
read -rp "Mnemo Cortex URL [http://localhost:50001]: " MNEMO_URL
MNEMO_URL="${MNEMO_URL:-http://localhost:50001}"
MNEMO_URL="${MNEMO_URL%/}"

info "Testing connection to ${MNEMO_URL}..."
if health=$(curl -sf --max-time 5 "${MNEMO_URL}/health" 2>/dev/null); then
    status=$(echo "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")
    ok "Connected — status: ${status}"
else
    warn "Could not reach ${MNEMO_URL}/health"
    echo "  Make sure mnemo-cortex is running:  mnemo-cortex start"
    read -rp "Continue anyway? (y/N): " cont
    [[ "$cont" =~ ^[Yy] ]] || exit 1
fi

# ─────────────────────────────────────────────
# Step 2: Agent ID
# ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}Step 2: Pick a Hermes agent ID${NC}"
echo ""
echo "  Memories are scoped per agent. If multiple Hermes instances share"
echo "  this Mnemo, give each one a distinct ID (e.g., 'hermes-laptop',"
echo "  'hermes-server', or your handle)."
echo ""
read -rp "Agent ID [hermes]: " AGENT_ID
AGENT_ID="${AGENT_ID:-hermes}"

# ─────────────────────────────────────────────
# Step 3: Cross-agent share mode
# ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}Step 3: Cross-agent memory sharing${NC}"
echo ""
echo "  separate  — only see your own agent's memories (default, safest)"
echo "  always    — see all agents' memories every search"
echo "  never     — even mnemo_share toggle is blocked"
echo ""
read -rp "Share mode [separate]: " SHARE_MODE
SHARE_MODE="${SHARE_MODE:-separate}"
case "$SHARE_MODE" in
    separate|always|never) ;;
    *) warn "Unknown share mode '$SHARE_MODE' — falling back to 'separate'"; SHARE_MODE="separate" ;;
esac

# ─────────────────────────────────────────────
# Step 4: Register with Hermes
# ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}Step 4: Registering 'mnemo' MCP server with Hermes${NC}"

# If a mnemo entry already exists, offer to replace it
if hermes mcp list 2>/dev/null | grep -qE '^\s*mnemo\b'; then
    warn "Hermes already has an 'mnemo' MCP server configured."
    read -rp "Replace it? (y/N): " replace
    [[ "$replace" =~ ^[Yy] ]] || fail "Aborted by user. Existing entry left in place."
    hermes mcp remove mnemo
    ok "Removed existing 'mnemo' entry"
fi

hermes mcp add mnemo \
    --command node \
    --args "${BRIDGE_DIR}/server.js" \
    --env "MNEMO_URL=${MNEMO_URL}" "MNEMO_AGENT_ID=${AGENT_ID}" "MNEMO_SHARE=${SHARE_MODE}"
ok "Registered 'mnemo' in ~/.hermes/config.yaml"

# ─────────────────────────────────────────────
# Step 5: Verify the wire
# ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}Step 5: Verifying the wire${NC}"
info "Running: hermes mcp test mnemo"
echo ""
if hermes mcp test mnemo; then
    echo ""
    ok "Mnemo Cortex is wired to Hermes. Memory tools will be available in every conversation."
else
    echo ""
    warn "hermes mcp test reported issues. Check the output above."
    echo "  Common fixes:"
    echo "    • Make sure mnemo-cortex is running:  mnemo-cortex start"
    echo "    • Check ~/.hermes/config.yaml — the 'mnemo' entry should reference $BRIDGE_DIR/server.js"
fi

# ─────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}Next steps${NC}"
echo "  • Start a Hermes session:    hermes"
echo "  • Save your first memory:    ask Hermes to use mnemo_save"
echo "  • Recall later:              ask Hermes to use mnemo_recall"
echo "  • Reconfigure / remove:      hermes mcp remove mnemo, then re-run this installer"
echo ""
echo "Optional advanced env vars (edit ~/.hermes/config.yaml directly):"
echo "  • BRAIN_DIR — point at a mnemo-plan brain checkout to enable read_brain_file etc."
echo "  • WIKI_DIR  — point at a WikAI dir to enable wiki_search etc."
echo ""
echo "Full guide: https://github.com/GuyMannDude/mnemo-cortex/blob/master/integrations/hermes/README.md"
echo ""
