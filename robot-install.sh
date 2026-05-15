#!/usr/bin/env bash
# robot-install.sh — non-interactive Mnemo Cortex installer driven by a JSON manifest.
#
# Usage:
#   ./robot-install.sh [path/to/manifest.json]
#   default manifest: ./robot.install
#
# Designed for LLM agents and CI: zero prompts, all human-readable progress on
# stderr, a single JSON object on stdout for the caller to parse.
#
# Stdout shape (always valid JSON):
#   {
#     "ok": true|false,
#     "steps": {
#       "deps":       {"ok": true, "python": "3.12"},
#       "venv":       {"ok": true, "path": "..."},
#       "pip":        {"ok": true},
#       "config":     {"ok": true, "config_path": "...", "data_dir": "..."},
#       "systemd":    {"ok": true, "service": "mnemo-cortex", "port": 50001},
#       "smoke_test": {"ok": true, "health": "ok", "memory_id": "...", "recall_hits": 1}
#     },
#     "error": "<reason>"        // only present when ok=false
#   }
#
# Exit codes:
#   0 — success (ok:true)
#   1 — failure (ok:false; error field describes which step blew up)
#
# Env overrides (for testing / sandboxed installs):
#   MNEMO_INSTALL_VENV_DIR     default <repo>/.venv
#   MNEMO_INSTALL_CONFIG_DIR   default ~/.config/mnemo-cortex
#   MNEMO_INSTALL_SYSTEMD_DIR  default ~/.config/systemd/user
#   MNEMO_INSTALL_DRY_RUN      "1" to skip pip + systemd write/enable

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${1:-${REPO_DIR}/robot.install}"
VENV_DIR="${MNEMO_INSTALL_VENV_DIR:-${REPO_DIR}/.venv}"
CONFIG_DIR="${MNEMO_INSTALL_CONFIG_DIR:-${HOME}/.config/mnemo-cortex}"
SYSTEMD_DIR="${MNEMO_INSTALL_SYSTEMD_DIR:-${HOME}/.config/systemd/user}"
DRY_RUN="${MNEMO_INSTALL_DRY_RUN:-0}"

log() { printf '[mnemo-cortex] %s\n' "$*" >&2; }

STEPS='{}'

set_step() {
  local key="$1" value="$2"
  STEPS=$(python3 - "$STEPS" "$key" "$value" <<'PY'
import json, sys
steps = json.loads(sys.argv[1])
steps[sys.argv[2]] = json.loads(sys.argv[3])
print(json.dumps(steps))
PY
)
}

emit() {
  local ok="$1" error="${2:-}"
  python3 - "$ok" "$error" "$STEPS" <<'PY'
import json, sys
ok = sys.argv[1] == "true"
err = sys.argv[2]
steps = json.loads(sys.argv[3])
out = {"ok": ok, "steps": steps}
if not ok and err:
    out["error"] = err
print(json.dumps(out, indent=2))
PY
  [ "$ok" = "true" ] && exit 0 || exit 1
}

if ! command -v python3 >/dev/null 2>&1; then
  printf '{"ok": false, "error": "python3 not found", "steps": {}}\n'
  exit 1
fi

# ─── parse manifest ───────────────────────────────────────────────────

