#!/usr/bin/env bash
# wheel-smoke-test — verify a built wheel actually installs and runs.
#
# Catches the class of failure where pyproject.toml drifts from the runtime
# layout (missing packages, missing package-data, missing dependencies).
#
# Run after every change to pyproject.toml:
#   bash scripts/wheel-smoke-test.sh
#
# Exits 0 on success, non-zero on any check failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR="$(mktemp -d -t mnemo-wheel-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

cd "$REPO_ROOT"

echo "==> cleaning previous build artifacts"
rm -rf dist build mnemo_cortex.egg-info

echo "==> building wheel"
python3 -m build --wheel --quiet 2>&1 | tail -5

WHEEL=$(ls dist/mnemo_cortex-*.whl | head -1)
[ -n "$WHEEL" ] || { echo "FAIL: no wheel produced"; exit 1; }
echo "    wheel: $(basename "$WHEEL")"

echo "==> creating clean venv"
python3 -m venv "$TMPDIR/venv"
# shellcheck disable=SC1091
source "$TMPDIR/venv/bin/activate"

echo "==> installing wheel (with all required deps)"
pip install --quiet "$WHEEL"

echo "==> import smoke tests"
python3 - <<'PY'
import importlib
import importlib.resources as r

REQUIRED_PACKAGES = ["agentb", "mnemo_v2", "passport", "sparks_bus"]
PACKAGE_DATA = [
    ("mnemo_v2", "db/schema.sql"),
    ("sparks_bus", "schema.sql"),
]

for mod in REQUIRED_PACKAGES:
    importlib.import_module(mod)
    print(f"    ✓ import {mod}")

for pkg, path in PACKAGE_DATA:
    res = r.files(pkg)
    for p in path.split("/"):
        res = res / p
    assert res.is_file(), f"missing package data: {pkg}/{path}"
    print(f"    ✓ {pkg}/{path} present ({res.stat().st_size} bytes)")
PY

echo "==> CLI version flag"
VERSION_OUT=$(mnemo-cortex --version)
echo "    $VERSION_OUT"

echo "==> CLI doctor (no-config path — should not crash)"
# `doctor --help` doesn't need a config; just exercises the click/rich/import chain.
mnemo-cortex doctor --help > /dev/null && echo "    ✓ doctor --help"

deactivate

echo
echo "✓ wheel smoke test passed"
