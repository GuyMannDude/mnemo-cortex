#!/usr/bin/env bash
# End-to-end test of robot-install.sh.
#
#   bash tests/robot-install-e2e.sh
#
# Every case gets its own scratch HOME and runs the real installer. The server
# is really started: a systemctl stand-in runs the unit file the installer
# wrote the way systemd does (WorkingDirectory, EnvironmentFile, ExecStart,
# clean environment), and a journalctl stand-in shows that unit's output.
# Reasoning goes to a fake OpenRouter endpoint that answers 200 only to the
# right Bearer key; embeddings use a real Ollama with nomic-embed-text.
# Cases that need Ollama print SKIP when it is absent. Nothing outside the
# scratch folder is touched; it is printed at the end for inspection.
#
# Env: OLLAMA_URL (default http://localhost:11434)
#      ROBOT_E2E_VENV=<dir> reuses a venv between runs (else one per run)
# Exit: 0 = every case that ran passed; 1 = a failure.

set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/robot-e2e.XXXXXX")
OLLAMA=${OLLAMA_URL:-http://localhost:11434}
VENV=${ROBOT_E2E_VENV:-$WORK/venv}
KEY="sk-or-""e2e-""fake-key"          # split so secret scanners don't flag it
MUTANT="$REPO/.robot-install-e2e-mutant.sh"
PASS=0 FAIL=0 SKIP=0

pass() { PASS=$((PASS + 1)); echo "  PASS $*"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL $*"; }
skip() { SKIP=$((SKIP + 1)); echo "SKIP $*"; }

free_port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }
# jget <file> <python expression over d>: read the installer's JSON result.
# eval is safe here: every expression is a literal written in this script.
jget() { python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(eval(sys.argv[2]))" "$1" "$2" 2>/dev/null; }
snap() { (cd "$1" && find . -type f ! -path './.config/systemd/*' -exec sha256sum {} + | sort -k2); }

# ── stand-ins ────────────────────────────────────────────────────────────
mkdir -p "$WORK/bin"
cat > "$WORK/bin/systemctl" <<'PY'
#!/usr/bin/env python3
# Test stand-in for `systemctl --user`: daemon-reload, enable [--now],
# restart, stop. Runs the unit the way systemd does.
import os, shlex, signal, subprocess, sys, time
args = [a for a in sys.argv[1:] if a != "--user"]
unitdir = os.environ.get("MNEMO_INSTALL_SYSTEMD_DIR") or os.path.expanduser("~/.config/systemd/user")
if args[0] == "daemon-reload":
    sys.exit(0)
unit = [a for a in args[1:] if not a.startswith("--")][0]
path = os.path.join(unitdir, unit)
pidf = path + ".pid"

def pid():
    try:
        p = int(open(pidf).read()); os.kill(p, 0); return p
    except Exception:
        return None

def stop():
    p = pid()
    if p:
        os.killpg(p, signal.SIGTERM)
        for _ in range(100):
            try:
                os.kill(p, 0); time.sleep(0.1)
            except ProcessLookupError:
                break
        else:
            os.killpg(p, signal.SIGKILL)
    if os.path.exists(pidf):
        os.remove(pidf)

def start():
    sec = {}
    for line in open(path):
        if "=" in line and not line.startswith("#"):
            k, v = line.rstrip("\n").split("=", 1)
            sec[k.strip()] = v.strip()
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"}
    for line in open(sec["EnvironmentFile"]):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    out = open(path + ".log", "a")
    proc = subprocess.Popen(shlex.split(sec["ExecStart"]), cwd=sec["WorkingDirectory"], env=env,
                            stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    open(pidf, "w").write(str(proc.pid))

if args[0] == "enable":
    if not os.path.exists(path):
        sys.exit(1)
    if "--now" in args and not pid():
        start()
elif args[0] == "restart":
    stop(); start()
elif args[0] == "stop":
    stop()
else:
    sys.exit(2)
PY
cat > "$WORK/bin/journalctl" <<'PY'
#!/usr/bin/env python3
# Test stand-in for `journalctl --user -u UNIT -n N`.
import os, sys
a = sys.argv[1:]
unit = a[a.index("-u") + 1]
n = int(a[a.index("-n") + 1]) if "-n" in a else 10
d = os.environ.get("MNEMO_INSTALL_SYSTEMD_DIR") or os.path.expanduser("~/.config/systemd/user")
try:
    print("\n".join(open(os.path.join(d, unit + ".log")).read().splitlines()[-n:]))
except FileNotFoundError:
    pass
PY
chmod +x "$WORK/bin/systemctl" "$WORK/bin/journalctl"

# ── fake OpenRouter: /key is 200 only for the right key ──────────────────
cat > "$WORK/fake_openrouter.py" <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port, key = int(sys.argv[1]), sys.argv[2]
class H(BaseHTTPRequestHandler):
    def _reply(self, body):
        ok = self.headers.get("Authorization") == "Bearer " + key
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self._reply(b'{"data":{}}')
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._reply(json.dumps({"choices": [{"message": {"content": "session_log"}}]}).encode())
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
OR_PORT=$(free_port)
python3 "$WORK/fake_openrouter.py" "$OR_PORT" "$KEY" & OR_PID=$!

HOMES=()
cleanup() {
  for h in "${HOMES[@]}"; do
    HOME="$h" "$WORK/bin/systemctl" --user stop mnemo-cortex.service 2>/dev/null
  done
  kill "$OR_PID" 2>/dev/null
  rm -f "$MUTANT"
}
trap cleanup EXIT

# manifest <port> [host]: the default robot.install values, pointed at the
# fake endpoint and the test's Ollama.
manifest() {
  python3 - "$WORK/manifest-$1.json" "$1" "${2:-127.0.0.1}" "$OR_PORT" "$OLLAMA" <<'PY'
import json, sys
out, port, host, orp, ollama = sys.argv[1:6]
json.dump({
  "service": {"host": host, "port": int(port), "data_dir": "~/.mnemo-cortex"},
  "reasoning": {"provider": "openrouter", "model": "google/gemini-2.5-flash",
                "api_base": f"http://127.0.0.1:{orp}", "api_key_env": "OPENROUTER_API_KEY"},
  "embedding": {"provider": "ollama", "model": "nomic-embed-text", "api_base": ollama},
}, open(out, "w"))
print(out)
PY
}

# install <case> <manifest> [nokey] [installer]: run the installer in the
# case's HOME. Result JSON in $WORK/<case>.json, rc in RC.
install() {
  local home="$WORK/$1" key="$KEY"
  [ "${3:-}" = nokey ] && key=""
  mkdir -p "$home"; HOMES+=("$home")
  env -i HOME="$home" PATH="$WORK/bin:/usr/bin:/bin" \
    PIP_CACHE_DIR="${PIP_CACHE_DIR:-$HOME/.cache/pip}" MNEMO_INSTALL_VENV_DIR="$VENV" \
    ${key:+OPENROUTER_API_KEY=$key} \
    bash "${4:-$REPO/robot-install.sh}" "$2" > "$WORK/$1.json" 2> "$WORK/$1.err"
  RC=$?
}
stop_svc() { HOME="$WORK/$1" "$WORK/bin/systemctl" --user stop mnemo-cortex.service 2>/dev/null; }
health() { curl -s -m 5 "http://127.0.0.1:$1/health"; }

# old_install <case> <port> <seed>: the state an install made before the
# service loaded its config leaves behind: the old installer's config (no
# api_key) and env file (no AGENTB_CONFIG), an empty ~/.mnemo-cortex, and a
# unit that starts the server on its defaults. With seed=yes, ~/.agentb gets
# memories written by a real server the way a pre-4.9.5 service wrote them.
old_install() {
  local home="$WORK/$1" port=$2 cfgdir="$WORK/$1/.config/mnemo-cortex"
  local unitdir="$WORK/$1/.config/systemd/user"
  mkdir -p "$cfgdir" "$unitdir" "$home/.mnemo-cortex/memory" "$home/.mnemo-cortex/logs"; HOMES+=("$home")
  cat > "$cfgdir/mnemo-cortex.yaml" <<EOF
# Mnemo Cortex config — generated by robot-install.sh
data_dir: $home/.mnemo-cortex
log_level: info

reasoning:
  provider: openrouter
  model: google/gemini-2.5-flash
  api_base: http://127.0.0.1:$OR_PORT
  timeout: 30

embedding:
  provider: ollama
  model: nomic-embed-text
  api_base: $OLLAMA

server:
  host: 127.0.0.1
  port: $port
EOF
  printf '%s\n' "# Mnemo Cortex env — generated by robot-install.sh" \
    "AGENTB_DATA_DIR=$home/.mnemo-cortex" "OPENROUTER_API_KEY=$KEY" > "$cfgdir/mnemo-cortex.env"
  cat > "$unitdir/mnemo-cortex.service" <<EOF
[Service]
WorkingDirectory=$REPO
EnvironmentFile=$cfgdir/mnemo-cortex.env
ExecStart=$VENV/bin/python -m uvicorn agentb.server:create_app --factory --host 127.0.0.1 --port $port --log-level info
EOF
  [ "$3" = yes ] || return 0
  # A loopback-only config stands in for the pre-4.9.5 defaults (the server
  # now refuses its 0.0.0.0 default): data dir and providers stay default.
  printf 'server:\n  host: "127.0.0.1"\nembedding:\n  provider: ollama\n  model: nomic-embed-text\n  api_base: "%s"\n' \
    "$OLLAMA" > "$WORK/$1-seed.yaml"
  (cd "$REPO" && env -i HOME="$home" PATH=/usr/bin:/bin AGENTB_CONFIG="$WORK/$1-seed.yaml" \
    "$VENV/bin/python" -m uvicorn agentb.server:create_app --factory --host 127.0.0.1 --port "$port" \
    > "$WORK/$1-seed.log" 2>&1) & local seed=$!
  for _ in $(seq 1 60); do health "$port" >/dev/null && break; sleep 0.5; done
  for text in "The blue teapot lives on shelf three, behind the cookbooks." \
              "Quarterly invoices go out on the first Monday, signed in green ink."; do
    curl -s -m 60 -X POST "http://127.0.0.1:$port/writeback" -H 'Content-Type: application/json' \
      -d "{\"session_id\":\"old\",\"summary\":\"$text\",\"key_facts\":[],\"projects_referenced\":[],\"decisions_made\":[],\"agent_id\":\"old-agent\",\"source\":\"user\",\"category\":\"decision\"}" >/dev/null
  done
  pkill -TERM -P "$seed" 2>/dev/null; kill "$seed" 2>/dev/null; wait "$seed" 2>/dev/null
  for _ in $(seq 1 40); do health "$port" >/dev/null || break; sleep 0.25; done
}

echo "robot-install e2e — repo $REPO"
echo "scratch: $WORK"
HAVE_OLLAMA=0
curl -s -m 3 "$OLLAMA/api/tags" | grep -q '"nomic-embed-text' && HAVE_OLLAMA=1
[ "$HAVE_OLLAMA" = 1 ] || echo "(no Ollama with nomic-embed-text at $OLLAMA: cases that save memories are skipped)"

# The venv is built by the first install that needs it; g never gets there.
if [ ! -x "$VENV/bin/python" ]; then
  echo "building the venv (pip install -e .) …"
  python3 -m venv "$VENV" && "$VENV/bin/pip" install --quiet -e "$REPO" >"$WORK/venv.log" 2>&1 \
    || { echo "FAIL venv build (see $WORK/venv.log)"; exit 1; }
fi

# ── g: key unset on a fresh install ──────────────────────────────────────
echo "== g: cloud reasoning, key not set"
P=$(free_port); install g "$(manifest "$P")" nokey
[ "$RC" = 1 ] && [ "$(jget "$WORK/g.json" 'd["ok"]')" = False ] && pass "stops (ok:false)" || fail "expected ok:false, rc=$RC"
grep -q "OPENROUTER_API_KEY" <<<"$(jget "$WORK/g.json" 'd["error"]')" && pass "error names the key" || fail "error does not name the key"
[ -z "$(ls -A "$WORK/g")" ] && pass "nothing written to HOME" || fail "HOME not empty: $(ls -A "$WORK/g")"

# ── mutant: without AGENTB_CONFIG the install must FAIL ──────────────────
echo "== mutant: installer with the AGENTB_CONFIG line removed must fail"
sed '/echo "AGENTB_CONFIG=\$CONFIG_FILE"/d' "$REPO/robot-install.sh" > "$MUTANT"
if [ "$(diff "$REPO/robot-install.sh" "$MUTANT" | grep -c '^<')" != 1 ]; then
  fail "mutant did not remove exactly one line (the control can't fail)"
else
  P=$(free_port); install mutant "$(manifest "$P")" "" "$MUTANT"; stop_svc mutant
  [ "$RC" = 1 ] && pass "mutant install fails" || fail "mutant install passed (rc=$RC): the smoke test can't catch the bug"
  grep -q "Refusing to start" <<<"$(jget "$WORK/mutant.json" 'd.get("error","")')" \
    && pass "error quotes the server's refusal" || fail "error lacks the server's reason: $(jget "$WORK/mutant.json" 'd.get("error")')"
fi
rm -f "$MUTANT"

if [ "$HAVE_OLLAMA" != 1 ]; then
  skip "a, b/f, c, d, h (need Ollama with nomic-embed-text at $OLLAMA)"
else
  # ── a: fresh install ───────────────────────────────────────────────────
  echo "== a: fresh install"
  PA=$(free_port); install a "$(manifest "$PA")"
  [ "$RC" = 0 ] && pass "ok:true" || fail "rc=$RC: $(jget "$WORK/a.json" 'd.get("error")')"
  h=$(health "$PA")
  [ "$(python3 -c 'import json,sys;d=json.loads(sys.argv[1]);print(d["reasoning"]["primary"],d["reasoning"]["healthy"])' "$h" 2>/dev/null)" = "openrouter/google/gemini-2.5-flash True" ] \
    && pass "/health: configured reasoning provider, key accepted" || fail "/health: $h"
  grep -q "^AGENTB_CONFIG=" "$WORK/a/.config/mnemo-cortex/mnemo-cortex.env" && pass "env file points at the config" || fail "no AGENTB_CONFIG in env file"
  grep -q "sk-or-" "$WORK/a/.config/mnemo-cortex/mnemo-cortex.yaml" && fail "key value written into the yaml" || pass "no key value in the yaml"
  stop_svc a

  # ── b/f: old install, service refusing to start, memories in ~/.agentb ─
  echo "== b/f: install from before the fix, re-run"
  PB=$(free_port); old_install b "$PB" yes
  HOME="$WORK/b" "$WORK/bin/systemctl" --user restart mnemo-cortex.service; sleep 4
  grep -q "Refusing to start" "$WORK/b/.config/systemd/user/mnemo-cortex.service.log" \
    && pass "old service refuses to start (the customer's state)" || fail "old service did not refuse: see $WORK/b/.config/systemd/user/mnemo-cortex.service.log"
  [ -n "$(find "$WORK/b/.agentb" -name '*.json' -path '*memory*' 2>/dev/null)" ] && pass "~/.agentb holds memories" || fail "seeding wrote no memories"
  (cd "$WORK/b/.agentb" && find . -type f -exec sha256sum {} + | sort -k2) > "$WORK/b.orig.sha"
  install b "$(manifest "$PB")"
  [ "$RC" = 0 ] && pass "re-run ok:true" || fail "rc=$RC: $(jget "$WORK/b.json" 'd.get("error")')"
  [ "$(jget "$WORK/b.json" 'd["steps"]["migration"]["action"]')" = copied ] && pass "memories copied" || fail "migration: $(jget "$WORK/b.json" 'd["steps"].get("migration")')"
  [ "$(jget "$WORK/b.json" 'd["steps"]["migration"]["reasoning_from"] + " -> " + d["steps"]["migration"]["reasoning_to"]')" = "ollama/qwen2.5:32b-instruct -> openrouter/google/gemini-2.5-flash" ] \
    && pass "reasoning change reported" || fail "reasoning change not reported"
  grep -q "reasoning changes:" "$WORK/b.err" && pass "reasoning change logged in plain words" || fail "no reasoning-change log line"
  r=$(curl -s -m 60 -X POST "http://127.0.0.1:$PB/context" -H 'Content-Type: application/json' \
      -d '{"prompt":"where does the blue teapot live","agent_id":"old-agent","max_results":2}')
  grep -q "shelf three" <<<"$r" && pass "old memory recalled through the new server" || fail "recall: $r"
  (cd "$WORK/b/.agentb" && find . -type f -exec sha256sum {} + | sort -k2) | diff -q - "$WORK/b.orig.sha" >/dev/null \
    && pass "originals in ~/.agentb unchanged" || fail "originals changed"
  stop_svc b

  # ── d: run twice ───────────────────────────────────────────────────────
  echo "== d: run the installer again on b"
  cfg_sum=$(sha256sum < "$WORK/b/.config/mnemo-cortex/mnemo-cortex.yaml")
  install b "$(manifest "$PB")"
  [ "$RC" = 0 ] && pass "second run ok:true" || fail "rc=$RC: $(jget "$WORK/b.json" 'd.get("error")')"
  [ "$(jget "$WORK/b.json" '"migration" in d["steps"]')" = False ] && pass "no second migration" || fail "migrated again"
  [ "$(sha256sum < "$WORK/b/.config/mnemo-cortex/mnemo-cortex.yaml")" = "$cfg_sum" ] && pass "config unchanged" || fail "config changed"
  stop_svc b

  # ── c: both folders hold files ─────────────────────────────────────────
  echo "== c: memories in ~/.agentb and files in the configured folder"
  PC=$(free_port); old_install c "$PC" yes
  echo '{"stray": true}' > "$WORK/c/.mnemo-cortex/memory/stray.json"
  before=$(snap "$WORK/c")
  install c "$(manifest "$PC")"
  [ "$RC" = 1 ] && [ "$(jget "$WORK/c.json" 'd["steps"]["migration"]["action"]')" = stop ] && pass "stops" || fail "rc=$RC"
  [ "$(snap "$WORK/c")" = "$before" ] && pass "nothing changed" || fail "files changed"

  # ── h: embedding model differs from the one the memories were made with ─
  echo "== h: config names a different embedding model"
  PH=$(free_port); old_install h "$PH" yes
  sed -i 's/model: nomic-embed-text/model: mxbai-embed-large/' "$WORK/h/.config/mnemo-cortex/mnemo-cortex.yaml"
  before=$(snap "$WORK/h")
  install h "$(manifest "$PH")"
  [ "$RC" = 1 ] && grep -q "embedded with" <<<"$(jget "$WORK/h.json" 'd["error"]')" && pass "stops, names both models" || fail "rc=$RC: $(jget "$WORK/h.json" 'd.get("error")')"
  [ "$(snap "$WORK/h")" = "$before" ] && pass "nothing changed" || fail "files changed"
fi

echo
echo "passed $PASS, failed $FAIL, skipped $SKIP   (scratch: $WORK)"
[ "$FAIL" = 0 ]
