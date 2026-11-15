"""Guide facts: the checkable claims in robot.info, robot.install and llms.txt,
each held against the code that makes it true.

Agents read these three files instead of the code, so a wrong port, a missing
tool or a stale default sends them the wrong way with full confidence. Each
test below pairs one kind of claim with its source of truth; a guide edit or a
code change that breaks a pair fails the build, and the message says which
file to fix.

What this cannot catch: prose about behaviour ("agent_startup loads ..."),
what a cold agent will misread, anything that needs real systemd / macOS or a
paid provider, and claims in other repos or on the site. The cold test per
release stays the check for those.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _jsonc(name: str) -> dict:
    """robot.info / robot.install: JSON with // line comments (ROBOT-INFO-SPEC)."""
    raw = (ROOT / name).read_text(encoding="utf-8")
    return json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))


INFO = _jsonc("robot.info")
INSTALL = _jsonc("robot.install")
INSTALL_RAW = (ROOT / "robot.install").read_text(encoding="utf-8")
LLMS = (ROOT / "llms.txt").read_text(encoding="utf-8")
PATHS = INFO["install"]["install_paths"]


def _pyproject() -> str:
    return (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _llms(pattern: str) -> str:
    m = re.search(pattern, LLMS)
    assert m, f"llms.txt no longer has a line matching {pattern!r}; update this test with the guide"
    return m.group(1)


# ── version ───────────────────────────────────────────────────────────────

def test_llms_txt_version_matches_release():
    version = re.search(r'^version\s*=\s*"([^"]+)"', _pyproject(), flags=re.M).group(1)
    assert _llms(r"Current version (\d+\.\d+\.\d+)") == version, (
        "llms.txt 'Current version' is not pyproject.toml's version: update llms.txt with the bump"
    )


def test_runtime_matches_requires_python():
    m = re.search(r'^requires-python\s*=\s*"([^"]+)"', _pyproject(), flags=re.M)
    assert INFO["install"]["runtime"] == "python" + m.group(1)
    assert _llms(r"needs Python (\d+\.\d+)\+") == m.group(1).lstrip(">=")


def test_listed_dependencies_match_pyproject_floors():
    block = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", _pyproject(), flags=re.M | re.S).group(1)
    floors = {}
    for spec in re.findall(r'"([^"]+)"', block):
        m = re.match(r"([A-Za-z0-9_.-]+).*?>=([0-9.]+)", spec)
        if m:
            floors[m.group(1).lower()] = m.group(2)
    for dep in INFO["install"]["dependencies"]:
        name, floor = dep.split(">=")
        assert floors.get(name.lower()) == floor, (
            f"robot.info install.dependencies says {dep}; pyproject.toml says "
            f"{name}>={floors.get(name.lower())}"
        )


# ── ports, paths, defaults ────────────────────────────────────────────────

def test_default_port_everywhere():
    from agentb.config import ServerConfig, _apply_defaults, AgentBConfig
    port = ServerConfig().port
    assert _apply_defaults(AgentBConfig()).server.port == port
    assert INFO["exposes"]["rest_api"]["default_port"] == port, "robot.info rest_api.default_port"
    assert INSTALL["service"]["port"] == port, "robot.install service.port"
    assert int(_llms(r"default port (\d+)")) == port, "llms.txt 'default port'"


def test_init_wizard_paths_and_defaults():
    """What robot.info and llms.txt say a no-config / wizard install uses."""
    from agentb import cli
    from agentb.config import AgentBConfig, DEFAULT_CONFIG_PATHS, _apply_defaults
    home = str(Path.home())
    tilde = lambda p: str(p).replace(home, "~", 1)

    wiz = PATHS["init_wizard"]
    assert tilde(cli.CONFIG_FILE) == wiz["config"]
    assert Path(wiz["config"].replace("~", home, 1)) in DEFAULT_CONFIG_PATHS, (
        "the wizard's config file is not a place load_config looks"
    )
    cfg = _apply_defaults(AgentBConfig())
    assert tilde(cli.DATA_DIR) == tilde(cfg.data_dir) == wiz["data_dir"]
    assert "`%s`" % wiz["data_dir"] in LLMS, "llms.txt names a different default data dir"

    r, e = cfg.reasoning.primary, cfg.embedding.primary
    assert wiz["reasoning_default"].startswith(f"{r.provider}, {r.model}, {r.api_base}")
    assert wiz["embedding_default"] == f"{e.provider}, {e.model}"
    assert f"default embedding model `{e.model}`" in LLMS

    # The all-local block robot.info and robot.install hand out is the same
    # one the server falls back to with no config.
    block = PATHS["all_local_reasoning_block"]
    assert (block["provider"], block["model"], block["api_base"]) == (r.provider, r.model, r.api_base)
    assert block["api_key_env"] == ""
    shown = re.search(r'^\s*//\s*"reasoning":\s*(\{.*?),\s*$\n\s*//\s*(.*?\}),?\s*$',
                      INSTALL_RAW, flags=re.M)
    assert shown, "robot.install no longer shows the all-local reasoning block in a comment"
    assert json.loads(shown.group(1) + ", " + shown.group(2)) == block, (
        "the all-local block in robot.install's comment differs from robot.info's"
    )


def _installer_default(block: str, key: str) -> str:
    src = (ROOT / "robot-install.sh").read_text(encoding="utf-8")
    m = re.search(rf"\b{block}\.get\('{key}', '?([^')]*)'?\)", src)
    assert m, f"robot-install.sh no longer reads {block}.get('{key}', <default>)"
    return m.group(1)


@pytest.mark.parametrize("section,key,var", [
    ("service", "host", "svc"), ("service", "port", "svc"), ("service", "data_dir", "svc"),
    ("reasoning", "provider", "rs"), ("reasoning", "model", "rs"), ("reasoning", "api_base", "rs"),
    ("embedding", "provider", "emb"), ("embedding", "model", "emb"), ("embedding", "api_base", "emb"),
])
def test_robot_install_shows_the_installers_defaults(section, key, var):
    """robot.install: 'A block you leave out takes the defaults shown here'."""
    assert str(INSTALL[section][key]) == _installer_default(var, key), (
        f"robot.install {section}.{key} = {INSTALL[section][key]!r}, but robot-install.sh "
        f"defaults it to {_installer_default(var, key)!r}"
    )


def test_robot_install_paths_and_key():
    src = (ROOT / "robot-install.sh").read_text(encoding="utf-8")
    ri = PATHS["robot_install"]
    assert 'CONFIG_DIR="${MNEMO_INSTALL_CONFIG_DIR:-${HOME}/.config/mnemo-cortex}"' in src
    assert 'CONFIG_FILE="$CONFIG_DIR/mnemo-cortex.yaml"' in src
    assert ri["config_written"].startswith("~/.config/mnemo-cortex/mnemo-cortex.yaml")
    assert "MNEMO_INSTALL_CONFIG_DIR" in ri["config_written"]
    key = INSTALL["reasoning"]["api_key_env"]
    assert re.search(rf"key_env\(rs, '{key}'\)", src), "installer's fallback key variable"
    assert f"`{key}`" in LLMS
    data_dir = INSTALL["service"]["data_dir"]
    assert f"data_dir {data_dir}" in ri["manifest_defaults"]
    assert f"keeps data in `{data_dir}`" in LLMS


# ── memory model ──────────────────────────────────────────────────────────

def test_embedding_size():
    from agentb.vec import EMBED_DIM
    assert f"{EMBED_DIM}-number vectors" in INSTALL_RAW, "robot.install embedding comment"


def _claimed_lists(text: str):
    srcs = re.findall(r"source[^(]{0,20}\(([a-z_ |/]+)\)", text)
    cats = re.findall(r"category[^(]{0,20}\(([a-z_, ]+)\)", text)
    split = lambda s: {w for w in re.split(r"[\s|/,]+", s) if w}
    return [split(s) for s in srcs], [split(c) for c in cats]


@pytest.mark.parametrize("where,text", [
    ("robot.info", json.dumps(INFO, ensure_ascii=False)),
    ("llms.txt", LLMS),
], ids=["robot.info", "llms.txt"])
def test_sources_and_categories(where, text):
    from agentb.provenance import VALID_CATEGORIES, VALID_SOURCES
    srcs, cats = _claimed_lists(text)
    assert srcs, f"{where} no longer lists the sources; update this test with the guide"
    for s in srcs:
        assert s == VALID_SOURCES, f"{where} lists sources {sorted(s)}, code has {sorted(VALID_SOURCES)}"
    for c in cats:
        assert c == VALID_CATEGORIES, (
            f"{where} lists categories {sorted(c)}, code has {sorted(VALID_CATEGORIES)}"
        )


def test_decay_claims():
    from agentb.provenance import DECAY_THRESHOLDS, VALID_CATEGORIES
    d = INSTALL["decay"]
    assert d["topology_warn_days"] == DECAY_THRESHOLDS["topology"]["warn"]
    assert d["topology_stale_days"] == DECAY_THRESHOLDS["topology"]["stale"]
    assert d["current_state_warn_days"] == DECAY_THRESHOLDS["current_state"]["warn"]
    assert d["relationship_warn_days"] == DECAY_THRESHOLDS["relationship"]["warn"]
    assert d["session_log_warn_days"] == DECAY_THRESHOLDS["session_log"]["warn"]
    warn, stale = DECAY_THRESHOLDS["topology"]["warn"], DECAY_THRESHOLDS["topology"]["stale"]
    assert f"warn at {warn} days, stale at {stale}" in LLMS
    perpetual = VALID_CATEGORIES - set(DECAY_THRESHOLDS)
    for claim in ("doctrine, incident, identity and decision never decay",
                  "Doctrine, incident, identity, and decision are perpetual"):
        where = LLMS if "never decay" in claim else json.dumps(INFO, ensure_ascii=False)
        assert claim in where, f"guide no longer says {claim!r}; update this test with the guide"
    assert {"doctrine", "incident", "identity", "decision"} <= perpetual


# ── interfaces ────────────────────────────────────────────────────────────

def _bridge_tools() -> set[str]:
    src = (ROOT / "integrations/mcp-bridge/server.js").read_text(encoding="utf-8")
    tools = set(re.findall(r"registerTool\(\s*[\"']([a-z_]+)[\"']", src))
    assert len(tools) > 10, "could not read the bridge's tool registrations"
    return tools


def test_robot_info_lists_exactly_the_bridge_tools():
    listed = {t["name"] for t in INFO["exposes"]["mcp_tools"]}
    real = _bridge_tools()
    assert not listed - real, f"robot.info lists tools the bridge does not have: {sorted(listed - real)}"
    assert not real - listed, f"robot.info is missing bridge tools: {sorted(real - listed)}"


def test_tools_named_in_llms_txt_exist():
    named = set(re.findall(r"`((?:mnemo|agent|session|read|write|list|wiki|passport)_[a-z_]+)`", LLMS))
    assert named, "llms.txt names no tools; update this test with the guide"
    assert named <= _bridge_tools(), f"llms.txt names tools the bridge does not have: {sorted(named - _bridge_tools())}"


def test_rest_endpoints_exist(tmp_path):
    from unittest.mock import patch
    from agentb.config import (AgentBConfig, CacheConfig, ClassificationConfig, DEFAULT_PERSONAS,
                               ProviderConfig, ResilientProviderConfig, ServerConfig)
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50099),
        data_dir=str(tmp_path), classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding"), patch("agentb.server.create_resilient_reasoning"):
        from agentb.server import create_app
        app = create_app(cfg)
    norm = lambda p: re.sub(r"\{[^}]+\}", "{}", p)
    real = set()
    for r in app.routes:  # include_in_schema=False routes (/app) are here too
        for m in getattr(r, "methods", None) or ():
            real.add((m, norm(r.path)))
    for op, ops in app.openapi()["paths"].items():  # routes from included routers
        for m in ops:
            real.add((m.upper(), norm(op)))
    claimed = {(e["method"], norm(e["path"])) for e in INFO["exposes"]["rest_api"]["endpoints"]}
    assert not claimed - real, f"robot.info lists endpoints the server does not serve: {sorted(claimed - real)}"


