"""
Mnemo Cortex Doctor — Comprehensive Health Diagnostic
=====================================================
Integrates into the existing CLI as: mnemo-cortex doctor

Add to cli.py by importing and registering:
    from agentb.doctor import doctor
    main.add_command(doctor)

Can also be run standalone:
    python -m agentb.doctor http://localhost:50001
"""

import os
import sys
import time
import json
import socket
import subprocess
from pathlib import Path
from typing import Optional

import click
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

BANNER = """[bold yellow]
  🧠 Mnemo Cortex Doctor
[/bold yellow][dim]  Comprehensive Health Diagnostic[/dim]
"""

# ─────────────────────────────────────────────
#  Helper: resolve server URL
# ─────────────────────────────────────────────

def resolve_url(url: Optional[str] = None) -> str:
    """Resolve the Mnemo Cortex server URL from args, env, or config."""
    if url:
        return url.rstrip("/")

    # Check environment
    env_url = os.environ.get("MNEMO_URL")
    if env_url:
        return env_url.rstrip("/")

    # Check config file
    config_file = Path.home() / ".config" / "agentb" / "agentb.yaml"
    if config_file.exists():
        try:
            import yaml
            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
            server = cfg.get("server", {})
            host = server.get("host", "0.0.0.0")
            port = server.get("port", 50001)
            # If host is 0.0.0.0, use localhost for the check
            if host == "0.0.0.0":
                host = "localhost"
            return f"http://{host}:{port}"
        except Exception:
            pass

    return "http://localhost:50001"


# ─────────────────────────────────────────────
#  Individual checks
# ─────────────────────────────────────────────

def check_reachability(url: str, timeout: int = 10) -> dict:
    """Check 1: Can we reach the server at all?"""
    try:
        resp = httpx.get(f"{url}/health", timeout=timeout)
        resp.raise_for_status()
        return {"status": "PASS", "data": resp.json(), "latency_ms": resp.elapsed.total_seconds() * 1000}
    except httpx.ConnectError:
        return {"status": "FAIL", "error": f"Connection refused at {url}"}
    except httpx.ConnectTimeout:
        return {"status": "FAIL", "error": f"Connection timed out after {timeout}s"}
    except Exception as e:
        return {"status": "FAIL", "error": str(e)}


def check_api_status(health: dict) -> dict:
    """Check 2: Is the server reporting OK?"""
    status = health.get("status", "unknown")
    version = health.get("version", "unknown")
    if status == "ok":
        return {"status": "PASS", "version": version, "detail": f"v{version}"}
    return {"status": "FAIL", "detail": f"Status is '{status}' (expected 'ok')"}


def check_reasoning(health: dict) -> dict:
    """Check 3: Is the reasoning model loaded and healthy?"""
    reasoning = health.get("reasoning", {})
    active = reasoning.get("active", "unknown")
    healthy = reasoning.get("healthy", False)
    if healthy:
        return {"status": "PASS", "model": active, "detail": f"{active} — healthy"}
    return {"status": "FAIL", "model": active, "detail": f"{active} — NOT healthy"}


def check_embedding(health: dict) -> dict:
    """Check 4: Is the embedding model loaded and healthy?"""
    embedding = health.get("embedding", {})
    active = embedding.get("active", "unknown")
    healthy = embedding.get("healthy", False)
    if healthy:
        return {"status": "PASS", "model": active, "detail": f"{active} — healthy"}
    return {"status": "FAIL", "model": active, "detail": f"{active} — NOT healthy"}


def check_agents(health: dict) -> dict:
    """Check 5: Which agents are registered?"""
    agents = health.get("agents_configured", [])
    if agents:
        return {"status": "PASS", "agents": agents, "detail": f"{len(agents)} agents: {', '.join(agents)}"}
    return {"status": "WARN", "agents": [], "detail": "No agents configured (using 'default' only)"}


def check_sessions(health: dict) -> dict:
    """Check 6: Session statistics."""
    sessions = health.get("sessions", {})
    hot = sessions.get("hot", 0)
    warm = sessions.get("warm", 0)
    cold = sessions.get("cold", 0)
    return {
        "status": "PASS",
        "hot": hot, "warm": warm, "cold": cold,
        "detail": f"{hot} hot / {warm} warm / {cold} cold",
    }