PARSED_ENV=$(python3 - "$MANIFEST" <<'PY'
import json, sys, re, os
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print(f"__ERROR__=manifest not found: {path}")
    sys.exit(0)

try:
    raw = path.read_text()
    cleaned = "\n".join(
        re.sub(r"^\s*//.*$", "", line) for line in raw.splitlines()
    )
    data = json.loads(cleaned)
except json.JSONDecodeError as e:
    print(f"__ERROR__=invalid JSON in {path}: {e}")
    sys.exit(0)

def shquote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"

def expand(p):
    return os.path.expanduser(str(p))

svc = data.get("service") or {}
print(f"SVC_HOST={shquote(svc.get('host', '127.0.0.1'))}")
print(f"SVC_PORT={int(svc.get('port', 50001))}")
print(f"DATA_DIR={shquote(expand(svc.get('data_dir', '~/.mnemo-cortex')))}")

rs = data.get("reasoning") or {}
print(f"REASON_PROVIDER={shquote(rs.get('provider', 'openrouter'))}")
print(f"REASON_MODEL={shquote(rs.get('model', 'google/gemini-2.5-flash'))}")
print(f"REASON_API_BASE={shquote(rs.get('api_base', 'https://openrouter.ai/api/v1'))}")
print(f"REASON_API_KEY_ENV={shquote(rs.get('api_key_env', 'OPENROUTER_API_KEY'))}")

emb = data.get("embedding") or {}
print(f"EMB_PROVIDER={shquote(emb.get('provider', 'ollama'))}")
print(f"EMB_MODEL={shquote(emb.get('model', 'nomic-embed-text'))}")
print(f"EMB_API_BASE={shquote(emb.get('api_base', 'http://localhost:11434'))}")

decay = data.get("decay") or {}
print(f"DECAY_TOPO_WARN={int(decay.get('topology_warn_days', 30))}")
print(f"DECAY_TOPO_STALE={int(decay.get('topology_stale_days', 90))}")
print(f"DECAY_CURRENT_WARN={int(decay.get('current_state_warn_days', 90))}")
print(f"DECAY_REL_WARN={int(decay.get('relationship_warn_days', 180))}")
print(f"DECAY_SLOG_WARN={int(decay.get('session_log_warn_days', 90))}")

m0 = data.get("mem0") or {}
print(f"MEM0_ENABLED={'1' if m0.get('enabled') else '0'}")
print(f"MEM0_API_KEY_ENV={shquote(m0.get('api_key_env', 'MEM0_API_KEY'))}")
print(f"MEM0_USER_ID={shquote(m0.get('user_id', 'default-agent'))}")
print(f"MEM0_FALLBACK={'1' if m0.get('fallback_only', True) else '0'}")
print(f"MEM0_SYNC={'1' if m0.get('sync_writes') else '0'}")

sd = data.get("systemd") or {}
print(f"SYSTEMD_ENABLED={'1' if sd.get('enabled', True) else '0'}")
svc_name = sd.get("service_name", "mnemo-cortex")
if not re.match(r"^[a-z][a-z0-9_-]{0,40}$", svc_name):
    print(f"__ERROR__=invalid systemd service_name {svc_name!r} (lowercase letters/digits/_/- only)")
    sys.exit(0)
print(f"SYSTEMD_SVC={shquote(svc_name)}")

st = data.get("smoke_test") or {}
print(f"SMOKE_ENABLED={'1' if st.get('enabled', True) else '0'}")
print(f"SMOKE_AGENT={shquote(st.get('agent_id', 'robot-install-smoke'))}")
PY
)

if echo "$PARSED_ENV" | grep -q '^__ERROR__='; then
  err=$(echo "$PARSED_ENV" | sed -n 's/^__ERROR__=//p')
  set_step manifest '{"ok": false}'
  emit false "$err"
fi

eval "$PARSED_ENV"
log "manifest parsed: $MANIFEST"

# ─── step 1/5: dependency check ───────────────────────────────────────

log "step 1/5 — dependencies"

missing=""
for c in python3 curl; do
  command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
done

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')
[ "$PY_OK" = "1" ] || missing="$missing python>=3.11(have_$PY_VER)"

if [ "$SYSTEMD_ENABLED" = "1" ] && [ "$DRY_RUN" != "1" ]; then
  command -v systemctl >/dev/null 2>&1 || missing="$missing systemctl"
fi

if [ -n "${missing// }" ]; then
  set_step deps "{\"ok\": false, \"missing\":\"${missing# }\"}"
  emit false "missing dependencies:${missing}"
fi

# Verify the API key for the reasoning provider is actually set in the
# install-time environment. Without it the bridge will fall through to
# the local Ollama provider, which the operator may not have configured.
KEY_PRESENT=1
REASON_API_KEY=""
if [ -n "$REASON_API_KEY_ENV" ]; then
  REASON_API_KEY="${!REASON_API_KEY_ENV:-}"
  if [ -z "$REASON_API_KEY" ]; then
    KEY_PRESENT=0
  fi
fi

set_step deps "{\"ok\": true, \"python\": \"$PY_VER\", \"reasoning_key_present\": $([ $KEY_PRESENT = 1 ] && echo true || echo false)}"

# ─── step 2/5: venv + pip install ─────────────────────────────────────

log "step 2/5 — venv at $VENV_DIR"

if [ "$DRY_RUN" = "1" ]; then
  set_step venv "{\"ok\": true, \"path\": \"$VENV_DIR\", \"dry_run\": true}"
  set_step pip '{"ok": true, "dry_run": true}'