def test_cli_commands_and_flags_exist():
    from agentb.cli import main
    for entry in INFO["exposes"]["cli"]:
        words = entry["command"].split()
        assert words[0] == "mnemo-cortex"
        cmd = main.commands.get(words[1])
        assert cmd, f"robot.info lists 'mnemo-cortex {words[1]}', which the CLI does not have"
        opts = {o for p in cmd.params for o in p.opts}
        for flag in re.findall(r"\[(--[a-z-]+)", entry["command"]):
            assert flag in opts, f"robot.info: 'mnemo-cortex {words[1]}' has no {flag}"


def test_wizard_providers_match_llms_txt():
    src = (ROOT / "agentb/cli.py").read_text(encoding="utf-8")
    choices = re.search(r'"Provider",\s*choices=\[([^\]]+)\]', src)
    assert choices, "the init wizard's provider menu moved; update this test"
    menu = set(re.findall(r'"([a-z]+)"', choices.group(1)))
    named = _llms(r"setup wizard also takes ([A-Za-z, ]+?) keys")
    said = {w.lower() for w in re.split(r",\s*|\s+or\s+", named) if w}
    assert said == menu - {"ollama"}, f"llms.txt names {sorted(said)}; the wizard offers {sorted(menu)}"


def test_common_questions_count():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    n = int(re.search(r"(\d+) common-question/answer pairs", readme).group(1))
    assert n == len(INFO["common_questions"]), (
        f"README says {n} common questions; robot.info has {len(INFO['common_questions'])}"
    )