def check_context_query(url: str, agent_id: str, timeout: int = 15) -> dict:
    """Check 7: Can we actually run a semantic search? (not just /health)"""
    try:
        start = time.time()
        resp = httpx.post(
            f"{url}/context",
            json={"prompt": "doctor diagnostic test", "agent_id": agent_id, "max_results": 1},
            timeout=timeout,
        )
        latency = (time.time() - start) * 1000
        resp.raise_for_status()
        data = resp.json()
        total = data.get("total_found", 0)
        provider = data.get("provider_used", "unknown")
        return {
            "status": "PASS",
            "latency_ms": round(latency, 1),
            "results_found": total,
            "provider": provider,
            "detail": f"Responded in {round(latency)}ms — {total} results — via {provider}",
        }
    except httpx.ReadTimeout:
        return {"status": "FAIL", "detail": f"Context query timed out after {timeout}s — model may be cold-loading"}
    except Exception as e:
        return {"status": "FAIL", "detail": f"Context query failed: {e}"}


def check_ingest(url: str, timeout: int = 10) -> dict:
    """Check 8: Can we write to the live wire?"""
    try:
        resp = httpx.post(
            f"{url}/ingest",
            json={
                "prompt": "Doctor diagnostic test",
                "response": f"Health check at {time.strftime('%Y-%m-%d %H:%M:%S')}",
                "agent_id": "_doctor_test",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "PASS",
            "session_id": data.get("session_id", "unknown"),
            "detail": f"Ingest accepted — session {data.get('session_id', '?')}, entry #{data.get('entry_number', '?')}",
        }
    except Exception as e:
        return {"status": "FAIL", "detail": f"Ingest failed: {e}"}


def check_port_conflict(url: str) -> dict:
    """Check 9: Is a zombie server intercepting requests?"""
    # Only relevant if the target URL is NOT localhost
    is_remote = not any(h in url for h in ["localhost", "127.0.0.1", "0.0.0.0"])

    if not is_remote:
        return {"status": "PASS", "detail": "Target is localhost — no conflict possible"}

    # Check if port 50001 is listening locally
    try:
        # Try to parse the port from the URL
        port = 50001
        if ":" in url.split("//")[-1]:
            port_str = url.split(":")[-1].split("/")[0]
            port = int(port_str)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(("127.0.0.1", port))
        sock.close()

        if result == 0:
            return {
                "status": "FAIL",
                "detail": f"Port {port} is ALSO listening locally — zombie server may intercept requests meant for {url}",
            }
        return {"status": "PASS", "detail": f"Port {port} clear locally — all requests go to remote server"}
    except Exception as e:
        return {"status": "INFO", "detail": f"Could not check local port: {e}"}


def check_daemons() -> dict:
    """Check 10: Are the watcher and refresher daemons running?"""
    results = {}

    for service in ["mnemo-watcher", "mnemo-refresh"]:
        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", service],
                capture_output=True, text=True, timeout=5,
            )
            if r.stdout.strip() == "active":
                results[service] = "running"
            else:
                # Check if it's at least installed
                r2 = subprocess.run(
                    ["systemctl", "--user", "is-enabled", service],
                    capture_output=True, text=True, timeout=5,
                )
                if r2.returncode == 0:
                    results[service] = "stopped"
                else:
                    results[service] = "not installed"
        except FileNotFoundError:
            results[service] = "systemctl not available"
        except Exception:
            results[service] = "unknown"

    return results


# ─────────────────────────────────────────────
#  Main doctor command
# ─────────────────────────────────────────────