else
  if [ ! -d "$VENV_DIR" ]; then
    if ! python3 -m venv "$VENV_DIR" >&2; then
      set_step venv "{\"ok\": false, \"path\": \"$VENV_DIR\"}"
      emit false "failed to create venv at $VENV_DIR"
    fi
  fi
  set_step venv "{\"ok\": true, \"path\": \"$VENV_DIR\"}"

  PIP_BIN="$VENV_DIR/bin/pip"
  if ! "$PIP_BIN" install --quiet --upgrade pip >&2; then
    set_step pip '{"ok": false, "error": "pip upgrade failed"}'
    emit false "pip upgrade failed"
  fi
  if ! "$PIP_BIN" install --quiet -e "$REPO_DIR" >&2; then
    set_step pip '{"ok": false, "error": "pip install -e . failed"}'
    emit false "pip install failed"
  fi
  set_step pip '{"ok": true}'
fi

# ─── step 3/5: config + data dirs ─────────────────────────────────────

CONFIG_FILE="$CONFIG_DIR/mnemo-cortex.yaml"
ENV_FILE="$CONFIG_DIR/mnemo-cortex.env"

if [ "$DRY_RUN" = "1" ]; then
  log "step 3/5 — config (skipped, DRY_RUN=1)"
  set_step config "{\"ok\": true, \"dry_run\": true, \"config_path\": \"$CONFIG_FILE\", \"env_path\": \"$ENV_FILE\", \"data_dir\": \"$DATA_DIR\"}"
else
  log "step 3/5 — config at $CONFIG_DIR"

  mkdir -p "$CONFIG_DIR"
  mkdir -p "$DATA_DIR"/{memory,cache/l1,cache/l2,logs}

  python3 - "$CONFIG_FILE" <<PY
import os
from pathlib import Path
cfg = Path("$CONFIG_FILE")
if cfg.exists():
    # Preserve existing config — only ensure it's there.
    print(f"[mnemo-cortex] config already exists: {cfg}", flush=True)
