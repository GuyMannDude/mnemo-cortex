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
#       "migration":  {"ok": true, "action": "copied", "from": "...", "to": "...", "files": 42},
#                     // only on a re-run over an install made before the
#                     // service loaded its config (see step 3)
#       "config":     {"ok": true, "config_path": "...", "data_dir": "..."},
#       "systemd":    {"ok": true, "service": "mnemo-cortex", "port": 50001},
#       "smoke_test": {"ok": true, "health": "ok", "memory_id": "...", "recall_hits": 1},
#       "facts_seed": {"ok": true, "skipped": true}   // unless enabled in the manifest
#     },
#     "error": "<reason>"        // only present when ok=false, in plain words
#   }
#
# The service loads the config this script writes (AGENTB_CONFIG in the env
# file). The smoke test checks that: /health must report the config's
# reasoning and embedding providers, and the test memory must land in the
# config's data dir. A provider with an api_key_env whose variable is unset
# stops the install before anything is written. Safe to run again: the
# config is kept, the env file and unit are rewritten, the service restarts.
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

# Optional post-step: seed the Phase 3 Facts table from the manifest's YAML, and
# (opt-in) install the post-commit hook + nightly cron that keep it in sync.
# Best-effort — a seeding failure is reported in its own step but never fails the
# overall install, since the service itself is already up by the time this runs.
do_facts_seed() {
  if [ "$FACTS_ENABLED" != "1" ] || [ "$DRY_RUN" = "1" ]; then
    log "facts seed — skipped"
    set_step facts_seed '{"ok": true, "skipped": true}'
    return 0
  fi

  # Resolve a relative YAML path against the repo dir.
  local yaml="$FACTS_YAML"
  case "$yaml" in
    /*) : ;;
    *) yaml="$REPO_DIR/$yaml" ;;
  esac

  if [ ! -f "$yaml" ]; then
    log "facts seed — yaml not found: $yaml"
    set_step facts_seed "$(facts_step_json false "$yaml" false false "yaml not found: $yaml")"
    return 1
  fi

  log "facts seed — $yaml"
  local py="$VENV_DIR/bin/python"
  [ -x "$py" ] || py="python3"

  local out
  if ! out=$(MNEMO_URL="$(svc_url)" "$py" "$REPO_DIR/tools/seed-facts.py" --yaml "$yaml" 2>&1); then
    echo "$out" >&2
    set_step facts_seed "$(facts_step_json false "$yaml" false false "seeder failed (see stderr)")"
    return 1
  fi
  echo "$out" >&2

  # Opt-in watchers — best-effort, reported but never fatal. Both operate on the
  # repo that holds the YAML (FACTS_HOOK_REPO), so both require it to be set.
  local hook_done=false cron_done=false
  if [ "$FACTS_HOOK" = "1" ] && [ -n "$FACTS_HOOK_REPO" ] && [ -d "$FACTS_HOOK_REPO/.git" ]; then
    if ln -sf "$REPO_DIR/tools/seed-facts-post-commit.sh" "$FACTS_HOOK_REPO/.git/hooks/post-commit" 2>/dev/null; then
      hook_done=true
      log "facts seed — post-commit hook installed in $FACTS_HOOK_REPO"
    fi
  fi
  if [ "$FACTS_CRON" = "1" ] && command -v crontab >/dev/null 2>&1; then
    if [ -z "$FACTS_HOOK_REPO" ]; then
      log "facts seed — nightly_cron requested but watcher.hook_repo is empty; skipping cron"
    elif crontab -l 2>/dev/null | grep -qF "seed-facts-nightly.sh"; then
      cron_done=true
    else
      local line="$FACTS_CRON_MIN $FACTS_CRON_HOUR * * * SEED_FACTS_REPO=$FACTS_HOOK_REPO MNEMO_URL=$(svc_url) $REPO_DIR/tools/seed-facts-nightly.sh"
      if { crontab -l 2>/dev/null; echo "$line"; } | crontab - 2>/dev/null; then
        cron_done=true
        log "facts seed — nightly cron installed ($FACTS_CRON_MIN $FACTS_CRON_HOUR * * *)"
      fi
    fi
  fi

  set_step facts_seed "$(facts_step_json true "$yaml" "$hook_done" "$cron_done" "")"
  return 0
}

# Build the facts_seed step object as valid JSON regardless of what characters
# the YAML path contains (it flows in from the manifest / repo dir). Hand-built
# f-string JSON would break json.loads in set_step on a path with a quote.
facts_step_json() {
  python3 - "$1" "$2" "$3" "$4" "$5" <<'PY'
import json, sys
ok, yaml_path, hook, cron, err = sys.argv[1:6]
out = {"ok": ok == "true"}
if out["ok"]:
    out.update({"yaml": yaml_path,
                "post_commit_hook": hook == "true",
                "nightly_cron": cron == "true"})
else:
    out["error"] = err
print(json.dumps(out))
PY
}

# Run the optional facts-seed step, then emit the final success object.
finish() {
  do_facts_seed || true
  emit true
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

def key_env(block, default):
    # The name is used as ${NAME} in the config and in bash ${!NAME}; anything
    # but a plain env-var name would break both.
    name = str(block.get('api_key_env', default))
    if name and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        print(f"__ERROR__=invalid api_key_env {name!r} (letters, digits and _ only)")
        sys.exit(0)
    return name

svc = data.get("service") or {}
print(f"SVC_HOST={shquote(svc.get('host', '127.0.0.1'))}")
print(f"SVC_PORT={int(svc.get('port', 50001))}")
print(f"DATA_DIR={shquote(expand(svc.get('data_dir', '~/.mnemo-cortex')))}")

rs = data.get("reasoning") or {}
print(f"REASON_PROVIDER={shquote(rs.get('provider', 'openrouter'))}")
print(f"REASON_MODEL={shquote(rs.get('model', 'google/gemini-2.5-flash'))}")
print(f"REASON_API_BASE={shquote(rs.get('api_base', 'https://openrouter.ai/api/v1'))}")
print(f"REASON_API_KEY_ENV={shquote(key_env(rs, 'OPENROUTER_API_KEY'))}")

emb = data.get("embedding") or {}
print(f"EMB_PROVIDER={shquote(emb.get('provider', 'ollama'))}")
print(f"EMB_MODEL={shquote(emb.get('model', 'nomic-embed-text'))}")
print(f"EMB_API_BASE={shquote(emb.get('api_base', 'http://localhost:11434'))}")
print(f"EMB_API_KEY_ENV={shquote(key_env(emb, ''))}")

decay = data.get("decay") or {}
print(f"DECAY_TOPO_WARN={int(decay.get('topology_warn_days', 30))}")
print(f"DECAY_TOPO_STALE={int(decay.get('topology_stale_days', 90))}")
print(f"DECAY_CURRENT_WARN={int(decay.get('current_state_warn_days', 90))}")
print(f"DECAY_REL_WARN={int(decay.get('relationship_warn_days', 180))}")
print(f"DECAY_SLOG_WARN={int(decay.get('session_log_warn_days', 90))}")

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

fs = data.get("facts_seed") or {}
print(f"FACTS_ENABLED={'1' if fs.get('enabled', False) else '0'}")
print(f"FACTS_YAML={shquote(fs.get('yaml', './tools/seed-facts.example.yaml'))}")
fw = fs.get("watcher") or {}
print(f"FACTS_HOOK={'1' if fw.get('post_commit_hook', False) else '0'}")
print(f"FACTS_HOOK_REPO={shquote(expand(fw.get('hook_repo', '')))}")
print(f"FACTS_CRON={'1' if fw.get('nightly_cron', False) else '0'}")
print(f"FACTS_CRON_HOUR={int(fw.get('cron_hour', 3))}")
print(f"FACTS_CRON_MIN={int(fw.get('cron_minute', 10))}")
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

# A provider that names a key env var gets that key from the install-time
# environment. If it isn't set, stop here: the server would install fine and
# then fail every call to that provider. Nothing has been written yet.
# A key's value: the install-time environment first, else the env file an
# earlier run wrote, so a re-run doesn't need the key exported again.
key_value() {
  # printenv, not ${!1}: only the environment counts, never this script's
  # own variables (a config naming ${DATA_DIR} must not pick up ours).
  local v
  v=$(printenv "$1" || true)
  if [ -z "$v" ] && [ -f "$CONFIG_DIR/mnemo-cortex.env" ]; then
    v=$(grep -m1 "^$1=" "$CONFIG_DIR/mnemo-cortex.env" | cut -d= -f2-)
  fi
  printf '%s' "$v"
}
REASON_API_KEY=""
EMB_API_KEY=""
[ -n "$REASON_API_KEY_ENV" ] && REASON_API_KEY=$(key_value "$REASON_API_KEY_ENV")
[ -n "$EMB_API_KEY_ENV" ] && EMB_API_KEY=$(key_value "$EMB_API_KEY_ENV")

# Once a config exists it decides which keys the server needs (checked in
# step 3); the manifest's api_key_env only matters for a new config.
[ -f "$CONFIG_DIR/mnemo-cortex.yaml" ] && KEY_PAIRS="" || KEY_PAIRS="reasoning:$REASON_API_KEY_ENV:$REASON_API_KEY embedding:$EMB_API_KEY_ENV:$EMB_API_KEY"
for pair in $KEY_PAIRS; do
  IFS=: read -r part name value <<<"$pair"
  if [ -n "$name" ] && [ -z "$value" ]; then
    set_step deps "{\"ok\": false, \"python\": \"$PY_VER\", \"missing_key\": \"$name\"}"
    if [ "$part" = reasoning ]; then
      alt="paste the all-local reasoning block shown in robot.install (no key needed)"
    else
      alt="set embedding back to local Ollama nomic-embed-text (no key needed)"
    fi
    emit false "The $part provider needs an API key in the environment variable $name, and it is not set. Nothing was installed. Set $name and run again, or $alt."
  fi
done

set_step deps "{\"ok\": true, \"python\": \"$PY_VER\", \"reasoning_key_present\": $([ -n "$REASON_API_KEY" ] && echo true || echo false)}"

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

# Base URL of the service; an IPv6 host goes in brackets.
svc_url() {
  case "$SVC_HOST" in
    *:*) printf 'http://[%s]:%s' "$SVC_HOST" "$SVC_PORT" ;;
    *) printf 'http://%s:%s' "$SVC_HOST" "$SVC_PORT" ;;
  esac
}

# A config an older installer generated has no api_key under reasoning.
# "check" prints "needed" when the line would be added; "apply" adds
# api_key: "${NAME}" (never the key), keeping a .bak copy, and prints the
# backup's path. A hand-written config is never touched.
patch_old_config() {
  "$VENV_PY" - "$CONFIG_FILE" "$REASON_API_KEY_ENV" "$REASON_PROVIDER" "$1" <<'PY'
import shutil, sys, time
from pathlib import Path
import yaml

cfg, name, provider, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
text = cfg.read_text()
if not text.startswith("# Mnemo Cortex config — generated by robot-install.sh"):
    sys.exit(0)  # a hand-written config is the user's to edit
rs = (yaml.safe_load(text) or {}).get("reasoning") or {}
if "api_key" in rs or "primary" in rs or rs.get("provider") != provider:
    sys.exit(0)
lines = text.splitlines(keepends=True)
at = [i for i, line in enumerate(lines) if line.rstrip() == "reasoning:"]
if len(at) != 1:
    sys.exit(0)
lines.insert(at[0] + 1, f'  api_key: "${{{name}}}"\n')
new = "".join(lines)
after = (yaml.safe_load(new) or {}).get("reasoning") or {}
if after.pop("api_key", None) != "${%s}" % name or after != rs:
    sys.exit(0)  # the insert didn't land cleanly: leave the file alone
if mode == "check":
    print("needed"); sys.exit(0)
backup = cfg.with_name(f"{cfg.name}.bak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
shutil.copy2(cfg, backup)
cfg.write_text(new)
print(backup)
PY
}

# ─── step 3/5: config + data dirs ─────────────────────────────────────

CONFIG_FILE="$CONFIG_DIR/mnemo-cortex.yaml"
ENV_FILE="$CONFIG_DIR/mnemo-cortex.env"
VENV_PY="$VENV_DIR/bin/python"

# Run Python against the server's own config loader, from the repo dir the
# service runs in, so the installer and the server always agree on what a
# config means (data dir, providers).
agentb_py() {
  (cd "$REPO_DIR" && env -u AGENTB_CONFIG "$VENV_PY" - "$@")
}

# Every ${NAME} the config refers to: the server resolves these from the env
# file, so each must have a value. On a re-run the kept config (not the
# manifest) decides which keys the server needs.
config_key_names() {
  "$VENV_PY" - "$1" <<'PY'
import re, sys, yaml
refs = set()
def walk(x):
    if isinstance(x, dict):
        for v in x.values(): walk(v)
    elif isinstance(x, list):
        for v in x: walk(v)
    elif isinstance(x, str):
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", x)
        if m: refs.add(m.group(1))
with open(sys.argv[1]) as f:
    walk(yaml.safe_load(f) or {})
print("\n".join(sorted(refs)))
PY
}

# The server's fail-closed auth check reads the host from the config, so the
# unit must bind the same host and port the config names. Once a config
# exists it decides both; the manifest's service.host/port only seed it.
use_config_server_address() {
  local addr host port
  if ! addr=$(agentb_py "$CONFIG_FILE" <<'PY'
import re, sys
from agentb.config import load_config
s = load_config(sys.argv[1]).server
if not re.fullmatch(r"[A-Za-z0-9.:_-]+", str(s.host)):
    sys.exit(f"server.host {s.host!r} is not a plain host name or address")
print(s.host, int(s.port))
PY
  ); then
    set_step config "$(python3 -c 'import json,sys;print(json.dumps({"ok": False, "config_path": sys.argv[1]}))' "$CONFIG_FILE")"
    emit false "Could not read the server address from $CONFIG_FILE (see stderr). Your settings and memories were not changed."
  fi
  read -r host port <<<"$addr"
  if [ "$host" != "$SVC_HOST" ] || [ "$port" != "$SVC_PORT" ]; then
    log "using host $host and port $port from $CONFIG_FILE (robot.install says $SVC_HOST:$SVC_PORT; the config wins once it exists)"
  fi
  SVC_HOST=$host
  SVC_PORT=$port
}

if [ "$DRY_RUN" = "1" ]; then
  log "step 3/5 — config (skipped, DRY_RUN=1)"
  set_step config "$(python3 -c 'import json,sys;print(json.dumps({"ok": True, "dry_run": True, "config_path": sys.argv[1], "env_path": sys.argv[2], "data_dir": sys.argv[3]}))' "$CONFIG_FILE" "$ENV_FILE" "$DATA_DIR")"
else
  log "step 3/5 — config at $CONFIG_DIR"

  mkdir -p "$CONFIG_DIR"

  KEPT_CONFIG=false
  if [ -f "$CONFIG_FILE" ]; then
    KEPT_CONFIG=true
    use_config_server_address
    if ! names=$(config_key_names "$CONFIG_FILE"); then
      set_step config "$(python3 -c 'import json,sys;print(json.dumps({"ok": False, "config_path": sys.argv[1]}))' "$CONFIG_FILE")"
      emit false "Could not read $CONFIG_FILE (see stderr). Your settings and memories were not changed."
    fi
    if [ -n "$REASON_API_KEY_ENV" ] && [ "$(patch_old_config check)" = needed ]; then
      names="$names $REASON_API_KEY_ENV"  # 3b will add it: it needs a value too
    fi
    for name in $names; do
      if [ -z "$(key_value "$name")" ]; then
        set_step config "$(python3 -c 'import json,sys;print(json.dumps({"ok": False, "config_path": sys.argv[1], "missing_key": sys.argv[2]}))' "$CONFIG_FILE" "$name")"
        emit false "Your existing config $CONFIG_FILE needs \${$name}, and $name is not set (not in the environment, not in the env file from the last run). Your settings and memories were not changed. Set $name and run again, or edit that config."
      fi
    done
  fi

  # 3a. Installs made before this installer wrote AGENTB_CONFIG ran their
  # service on the loader's defaults (data in ~/.agentb, local Ollama), not
  # on the config written here. When this run switches the service to the
  # config, bring the memories along: copy, verify, keep the original.
  if [ -f "$ENV_FILE" ] && ! grep -q '^AGENTB_CONFIG=' "$ENV_FILE"; then
    plan=$(agentb_py "$CONFIG_FILE" "$DATA_DIR" "$EMB_PROVIDER" "$EMB_MODEL" "$EMB_API_BASE" <<'PY'
import json, os, sys
from pathlib import Path
from agentb.config import load_config, _parse_config

cfg_file, data_dir, emb_provider, emb_model, emb_base = sys.argv[1:6]
old = load_config()  # what the old service loaded: no AGENTB_CONFIG
if Path(cfg_file).exists():
    new, where = load_config(cfg_file), cfg_file
else:
    new = _parse_config({"data_dir": data_dir, "embedding": {
        "provider": emb_provider, "model": emb_model, "api_base": emb_base}})
    where = "robot.install"

def has_files(d):
    # A link counts as content: copying through a linked folder could write
    # outside data_dir.
    for _r, dirs, files in os.walk(d):
        if files or any(os.path.islink(os.path.join(_r, x)) for x in dirs):
            return True
    return False

def emb(c):
    return f"{c.embedding.primary.provider}/{c.embedding.primary.model}"

# Relative paths mean what they mean to the server, which runs in the repo dir.
old.data_dir, new.data_dir = os.path.abspath(old.data_dir), os.path.abspath(new.data_dir)
src, dst = os.path.realpath(old.data_dir), os.path.realpath(new.data_dir)
out = {"from": old.data_dir, "to": new.data_dir}
if src == dst or not has_files(src):
    out["action"] = "none"
elif os.path.commonpath([src, dst]) in (src, dst):
    out["action"] = "stop"
    out["error"] = (f"Your memories are in {old.data_dir}, and data_dir in {where} points "
                    f"at {new.data_dir}, one folder inside the other. Your settings and memories were not changed. "
                    f"Point data_dir at a separate folder (or at {old.data_dir}) and run again.")
elif has_files(dst):
    out["action"] = "stop"
    out["error"] = (f"Your memories are in {old.data_dir} (where the server has kept them "
                    f"until now), and {new.data_dir} (data_dir in {where}) also holds files. "
                    f"Your settings and memories were not changed. Keep one: set data_dir to {old.data_dir} to keep "
                    f"using it, or move {new.data_dir} aside and run again to copy your "
                    f"memories there.")
elif emb(old) != emb(new):
    out["action"] = "stop"
    out["error"] = (f"Your memories in {old.data_dir} were embedded with {emb(old)}, and "
                    f"{where} names {emb(new)}. Search compares those vectors directly, so "
                    f"mixing two models makes the old memories hard to find. Nothing was "
                    f"changed. Set the embedding back to {emb(old)} and run again.")
else:
    out["action"] = "copy"
print(json.dumps(out))
PY
)
    action=$(printf '%s' "$plan" | python3 -c 'import sys,json;print(json.load(sys.stdin)["action"])' 2>/dev/null || echo error)
    plan_get() { printf '%s' "$plan" | python3 -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

    case "$action" in
      none)
        set_step migration "{\"ok\": true, \"action\": \"none\"}"
        ;;
      stop)
        set_step migration "$(printf '%s' "$plan" | python3 -c 'import sys,json;d=json.load(sys.stdin);d["ok"]=False;print(json.dumps(d))')"
        emit false "$(plan_get error)"
        ;;
      copy)
        OLD_DIR=$(plan_get from)
        NEW_DIR=$(plan_get to)
        # The old service writes to OLD_DIR: stop it before copying.
        if [ "$SYSTEMD_ENABLED" = "1" ]; then
          systemctl --user stop "${SYSTEMD_SVC}.service" >/dev/null 2>&1 || true
        fi
        if curl -s -m 2 "$(svc_url)/health" 2>/dev/null | python3 -c 'import sys,json;sys.exit(0 if "status" in json.load(sys.stdin) else 1)' 2>/dev/null; then
          set_step migration "{\"ok\": false, \"action\": \"copy\"}"
          stopped=""
          [ "$SYSTEMD_ENABLED" = "1" ] && stopped=" The ${SYSTEMD_SVC} service was stopped, but another one still answers."
          emit false "A Mnemo Cortex server is running on port $SVC_PORT and may be writing to $OLD_DIR.${stopped} Stop it and run again. Your memories and settings were not changed."
        fi
        log "copying your memories from $OLD_DIR to $NEW_DIR"
        copied=$(python3 - "$OLD_DIR" "$NEW_DIR" <<'PY'
import hashlib, json, os, shutil, sys

src, dst = sys.argv[1:3]

def digest(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

try:
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
except Exception as e:  # shutil.Error lists every file that failed
    print(json.dumps({"ok": False, "error": str(e)[:500]}))
    sys.exit(0)
count, bad = 0, []
for root, _dirs, files in os.walk(src):
    for name in files:
        s = os.path.join(root, name)
        rel = os.path.relpath(s, src)
        d = os.path.join(dst, rel)
        if os.path.islink(s):
            same = os.path.islink(d) and os.readlink(s) == os.readlink(d)
        else:
            same = os.path.isfile(d) and not os.path.islink(d) and digest(s) == digest(d)
        count += 1
        if not same:
            bad.append(rel)
print(json.dumps({"ok": not bad, "files": count, "mismatched": bad[:20]}))
PY
)
        if ! printf '%s' "$copied" | python3 -c 'import sys,json;sys.exit(0 if json.load(sys.stdin)["ok"] else 1)' 2>/dev/null; then
          set_step migration "{\"ok\": false, \"action\": \"copy\", \"from\": $(printf '%s' "$OLD_DIR" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))'), \"result\": ${copied:-null}}"
          emit false "Copying your memories from $OLD_DIR to $NEW_DIR did not check out (see steps.migration). Your originals in $OLD_DIR are untouched. Move $NEW_DIR aside and run again. The server is stopped."
        fi
        n=$(printf '%s' "$copied" | python3 -c 'import sys,json;print(json.load(sys.stdin)["files"])')
        log "copied $n files from $OLD_DIR to $NEW_DIR and checked every one. The originals are still in $OLD_DIR; delete them yourself when you're happy."
        set_step migration "$(python3 -c 'import json,sys;print(json.dumps({"ok": True, "action": "copied", "from": sys.argv[1], "to": sys.argv[2], "files": int(sys.argv[3]), "original_left_in": sys.argv[1]}))' "$OLD_DIR" "$NEW_DIR" "$n")"
        ;;
      *)
        set_step migration '{"ok": false, "action": "unknown"}'
        emit false "Could not read the existing install's settings (see stderr). Your settings and memories were not changed."
        ;;
    esac
  fi

  # 3b. A config this installer generated earlier has no api_key, so the
  # server would call the provider with an empty key. Add the ${NAME}
  # reference (never the key itself); keep a copy of the file as it was.
  if [ -f "$CONFIG_FILE" ] && [ -n "$REASON_API_KEY_ENV" ]; then
    patched=$(patch_old_config apply)
    if [ -n "$patched" ]; then
      log "added api_key: \${$REASON_API_KEY_ENV} to $CONFIG_FILE (the file as it was: $patched)"
    fi
  fi

  if [ -f "$CONFIG_FILE" ]; then
    # Preserve an existing config. It wins over the manifest: data_dir,
    # reasoning, embedding, host and port come from it, not robot.install.
    log "config already exists, keeping it (its data_dir, reasoning, embedding, host and port win over the manifest): $CONFIG_FILE"
  else
    python3 - "$CONFIG_FILE" "$DATA_DIR" \
      "$REASON_PROVIDER" "$REASON_MODEL" "$REASON_API_BASE" "$REASON_API_KEY_ENV" \
      "$EMB_PROVIDER" "$EMB_MODEL" "$EMB_API_BASE" "$EMB_API_KEY_ENV" \
      "$SVC_HOST" "$SVC_PORT" <<'PY'
import json, sys
from pathlib import Path

(cfg, data_dir, r_prov, r_model, r_base, r_key,
 e_prov, e_model, e_base, e_key, host, port) = sys.argv[1:13]
q = json.dumps  # a JSON string is a valid YAML double-quoted scalar

def key_line(name):
    # The server resolves "${NAME}" from its environment (mnemo-cortex.env).
    return f"  api_key: {q('${' + name + '}')}\n" if name else ""

Path(cfg).write_text(
    "# Mnemo Cortex config — generated by robot-install.sh\n"
    "# The service loads this file (AGENTB_CONFIG in mnemo-cortex.env).\n"
    "# API keys are not stored here: \"${NAME}\" is read from mnemo-cortex.env.\n"
    f"data_dir: {q(data_dir)}\n"
    "log_level: info\n\n"
    "reasoning:\n"
    f"  provider: {q(r_prov)}\n  model: {q(r_model)}\n  api_base: {q(r_base)}\n"
    + key_line(r_key) +
    "  timeout: 30\n\n"
    "embedding:\n"
    f"  provider: {q(e_prov)}\n  model: {q(e_model)}\n  api_base: {q(e_base)}\n"
    + key_line(e_key) +
    "\nserver:\n"
    f"  host: {q(host)}\n  port: {int(port)}\n"
)
PY
  fi

  # Env file — loaded by the systemd unit. Points the server at the config,
  # holds the API keys (read from the install-time environment) and the v3
  # decay overrides. Create it 0600 BEFORE writing: write-then-chmod leaves a
  # window where the key is world-readable on a multi-user host.
  if ! names=$(config_key_names "$CONFIG_FILE"); then
    set_step config "$(python3 -c 'import json,sys;print(json.dumps({"ok": False, "config_path": sys.argv[1]}))' "$CONFIG_FILE")"
    emit false "Could not read $CONFIG_FILE (see stderr)."
  fi
  key_lines=""
  for name in $names; do
    key_lines+="$name=$(key_value "$name")"$'\n'
  done

  install -m 600 /dev/null "$ENV_FILE"
  {
    echo "# Mnemo Cortex env — generated by robot-install.sh"
    echo "AGENTB_CONFIG=$CONFIG_FILE"
    echo "MNEMO_DECAY_TOPOLOGY_WARN_DAYS=$DECAY_TOPO_WARN"
    echo "MNEMO_DECAY_TOPOLOGY_STALE_DAYS=$DECAY_TOPO_STALE"
    echo "MNEMO_DECAY_CURRENT_STATE_WARN_DAYS=$DECAY_CURRENT_WARN"
    echo "MNEMO_DECAY_RELATIONSHIP_WARN_DAYS=$DECAY_REL_WARN"
    echo "MNEMO_DECAY_SESSION_LOG_WARN_DAYS=$DECAY_SLOG_WARN"
    printf '%s' "$key_lines"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  # The data dir the server will use: the config's, which can differ from
  # the manifest's when an existing config was kept.
  if ! EFFECTIVE_DATA_DIR=$(agentb_py "$CONFIG_FILE" <<'PY'
import os, sys
from agentb.config import load_config
print(os.path.abspath(load_config(sys.argv[1]).data_dir))
PY
  ) || [ -z "$EFFECTIVE_DATA_DIR" ]; then
    set_step config "$(python3 -c 'import json,sys;print(json.dumps({"ok": False, "config_path": sys.argv[1]}))' "$CONFIG_FILE")"
    emit false "The server could not load $CONFIG_FILE (see stderr)."
  fi
  mkdir -p "$EFFECTIVE_DATA_DIR"/{memory,logs}

  set_step config "$(python3 -c 'import json,sys;print(json.dumps({"ok": True, "config_path": sys.argv[1], "env_path": sys.argv[2], "data_dir": sys.argv[3], "kept_existing_config": sys.argv[4] == "true"}))' "$CONFIG_FILE" "$ENV_FILE" "$EFFECTIVE_DATA_DIR" "$KEPT_CONFIG")"
fi

# The last lines the service logged, so a failure says why in the server's
# own words. Empty when there is no systemd unit or no journal.
service_log_tail() {
  [ "$SYSTEMD_ENABLED" = "1" ] || return 0
  journalctl --user -u "${SYSTEMD_SVC}.service" -n 12 --no-pager -o cat 2>/dev/null | tail -n 12
}

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
ExecStart=$PY_BIN -m uvicorn agentb.server:create_app --factory --host $SVC_HOST --port $SVC_PORT --log-level info
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  # restart, not `enable --now`: on a re-run the service is already running
  # with the old settings, and `--now` leaves a running service alone.
  if ! { systemctl --user enable "${SYSTEMD_SVC}.service" && systemctl --user restart "${SYSTEMD_SVC}.service"; } >/dev/null 2>&1; then
    set_step systemd "{\"ok\": false, \"service\": \"$SYSTEMD_SVC\", \"port\": $SVC_PORT, \"error\": \"service failed to start\"}"
    emit false "systemd: ${SYSTEMD_SVC}.service failed to start. Check: journalctl --user -u ${SYSTEMD_SVC}.service"
  fi
  set_step systemd "{\"ok\": true, \"service\": \"$SYSTEMD_SVC\", \"port\": $SVC_PORT}"
fi

# ─── step 5/5: smoke test ─────────────────────────────────────────────

if [ "$SMOKE_ENABLED" != "1" ] || [ "$DRY_RUN" = "1" ]; then
  log "step 5/5 — smoke test (skipped)"
  set_step smoke_test '{"ok": true, "skipped": true}'
  finish
fi

log "step 5/5 — smoke test"

# Wait up to 15s for /health
attempts=0
while [ $attempts -lt 15 ]; do
  health=$(curl -s -m 2 "$(svc_url)/health" 2>/dev/null || true)
  if echo "$health" | python3 -c 'import sys,json;sys.exit(0 if json.load(sys.stdin).get("status") in ("ok","degraded") else 1)' 2>/dev/null; then
    break
  fi
  sleep 1
  attempts=$((attempts + 1))
done

if [ $attempts -ge 15 ]; then
  tail_txt=$(service_log_tail)
  set_step smoke_test "$(python3 -c 'import json,sys;print(json.dumps({"ok": False, "error": "/health unreachable after 15s", "service_log_tail": sys.argv[1].splitlines()}))' "$tail_txt")"
  # The server's last error line is usually the reason (e.g. a startup refusal).
  why=$(printf '%s\n' "$tail_txt" | grep -E '^[A-Za-z_.]+(Error|Exception): ' | tail -n 1)
  [ -n "$why" ] || why=$(printf '%s\n' "$tail_txt" | grep -E 'ERROR|Error' | tail -n 1)
  if [ -n "$tail_txt" ]; then
    hint=" Last log lines: steps.smoke_test.service_log_tail."
  else
    hint=" Check the server's log (journalctl --user -u ${SYSTEMD_SVC}.service)."
  fi
  emit false "smoke test: the server did not come up on port $SVC_PORT.${why:+ It said: $why}$hint"
fi

# A server that answers is not enough: it must be running on the config this
# installer wrote. Compare what /health reports with what the config says.
expected=$(agentb_py "$CONFIG_FILE" "$SMOKE_AGENT" "$ENV_FILE" <<'PY'
import json, os, sys
# The server resolves ${VAR}s from its env file: do the same.
for line in open(sys.argv[3]):
    k, sep, v = line.rstrip("\n").partition("=")
    if sep and not k.startswith("#"):
        os.environ[k] = v
from agentb.config import load_config, get_agent_data_dir
cfg = load_config(sys.argv[1])
label = lambda p: f"{p.provider}/{p.model}"
print(json.dumps({"reasoning": label(cfg.reasoning.primary),
                  "embedding": label(cfg.embedding.primary),
                  "memory_dir": os.path.abspath(get_agent_data_dir(cfg, sys.argv[2]) / "memory"),
                  "token": cfg.server.auth_token}))
PY
)
mismatch=$(python3 - "$health" "$expected" <<'PY'
import json, sys
try:
    health, want = json.loads(sys.argv[1]), json.loads(sys.argv[2])
except ValueError:
    print("could not read /health or the config"); sys.exit(0)
for part in ("reasoning", "embedding"):
    have = (health.get(part) or {}).get("primary")
    if have != want[part]:
        print(f"{part} is {have}, the config says {want[part]}")
PY
)
if [ -n "$mismatch" ]; then
  set_step smoke_test "$(python3 -c 'import json,sys;print(json.dumps({"ok": False, "error": "server is not using the config", "detail": sys.argv[1]}))' "$mismatch")"
  emit false "smoke test: the running server is not using $CONFIG_FILE ($(echo "$mismatch" | paste -sd ';' -))"
fi
MEMORY_DIR=$(printf '%s' "$expected" | python3 -c 'import sys,json;print(json.load(sys.stdin)["memory_dir"])')
API_TOKEN=$(printf '%s' "$expected" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# POST to the service, with the config's auth token when it sets one (the
# header goes in on stdin so the token never shows in the process list).
api_post() {
  if [ -n "$API_TOKEN" ]; then
    printf 'X-API-KEY: %s\n' "$API_TOKEN" | curl -s -m "$1" -X POST "$(svc_url)$2" -H @- -H 'Content-Type: application/json' -d "$3"
  else
    curl -s -m "$1" -X POST "$(svc_url)$2" -H 'Content-Type: application/json' -d "$3"
  fi
}

# save → recall round-trip with a unique marker
MARKER="robot-install-marker-$$-$(date +%s)"
save=$(api_post 5 /writeback "{\"session_id\":\"robot-install\",\"summary\":\"smoke test $MARKER. Mnemo Cortex robot.install verification.\",\"key_facts\":[\"marker $MARKER\"],\"projects_referenced\":[],\"decisions_made\":[],\"agent_id\":\"$SMOKE_AGENT\",\"source\":\"tool\",\"category\":\"session_log\"}")

if ! echo "$save" | python3 -c 'import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get("status")=="archived" else 1)' 2>/dev/null; then
  set_step smoke_test "{\"ok\": false, \"error\": \"save failed\", \"response\": $(echo "$save" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')}"
  emit false "smoke test: save call failed"
fi

memory_id=$(echo "$save" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("memory_id",""))')

# The saved memory must land in the configured data dir.
if [ -z "$memory_id" ] || [ ! -f "$MEMORY_DIR/$memory_id.json" ]; then
  set_step smoke_test "$(python3 -c 'import json,sys;print(json.dumps({"ok": False, "error": "memory not in the configured data dir", "memory_id": sys.argv[1], "expected_in": sys.argv[2]}))' "$memory_id" "$MEMORY_DIR")"
  emit false "smoke test: the server saved $memory_id, but not in $MEMORY_DIR (the data dir in $CONFIG_FILE)"
fi

sleep 1
recall=$(api_post 10 /context "{\"prompt\":\"$MARKER\",\"agent_id\":\"$SMOKE_AGENT\",\"max_results\":3,\"category\":\"session_log\"}")

hits=$(echo "$recall" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d.get("chunks",[])))' 2>/dev/null || echo 0)

if [ "${hits:-0}" -ge 1 ]; then
  set_step smoke_test "{\"ok\": true, \"health\": \"ok\", \"memory_id\": \"$memory_id\", \"recall_hits\": $hits, \"marker\": \"$MARKER\"}"
  finish
else
  set_step smoke_test "{\"ok\": false, \"health\": \"ok\", \"memory_id\": \"$memory_id\", \"recall_hits\": $hits, \"marker\": \"$MARKER\"}"
  emit false "smoke test: marker $MARKER saved as $memory_id but did not return on recall"
fi