@click.command()
@click.argument("url", required=False, default=None)
@click.option("--json-output", "--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
@click.option("--timeout", "-t", default=10, help="Request timeout in seconds")
@click.option("--skip-ingest", is_flag=True, help="Skip the ingest write test")
def doctor(url, as_json, timeout, skip_ingest):
    """Full diagnostic health check.

    \b
    Runs 10 checks against a Mnemo Cortex server:
      1. Server reachability
      2. API status & version
      3. Reasoning model health
      4. Embedding model health
      5. Registered agents
      6. Session statistics
      7. Live context query (real semantic search)
      8. Live ingest test (real write)
      9. Port conflict detection (zombie servers)
     10. Watcher/refresher daemon status

    \b
    Usage:
      mnemo-cortex doctor                          # auto-detect URL
      mnemo-cortex doctor http://localhost:50001   # explicit URL
      mnemo-cortex doctor --json                   # machine-readable
      mnemo-cortex doctor --skip-ingest            # skip write test
    """
    target = resolve_url(url)
    failures = 0
    warnings = 0
    results = {}

    if not as_json:
        console.print(BANNER)
        console.print(f"  Target: [bold]{target}[/]")
        console.print()

    # ── Check 1: Reachability ──
    if not as_json:
        console.print("[bold]1. Server Reachability[/]")

    reach = check_reachability(target, timeout)
    results["reachability"] = reach

    if reach["status"] != "PASS":
        failures += 1
        if not as_json:
            console.print(f"  [red]❌ {reach['error']}[/]")

            # Bonus: check for zombie
            zombie = check_port_conflict(target)
            if zombie["status"] == "FAIL":
                console.print(f"  [yellow]⚠️  {zombie['detail']}[/]")

            console.print()
            console.print("  [red]Server unreachable. Cannot continue diagnostics.[/]")
            console.print("  Check that mnemo-cortex is running and the URL is correct.")
        else:
            results["port_conflict"] = check_port_conflict(target)
            console.print_json(json.dumps({"status": "UNREACHABLE", "url": target, "checks": results}))
        sys.exit(2)

    health = reach["data"]
    if not as_json:
        console.print(f"  [green]✅[/] Responding in {reach['latency_ms']:.0f}ms")
        console.print()

    # ── Check 2: API Status ──
    if not as_json:
        console.print("[bold]2. API Status[/]")
    api = check_api_status(health)
    results["api_status"] = api
    if api["status"] != "PASS":
        failures += 1
    if not as_json:
        icon = "✅" if api["status"] == "PASS" else "❌"
        color = "green" if api["status"] == "PASS" else "red"
        console.print(f"  [{color}]{icon}[/] Status: ok — Version: {api.get('version', '?')}")
        console.print()

    # ── Check 3: Reasoning ──
    if not as_json:
        console.print("[bold]3. Reasoning Model[/]")
    reasoning = check_reasoning(health)
    results["reasoning"] = reasoning
    if reasoning["status"] != "PASS":
        failures += 1
    if not as_json:
        icon = "✅" if reasoning["status"] == "PASS" else "❌"
        color = "green" if reasoning["status"] == "PASS" else "red"
        console.print(f"  [{color}]{icon}[/] {reasoning['detail']}")
        console.print()

    # ── Check 4: Embedding ──
    if not as_json:
        console.print("[bold]4. Embedding Model[/]")
    embedding = check_embedding(health)
    results["embedding"] = embedding
    if embedding["status"] != "PASS":
        failures += 1
    if not as_json:
        icon = "✅" if embedding["status"] == "PASS" else "❌"
        color = "green" if embedding["status"] == "PASS" else "red"
        console.print(f"  [{color}]{icon}[/] {embedding['detail']}")
        console.print()

    # ── Check 5: Agents ──
    if not as_json:
        console.print("[bold]5. Registered Agents[/]")
    agents = check_agents(health)
    results["agents"] = agents
    if agents["status"] == "WARN":
        warnings += 1
    if not as_json:
        icon = "✅" if agents["status"] == "PASS" else "⚠️ "
        console.print(f"  [green]{icon}[/] {agents['detail']}")
        console.print()

    # ── Check 6: Sessions ──
    if not as_json:
        console.print("[bold]6. Session Statistics[/]")
    sessions = check_sessions(health)
    results["sessions"] = sessions
    if not as_json:
        console.print(f"  [green]✅[/] {sessions['detail']}")
        console.print()

    # ── Check 7: Live Context Query ──
    if not as_json:
        console.print("[bold]7. Live Context Query[/]")
    first_agent = agents.get("agents", ["default"])[0] if agents.get("agents") else "default"
    context = check_context_query(target, first_agent, timeout + 5)
    results["context_query"] = context
    if context["status"] != "PASS":
        failures += 1
    if not as_json:
        icon = "✅" if context["status"] == "PASS" else "❌"
        color = "green" if context["status"] == "PASS" else "red"
        console.print(f"  [{color}]{icon}[/] {context['detail']}")
        if context.get("latency_ms", 0) > 5000:
            console.print(f"  [yellow]⚠️  High latency — model may be cold-loading[/]")
            warnings += 1
        console.print()

    # ── Check 8: Live Ingest ──
    if not skip_ingest:
        if not as_json:
            console.print("[bold]8. Live Ingest Test[/]")
        ingest = check_ingest(target, timeout)
        results["ingest"] = ingest
        if ingest["status"] != "PASS":
            failures += 1
        if not as_json:
            icon = "✅" if ingest["status"] == "PASS" else "❌"
            color = "green" if ingest["status"] == "PASS" else "red"
            console.print(f"  [{color}]{icon}[/] {ingest['detail']}")
            console.print()
    else:
        results["ingest"] = {"status": "SKIPPED"}
        if not as_json:
            console.print("[bold]8. Live Ingest Test[/]")
            console.print(f"  [blue]ℹ[/]  Skipped (--skip-ingest)")
            console.print()

    # ── Check 9: Port Conflict ──
    if not as_json:
        console.print("[bold]9. Port Conflict Check[/]")
    port_check = check_port_conflict(target)
    results["port_conflict"] = port_check
    if port_check["status"] == "FAIL":
        failures += 1
    if not as_json:
        if port_check["status"] == "PASS":
            console.print(f"  [green]✅[/] {port_check['detail']}")
        elif port_check["status"] == "FAIL":
            console.print(f"  [red]❌ {port_check['detail']}[/]")
        else:
            console.print(f"  [blue]ℹ[/]  {port_check['detail']}")
        console.print()

    # ── Check 10: Daemons ──
    if not as_json:
        console.print("[bold]10. Daemon Status[/]")
    daemons = check_daemons()
    results["daemons"] = daemons
    for svc, state in daemons.items():
        if state == "running":
            if not as_json:
                console.print(f"  [green]✅[/] {svc}: running")
        elif state == "stopped":
            warnings += 1
            if not as_json:
                console.print(f"  [yellow]⚠️ [/] {svc}: installed but not running")
        elif state == "not installed":
            if not as_json:
                console.print(f"  [blue]ℹ[/]  {svc}: not installed on this host")
        else:
            if not as_json:
                console.print(f"  [blue]ℹ[/]  {svc}: {state}")

    # ── Summary ──
    if as_json:
        overall = "HEALTHY" if failures == 0 else "UNHEALTHY"
        output = {
            "status": overall,
            "url": target,
            "version": api.get("version", "?"),
            "failures": failures,
            "warnings": warnings,
            "checks": results,
        }
        console.print_json(json.dumps(output))
        sys.exit(0 if failures == 0 else 1)

    console.print()

    # Build summary table
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")
    table.add_row("Server", target)
    table.add_row("Version", api.get("version", "?"))
    table.add_row("Agents", ", ".join(agents.get("agents", ["default"])))
    table.add_row("Sessions", sessions["detail"])
    if context.get("latency_ms"):
        table.add_row("Query latency", f"{context['latency_ms']}ms")

    if failures == 0 and warnings == 0:
        console.print(Panel(
            "[bold green]🟢 ALL CHECKS PASSED[/]\n\nMnemo Cortex is fully healthy.",
            title="⚡ Doctor Summary",
            border_style="green",
        ))
    elif failures == 0:
        console.print(Panel(
            f"[bold yellow]🟡 PASSED WITH {warnings} WARNING(S)[/]\n\nReview warnings above.",
            title="⚡ Doctor Summary",
            border_style="yellow",
        ))
    else:
        console.print(Panel(
            f"[bold red]🔴 {failures} CHECK(S) FAILED[/]"
            + (f"\nPlus {warnings} warning(s)." if warnings else ""),
            title="⚡ Doctor Summary",
            border_style="red",
        ))

    console.print(table)
    console.print()

    sys.exit(0 if failures == 0 else 1)


# Allow standalone execution
if __name__ == "__main__":
    doctor()
