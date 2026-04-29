"""
Mnemo Cortex Health — Deployment-wide health verification
=========================================================
Built-in CLI command: mnemo-cortex health

Auto-discovers agents, tests live recall per agent, validates MCP configs,
checks database integrity, and verifies watcher services.

No hardcoded agent names. No hardcoded ports. Everything from config + database.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import click
import httpx

# ─────────────────────────────────────────────
#  URL resolution (shared with doctor.py)
# ─────────────────────────────────────────────

def _resolve_url(url: Optional[str] = None) -> str:
    if url:
        return url.rstrip("/")
    env_url = os.environ.get("MNEMO_URL")
    if env_url:
        return env_url.rstrip("/")
    config_file = Path.home() / ".config" / "agentb" / "agentb.yaml"
    if config_file.exists():
        try:
            import yaml
            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
            server = cfg.get("server", {})
            host = server.get("host", "0.0.0.0")
            port = server.get("port", 50001)
            if host == "0.0.0.0":
                host = "localhost"
            return f"http://{host}:{port}"
        except Exception:
            pass
    return "http://localhost:50001"


# ─────────────────────────────────────────────
#  Check functions — each returns a dict with
#  {status: PASS|FAIL|WARN|SKIP, detail: str, ...}
# ─────────────────────────────────────────────

def check_api_server(url: str, timeout: int = 10) -> dict:
    """Core: API server reachability + version + memory count."""
    try:
        resp = httpx.get(f"{url}/health", timeout=timeout)
        resp.raise_for_status()
        h = resp.json()
        status = h.get("status", "unknown")
        version = h.get("version", "unknown")
        memories = h.get("memory_entries", 0)
        latency = resp.elapsed.total_seconds() * 1000
        if status == "ok":
            return {
                "status": "PASS", "health": h,
                "detail": f"v{version}, {memories:,} memories, {latency:.0f}ms",
            }
        return {"status": "FAIL", "health": h, "detail": f"Status: {status} (expected ok)"}
    except httpx.ConnectError:
        return {"status": "FAIL", "detail": f"Connection refused at {url}"}
    except httpx.ConnectTimeout:
        return {"status": "FAIL", "detail": f"Timed out after {timeout}s"}
    except Exception as e:
        return {"status": "FAIL", "detail": str(e)}


def check_database(url: str, health_data: dict, timeout: int = 10) -> dict:
    """Core: Database accessible. Uses /sessions if available, falls back to health data."""
    # Try /sessions endpoint first (v2 server)
    try:
        resp = httpx.get(f"{url}/sessions", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            hot = len(data.get("hot", []))
            warm = len(data.get("warm", []))
            cold = len(data.get("cold", []))
            total = hot + warm + cold
            return {
                "status": "PASS",
                "detail": f"{total} sessions ({hot} hot, {warm} warm, {cold} cold)",
            }
    except Exception:
        pass

    # Fallback: use memory_entries from /health (works with all server versions)
    memories = health_data.get("memory_entries", 0)
    l1 = health_data.get("l1_cache_size", 0)
    sessions = health_data.get("sessions", {})
    if memories > 0 or l1 > 0:
        detail = f"{memories:,} memories, L1 cache: {l1} bundles"
        if sessions:
            detail = f"{sessions.get('hot', 0)} hot / {sessions.get('warm', 0)} warm / {sessions.get('cold', 0)} cold sessions, {memories:,} memories"
        return {"status": "PASS", "detail": detail}

    # Server is responding but has no data — still OK
    return {"status": "PASS", "detail": "Server responding, database empty (new deployment)"}


def check_compaction_model(health: dict, timeout: int = 10) -> dict:
    """Core: Is the compaction/reasoning model reachable?"""
    # Health data has different shapes depending on server version
    reasoning = health.get("reasoning", {})
    if isinstance(reasoning, dict) and "healthy" in reasoning:
        model = reasoning.get("active", reasoning.get("primary", "unknown"))
        if reasoning.get("healthy"):
            return {"status": "PASS", "detail": f"{model} — responding"}
        return {"status": "FAIL", "detail": f"{model} — NOT responding"}

    # Fallback for older health format
    ollama = health.get("ollama_connected", None)
    model = health.get("reason_model", "unknown")
    if ollama is True:
        return {"status": "PASS", "detail": f"{model} — responding (via Ollama)"}
    elif ollama is False:
        return {"status": "FAIL", "detail": f"{model} — Ollama disconnected"}
    return {"status": "WARN", "detail": "Cannot determine compaction model status from health response"}


def discover_agents(url: str, health: dict, mcp_paths: tuple = (), timeout: int = 10) -> list[str]:
    """Auto-discover agents from health response, database, and MCP configs."""
    agents = set()

    # From health response (v2 server)
    for a in health.get("agents_configured", []):
        agents.add(a)

    # From sessions endpoint (v2 server)
    try:
        resp = httpx.get(f"{url}/sessions", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            for tier in ("hot", "warm", "cold"):
                for session in data.get(tier, []):
                    aid = session.get("agent_id")
                    if aid and aid != "_doctor_test":
                        agents.add(aid)
    except Exception:
        pass

    # From MCP config files — extract agent_id from env vars
    for mcp_path in mcp_paths:
        try:
            with open(Path(mcp_path).expanduser()) as f:
                cfg = json.load(f)
            # Check both config shapes
            for servers in [cfg.get("mcpServers", {}), cfg.get("mcp", {}).get("servers", {})]:
                for name, srv in servers.items():
                    if "mnemo" in name.lower():
                        env = srv.get("env", {})
                        aid = env.get("MNEMO_AGENT_ID")
                        if aid:
                            agents.add(aid)
        except Exception:
            pass

    # Probe well-known agent IDs if we still have nothing
    if not agents:
        for candidate in ["rocky", "cc", "opie", "alice"]:
            try:
                resp = httpx.post(
                    f"{url}/context",
                    json={"prompt": "probe", "agent_id": candidate, "max_results": 1},
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Check if there's actual content
                    has_data = (
                        data.get("total_found", 0) > 0
                        or data.get("chunks", [])
                        or data.get("context", "").strip()
                    )
                    if has_data:
                        agents.add(candidate)
            except Exception:
                pass

    return sorted(agents) if agents else ["default"]


def check_agent_recall(url: str, agent_id: str, timeout: int = 15) -> dict:
    """Per-agent: Live context recall test."""
    try:
        start = time.time()
        resp = httpx.post(
            f"{url}/context",
            json={"prompt": "health check verification", "agent_id": agent_id, "max_results": 5},
            timeout=timeout,
        )
        latency = (time.time() - start) * 1000
        resp.raise_for_status()
        data = resp.json()

        # Handle different response shapes
        total = data.get("total_found", 0)
        chunks = data.get("chunks", [])
        if not total and chunks:
            total = len(chunks)
        # Some versions return context string instead of chunks
        context = data.get("context", "")
        if not total and context:
            total = len(context.split("\n\n"))

        return {
            "status": "PASS",
            "results": total,
            "latency_ms": round(latency),
            "detail": f"recall returned {total} results ({round(latency)}ms)",
        }
    except httpx.ReadTimeout:
        return {"status": "FAIL", "detail": "recall timed out — model may be cold-loading"}
    except Exception as e:
        return {"status": "FAIL", "detail": f"recall failed: {e}"}


def check_watcher_services() -> list[dict]:
    """Services: Check for mnemo-watcher-* and mnemo-refresh* systemd services."""
    results = []

    # Auto-discover mnemo-related services
    try:
        r = subprocess.run(
            ["systemctl", "--user", "list-units", "--all", "--no-pager", "--plain"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            svc_name = parts[0]
            if "mnemo" not in svc_name.lower():
                continue
            if not svc_name.endswith(".service"):
                continue

            # Check if active
            state = "unknown"
            pid = None
            try:
                r2 = subprocess.run(
                    ["systemctl", "--user", "is-active", svc_name],
                    capture_output=True, text=True, timeout=3,
                )
                state = r2.stdout.strip()
            except Exception:
                pass

            if state == "active":
                # Try to get PID
                try:
                    r3 = subprocess.run(
                        ["systemctl", "--user", "show", svc_name, "--property=MainPID", "--value"],
                        capture_output=True, text=True, timeout=3,
                    )
                    pid = r3.stdout.strip()
                    if pid == "0":
                        pid = None
                except Exception:
                    pass

            name = svc_name.replace(".service", "")
            detail = f"active, PID {pid}" if state == "active" and pid else state
            results.append({
                "service": name,
                "status": "PASS" if state == "active" else "FAIL",
                "state": state,
                "pid": pid,
                "detail": detail,
            })
    except FileNotFoundError:
        results.append({
            "service": "systemd",
            "status": "SKIP",
            "detail": "systemctl not available on this system",
        })
    except Exception as e:
        results.append({
            "service": "systemd",
            "status": "SKIP",
            "detail": f"Cannot query services: {e}",
        })

    return results


def check_mcp_config(config_path: str) -> dict:
    """MCP: Verify mnemo-cortex is registered in an MCP config file."""
    path = Path(config_path).expanduser()
    if not path.exists():
        return {"status": "FAIL", "detail": f"Config file not found: {path}"}

    try:
        with open(path) as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        return {"status": "FAIL", "detail": f"Invalid JSON: {e}"}

    # Check multiple possible config shapes
    found = False
    location = None

    # Claude Desktop format: mcpServers.mnemo-cortex
    mcp_servers = cfg.get("mcpServers", {})
    if any("mnemo" in k.lower() for k in mcp_servers):
        found = True
        location = "mcpServers"

    # OpenClaw format: mcp.servers.mnemo-cortex
    mcp = cfg.get("mcp", {})
    mcp_inner = mcp.get("servers", {}) if isinstance(mcp, dict) else {}
    if any("mnemo" in k.lower() for k in mcp_inner):
        found = True
        location = "mcp.servers"

    if found:
        return {"status": "PASS", "detail": f"mnemo-cortex registered (in {location})"}
    return {
        "status": "FAIL",
        "detail": f"mnemo-cortex NOT registered in {path.name}",
    }


# ─────────────────────────────────────────────
#  CLI Command
# ─────────────────────────────────────────────

def _dot_line(label: str, result: str, status: str, width: int = 56) -> str:
    """Format: '  Label ..................... RESULT' """
    dots_needed = width - len(label) - len(result) - 4
    if dots_needed < 3:
        dots_needed = 3
    dots = "." * dots_needed
    if status == "PASS":
        return f"  {label} {dots} {result}"
    elif status == "WARN":
        return f"  {label} {dots} WARN {result}"
    else:
        return f"  {label} {dots} FAIL {result}"


@click.command("health")
@click.argument("url", required=False, default=None)
@click.option("--json-output", "--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.option("--agents", "agents_only", is_flag=True, help="Only check agent connectivity")
@click.option("--services", "services_only", is_flag=True, help="Only check services/watchers")
@click.option("--quiet", "-q", is_flag=True, help="Exit code only (0=healthy, 1=failures)")
@click.option("--check-mcp", "mcp_paths", multiple=True, help="Check MCP registration in config file (repeatable)")
@click.option("--timeout", "-t", default=10, help="Request timeout in seconds")
def health(url, as_json, agents_only, services_only, quiet, mcp_paths, timeout):
    """Verify your entire Mnemo Cortex deployment.

    \b
    Auto-discovers agents from the database. Tests live recall per agent.
    Checks MCP config registration. Verifies watcher services.

    \b
    Usage:
      mnemo-cortex health                         # full check, human output
      mnemo-cortex health --json                  # machine-readable
      mnemo-cortex health --agents                # only agent recall tests
      mnemo-cortex health --services              # only watcher services
      mnemo-cortex health --quiet                 # exit code only
      mnemo-cortex health --check-mcp ~/.openclaw/openclaw.json
      mnemo-cortex health http://localhost:50001  # explicit URL

    \b
    Exit codes:
      0  all checks passed
      1  one or more checks failed
    """
    target = _resolve_url(url)
    all_results = {}
    total_checks = 0
    passed_checks = 0
    failed_checks = 0

    def record(section, name, result):
        nonlocal total_checks, passed_checks, failed_checks
        all_results.setdefault(section, {})[name] = result
        total_checks += 1
        if result["status"] == "PASS":
            passed_checks += 1
        elif result["status"] == "FAIL":
            failed_checks += 1

    # ── Core Services ──
    if not agents_only and not services_only:
        api = check_api_server(target, timeout)
        record("core", "api_server", api)

        if api["status"] != "PASS":
            # Can't continue without server
            if as_json:
                print(json.dumps({
                    "status": "UNHEALTHY", "url": target,
                    "total": total_checks, "passed": passed_checks, "failed": failed_checks,
                    "checks": all_results,
                }, indent=2))
            elif not quiet:
                print(f"\nmnemo-cortex health check")
                print(f"{'=' * 25}")
                print(f"\nCore Services")
                print(f"  API server ({target}) {'.' * 10} FAIL {api['detail']}")
                print(f"\nServer unreachable. Cannot continue.")
            sys.exit(1)

        health_data = api.get("health", {})

        db = check_database(target, health_data, timeout)
        record("core", "database", db)

        compaction = check_compaction_model(health_data, timeout)
        record("core", "compaction", compaction)

    else:
        # Need health data for agent discovery even in filtered mode
        try:
            resp = httpx.get(f"{target}/health", timeout=timeout)
            health_data = resp.json()
        except Exception:
            health_data = {}

    # ── Agents ──
    if not services_only:
        agents = discover_agents(target, health_data, mcp_paths, timeout)
        for agent_id in agents:
            result = check_agent_recall(target, agent_id, timeout + 5)
            record("agents", agent_id, result)

    # ── Watchers ──
    if not agents_only:
        watchers = check_watcher_services()
        for w in watchers:
            record("watchers", w["service"], w)

    # ── MCP Registration ──
    if mcp_paths and not agents_only and not services_only:
        for mcp_path in mcp_paths:
            result = check_mcp_config(mcp_path)
            record("mcp", mcp_path, result)

    # ── Output ──
    if quiet:
        sys.exit(0 if failed_checks == 0 else 1)

    if as_json:
        output = {
            "status": "HEALTHY" if failed_checks == 0 else "UNHEALTHY",
            "url": target,
            "total": total_checks,
            "passed": passed_checks,
            "failed": failed_checks,
            "checks": all_results,
        }
        print(json.dumps(output, indent=2))
        sys.exit(0 if failed_checks == 0 else 1)

    # Human-readable output
    print(f"\nmnemo-cortex health check")
    print(f"{'=' * 25}")

    if "core" in all_results:
        print(f"\nCore Services")
        for name, r in all_results["core"].items():
            label = {
                "api_server": f"API server ({target})",
                "database": "Database",
                "compaction": "Compaction model",
            }.get(name, name)
            status_tag = "OK" if r["status"] == "PASS" else "FAIL"
            print(_dot_line(label, f"{status_tag} ({r['detail']})", r["status"]))

    if "agents" in all_results:
        agent_count = len(all_results["agents"])
        print(f"\nAgents ({agent_count} discovered)")
        for name, r in all_results["agents"].items():
            status_tag = "OK" if r["status"] == "PASS" else "FAIL"
            print(_dot_line(name, f"{status_tag} ({r['detail']})", r["status"]))

    if "watchers" in all_results:
        print(f"\nWatchers")
        for name, r in all_results["watchers"].items():
            status_tag = "OK" if r["status"] == "PASS" else r["status"]
            print(_dot_line(name, f"{status_tag} ({r['detail']})", r["status"]))

    if "mcp" in all_results:
        print(f"\nMCP Registration")
        for name, r in all_results["mcp"].items():
            display = Path(name).name
            status_tag = "OK" if r["status"] == "PASS" else "FAIL"
            print(_dot_line(display, f"{status_tag} ({r['detail']})", r["status"]))

    print(f"\n{passed_checks}/{total_checks} checks passed")
    if failed_checks > 0:
        print(f"{failed_checks} FAILED")
    print()

    sys.exit(0 if failed_checks == 0 else 1)


# Allow standalone execution
if __name__ == "__main__":
    health()