else:
    cfg.write_text("""# Mnemo Cortex config — generated by robot-install.sh
data_dir: $DATA_DIR
log_level: info

reasoning:
  provider: $REASON_PROVIDER
  model: $REASON_MODEL
  api_base: $REASON_API_BASE
  timeout: 30

embedding:
  provider: $EMB_PROVIDER
  model: $EMB_MODEL
  api_base: $EMB_API_BASE

server:
  host: $SVC_HOST
  port: $SVC_PORT
""")
PY

  # Env file — sourced by the systemd unit. Holds the API keys (read
  # from the install-time environment) and v3 decay overrides.
  {
    echo "# Mnemo Cortex env — generated by robot-install.sh"
    echo "AGENTB_DATA_DIR=$DATA_DIR"
    echo "AGENTB_REASON_MODEL=$REASON_MODEL"
    echo "AGENTB_EMBED_MODEL=$EMB_MODEL"
    echo "AGENTB_REASON_URL=$REASON_API_BASE/chat/completions"
    echo "AGENTB_PORT=$SVC_PORT"
    echo "MNEMO_DECAY_TOPOLOGY_WARN_DAYS=$DECAY_TOPO_WARN"
    echo "MNEMO_DECAY_TOPOLOGY_STALE_DAYS=$DECAY_TOPO_STALE"
    echo "MNEMO_DECAY_CURRENT_STATE_WARN_DAYS=$DECAY_CURRENT_WARN"
    echo "MNEMO_DECAY_RELATIONSHIP_WARN_DAYS=$DECAY_REL_WARN"
    echo "MNEMO_DECAY_SESSION_LOG_WARN_DAYS=$DECAY_SLOG_WARN"
    if [ -n "$REASON_API_KEY" ]; then
      echo "${REASON_API_KEY_ENV}=$REASON_API_KEY"
    fi
    if [ "$MEM0_ENABLED" = "1" ]; then
      MEM0_KEY="${!MEM0_API_KEY_ENV:-}"
      echo "MEM0_ENABLED=true"
      echo "MEM0_USER_ID=$MEM0_USER_ID"
      echo "MEM0_FALLBACK_ONLY=$([ "$MEM0_FALLBACK" = "1" ] && echo true || echo false)"
      echo "MEM0_SYNC_WRITES=$([ "$MEM0_SYNC" = "1" ] && echo true || echo false)"
      if [ -n "$MEM0_KEY" ]; then
        echo "${MEM0_API_KEY_ENV}=$MEM0_KEY"
      fi
    fi
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  set_step config "{\"ok\": true, \"config_path\": \"$CONFIG_FILE\", \"env_path\": \"$ENV_FILE\", \"data_dir\": \"$DATA_DIR\"}"
fi

# ─── step 4/5: systemd unit ───────────────────────────────────────────

if [ "$SYSTEMD_ENABLED" != "1" ]; then
  log "step 4/5 — systemd (disabled by manifest)"
  set_step systemd '{"ok": true, "skipped": true, "reason": "disabled by manifest"}'
elif [ "$DRY_RUN" = "1" ]; then
  log "step 4/5 — systemd (skipped, DRY_RUN=1)"
  set_step systemd "{\"ok\": true, \"dry_run\": true, \"service\": \"$SYSTEMD_SVC\", \"port\": $SVC_PORT}"
else
  log "step 4/5 — systemd unit $SYSTEMD_SVC.service"
  mkdir -p "$SYSTEMD_DIR"
  PY_BIN="$VENV_DIR/bin/python"

  cat > "$SYSTEMD_DIR/${SYSTEMD_SVC}.service" <<EOF
[Unit]
Description=Mnemo Cortex memory service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PY_BIN -m uvicorn agentb.server:app --host $SVC_HOST --port $SVC_PORT --log-level info
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  if ! systemctl --user enable --now "${SYSTEMD_SVC}.service" >/dev/null 2>&1; then
    set_step systemd "{\"ok\": false, \"service\": \"$SYSTEMD_SVC\", \"port\": $SVC_PORT, \"error\": \"service failed to start\"}"
    emit false "systemd: ${SYSTEMD_SVC}.service failed to start. Check: journalctl --user -u ${SYSTEMD_SVC}.service"
  fi
  set_step systemd "{\"ok\": true, \"service\": \"$SYSTEMD_SVC\", \"port\": $SVC_PORT}"
fi

# ─── step 5/5: smoke test ─────────────────────────────────────────────

if [ "$SMOKE_ENABLED" != "1" ] || [ "$DRY_RUN" = "1" ]; then
  log "step 5/5 — smoke test (skipped)"
  set_step smoke_test '{"ok": true, "skipped": true}'
  emit true
fi

log "step 5/5 — smoke test"

# Wait up to 15s for /health
attempts=0
while [ $attempts -lt 15 ]; do
  health=$(curl -s -m 2 "http://$SVC_HOST:$SVC_PORT/health" 2>/dev/null || true)
  if echo "$health" | python3 -c 'import sys,json;sys.exit(0 if json.load(sys.stdin).get("status") in ("ok","degraded") else 1)' 2>/dev/null; then
    break
  fi
  sleep 1
  attempts=$((attempts + 1))
done

if [ $attempts -ge 15 ]; then
  set_step smoke_test "{\"ok\": false, \"error\": \"/health unreachable after 15s\"}"
  emit false "smoke test: /health did not come up on port $SVC_PORT"
fi

# save → recall round-trip with a unique marker
MARKER="robot-install-marker-$$-$(date +%s)"
save=$(curl -s -m 5 -X POST "http://$SVC_HOST:$SVC_PORT/writeback" \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"robot-install\",\"summary\":\"smoke test $MARKER. Mnemo Cortex robot.install verification.\",\"key_facts\":[\"marker $MARKER\"],\"projects_referenced\":[],\"decisions_made\":[],\"agent_id\":\"$SMOKE_AGENT\",\"source\":\"tool\",\"category\":\"session_log\"}")

if ! echo "$save" | python3 -c 'import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get("status")=="archived" else 1)' 2>/dev/null; then
  set_step smoke_test "{\"ok\": false, \"error\": \"save failed\", \"response\": $(echo "$save" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')}"
  emit false "smoke test: save call failed"
fi

memory_id=$(echo "$save" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("memory_id",""))')

sleep 1
recall=$(curl -s -m 10 -X POST "http://$SVC_HOST:$SVC_PORT/context" \
  -H 'Content-Type: application/json' \
  -d "{\"prompt\":\"$MARKER\",\"agent_id\":\"$SMOKE_AGENT\",\"max_results\":3,\"category\":\"session_log\"}")

hits=$(echo "$recall" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d.get("chunks",[])))' 2>/dev/null || echo 0)

if [ "${hits:-0}" -ge 1 ]; then
  set_step smoke_test "{\"ok\": true, \"health\": \"ok\", \"memory_id\": \"$memory_id\", \"recall_hits\": $hits, \"marker\": \"$MARKER\"}"
  emit true
else
  set_step smoke_test "{\"ok\": false, \"health\": \"ok\", \"memory_id\": \"$memory_id\", \"recall_hits\": $hits, \"marker\": \"$MARKER\"}"
  emit false "smoke test: marker $MARKER saved as $memory_id but did not return on recall"
fi
