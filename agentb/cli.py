"""
Mnemo Cortex CLI
================
The command-line interface for managing Mnemo Cortex.

  mnemo-cortex init      → Interactive setup wizard
  mnemo-cortex start     → Start the server
  mnemo-cortex stop      → Stop the server
  mnemo-cortex status    → Check health + session stats
  mnemo-cortex watch     → Auto-capture sessions TO Mnemo
  mnemo-cortex refresh   → Write Mnemo context to workspace (FROM Mnemo)
  mnemo-cortex recall    → Exact-match memory search (SQLite FTS5)
  mnemo-cortex logs      → Tail the server logs
  mnemo-cortex test      → Quick connectivity test

https://github.com/GuyMannDude/mnemo-cortex
"""

import os
import sys
import json
import signal
import subprocess
import time
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich import print as rprint

# Windows redirected-stdout safety (issue #3): under a Scheduled Task / service /
# pipe, stdout is not a real console and defaults to the system codepage (cp1252),
# which can't encode the banner's ⚡ (U+26A1) — the CLI crashed before the watcher
# ever started, silently killing auto-capture. Reconfigure the streams to
# utf-8/replace (belt), and keep rich off the Windows legacy-console path when
# stdout isn't a TTY (suspenders). Both are no-ops on a normal interactive terminal.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

_stdout_is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
console = Console(
    legacy_windows=False if not _stdout_is_tty else None,
    force_terminal=False if not _stdout_is_tty else None,
)

CONFIG_DIR = Path.home() / ".config" / "agentb"
CONFIG_FILE = CONFIG_DIR / "agentb.yaml"
DATA_DIR = Path.home() / ".agentb"
PID_FILE = DATA_DIR / "mnemo.pid"
LOG_FILE = DATA_DIR / "logs" / "mnemo.log"

BANNER = """[bold yellow]
  ⚡ Mnemo Cortex
[/bold yellow][dim]  "I remember everything so your agent doesn't have to."[/dim]
"""

# ─────────────────────────────────────────────
#  Main CLI Group
# ─────────────────────────────────────────────

from agentb import __version__
from agentb.doctor import doctor
from agentb.health import health

@click.group(invoke_without_command=True)
@click.pass_context
@click.version_option(version=__version__, prog_name="mnemo-cortex")
def main(ctx):
    """⚡ Mnemo Cortex — Drop-in memory superhero for AI agents."""
    if ctx.invoked_subcommand is None:
        console.print(BANNER)
        console.print("  Commands: [bold]init[/] · [bold]start[/] · [bold]stop[/] · [bold]health[/] · [bold]doctor[/] · [bold]watch[/] · [bold]refresh[/] · [bold]recall[/] · [bold]logs[/] · [bold]test[/]")
        console.print()
        if not CONFIG_FILE.exists():
            console.print("  [yellow]→ Run [bold]mnemo-cortex init[/bold] to get started![/]")
        else:
            console.print(f"  Config: {CONFIG_FILE}")
            console.print(f"  Data:   {DATA_DIR}")
        console.print()


# ─────────────────────────────────────────────
#  Init — Interactive Setup Wizard
# ─────────────────────────────────────────────

@main.command()
def init():
    """Interactive setup wizard. Gets you running in 2 minutes."""
    console.print(BANNER)
    console.print(Panel(
        "[bold]Welcome to Mnemo Cortex setup![/]\n\n"
        "This wizard will configure your memory coprocessor.\n"
        "You'll pick a reasoning provider, embedding provider, and we'll test the connection.",
        title="⚡ Setup Wizard",
        border_style="yellow",
    ))
    console.print()

    # Step 1: Reasoning provider
    console.print("[bold cyan]Step 1/4: Reasoning Provider[/]")
    console.print("This model handles preflight checks (validating your agent's responses).\n")

    reasoning_choice = Prompt.ask(
        "Provider",
        choices=["ollama", "openai", "anthropic", "openrouter", "google"],
        default="ollama",
    )

    reasoning_config = _configure_provider(reasoning_choice, "reasoning")

    # Step 2: Embedding provider
    console.print()
    console.print("[bold cyan]Step 2/4: Embedding Provider[/]")
    console.print("This model powers semantic memory search.\n")

    embedding_choice = Prompt.ask(
        "Provider",
        choices=["ollama", "openai", "huggingface", "google", "openrouter"],
        default="ollama" if reasoning_choice == "ollama" else "openai",
    )

    embedding_config = _configure_provider(embedding_choice, "embedding")

    # Step 3: Server settings
    console.print()
    console.print("[bold cyan]Step 3/4: Server Settings[/]")
    console.print(
        "[dim]The server defaults to loopback (127.0.0.1) — only this machine can reach it.\n"
        "Bind to 0.0.0.0 only if you need other machines on your network to connect,\n"
        "and set an auth token in that case.[/dim]"
    )
    host = Prompt.ask("Bind host", default="127.0.0.1")
    port = Prompt.ask("Port", default="50001")

    # Auth: blank is OK on loopback, REQUIRED off-loopback.
    is_loopback = host in ("127.0.0.1", "localhost", "::1")
    auth_prompt = (
        "API auth token (blank OK for loopback)" if is_loopback
        else "API auth token (REQUIRED — server is bound off-loopback)"
    )
    while True:
        auth = Prompt.ask(auth_prompt, default="")
        if is_loopback or auth.strip():
            break
        console.print(
            "[red]Auth token is required when host is not loopback. "
            "Set one, or change host back to 127.0.0.1.[/red]"
        )

    # CORS: locked-down list when loopback, broader (but not wildcard) off-loopback.
    if is_loopback:
        cors_list = '["http://127.0.0.1", "http://localhost"]'
    else:
        cors_list = f'["http://{host}", "http://localhost"]'

    # Step 4: Agent setup
    console.print()
    console.print("[bold cyan]Step 4/4: Agent Setup[/]")
    console.print("Name your agents so their memories stay isolated.\n")

    agents = {}
    while True:
        agent_name = Prompt.ask("Agent name (or 'done' to finish)", default="done")
        if agent_name.lower() == "done":
            break
        persona = Prompt.ask(
            f"  Persona for {agent_name}",
            choices=["default", "strict", "creative"],
            default="default",
        )
        agents[agent_name] = {"persona": persona}

    # Build config YAML
    yaml_lines = [
        "# Mnemo Cortex Configuration",
        f"# Generated by mnemo-cortex init on {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"data_dir: {DATA_DIR}",
        "log_level: info",
        "",
        "# Reasoning provider (preflight checks)",
        "reasoning:",
        f"  provider: {reasoning_config['provider']}",
        f"  model: {reasoning_config['model']}",
    ]
    if reasoning_config.get("api_key"):
        yaml_lines.append(f"  api_key: {reasoning_config['api_key']}")
    if reasoning_config.get("api_base"):
        yaml_lines.append(f"  api_base: {reasoning_config['api_base']}")
    yaml_lines.append(f"  timeout: {reasoning_config.get('timeout', 30)}")

    yaml_lines.extend([
        "",
        "# Embedding provider (semantic search)",
        "embedding:",
        f"  provider: {embedding_config['provider']}",
        f"  model: {embedding_config['model']}",
    ])
    if embedding_config.get("api_key"):
        yaml_lines.append(f"  api_key: {embedding_config['api_key']}")
    if embedding_config.get("api_base"):
        yaml_lines.append(f"  api_base: {embedding_config['api_base']}")

    yaml_lines.extend([
        "",
        "# Cache settings",
        "cache:",
        "  l3_similarity_threshold: 0.4",
        "",
        "# Server",
        "server:",
        f"  host: {host}",
        f"  port: {port}",
        f"  cors_origins: {cors_list}",
    ])
    if auth:
        yaml_lines.append(f"  auth_token: {auth}")

    if agents:
        yaml_lines.extend(["", "# Agents"])
        yaml_lines.append("agents:")
        for name, cfg in agents.items():
            yaml_lines.append(f"  {name}:")
            yaml_lines.append(f"    persona: {cfg['persona']}")

    yaml_content = "\n".join(yaml_lines) + "\n"

    # Write config
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)

    CONFIG_FILE.write_text(yaml_content, encoding="utf-8")

    console.print()
    console.print(Panel(
        f"[green]Config saved to:[/] {CONFIG_FILE}\n"
        f"[green]Data directory:[/]  {DATA_DIR}\n\n"
        "[bold]Next steps:[/]\n"
        "  [yellow]mnemo-cortex start[/]   — Start the server\n"
        "  [yellow]mnemo-cortex status[/]  — Check everything is working\n"
        "  [yellow]mnemo-cortex test[/]    — Quick connectivity test",
        title="⚡ Setup Complete!",
        border_style="green",
    ))


def _configure_provider(provider: str, role: str) -> dict:
    """Interactive configuration for a specific provider."""
    config = {"provider": provider}

    if provider == "ollama":
        config["api_base"] = Prompt.ask("  Ollama URL", default="http://localhost:11434")
        if role == "reasoning":
            config["model"] = Prompt.ask("  Model", default="qwen2.5:32b-instruct")
        else:
            config["model"] = Prompt.ask("  Model", default="nomic-embed-text")
        config["timeout"] = 30

    elif provider == "openai":
        config["api_key"] = Prompt.ask("  API key (or env var like ${OPENAI_API_KEY})")
        if role == "reasoning":
            config["model"] = Prompt.ask("  Model", default="gpt-4o-mini")
        else:
            config["model"] = Prompt.ask("  Model", default="text-embedding-3-small")

    elif provider == "anthropic":
        config["api_key"] = Prompt.ask("  API key (or env var like ${ANTHROPIC_API_KEY})")
        config["model"] = Prompt.ask("  Model", default="claude-sonnet-4-5-20250929")

    elif provider == "openrouter":
        config["api_key"] = Prompt.ask("  API key (or env var like ${OPENROUTER_API_KEY})")
        if role == "reasoning":
            config["model"] = Prompt.ask("  Model", default="nousresearch/hermes-3-llama-3.1-405b:free")
        else:
            config["model"] = Prompt.ask("  Model", default="thenlper/gte-base")

    elif provider == "google":
        config["api_key"] = Prompt.ask("  API key (or env var like ${GEMINI_API_KEY})")
        if role == "reasoning":
            config["model"] = Prompt.ask("  Model", default="gemini-2.5-flash")
        else:
            config["model"] = Prompt.ask("  Model", default="gemini-embedding-001")

    elif provider == "huggingface":
        config["model"] = Prompt.ask("  Model", default="sentence-transformers/all-MiniLM-L6-v2")
        api_base = Prompt.ask("  Local server URL (blank for HF API)", default="")
        if api_base:
            config["api_base"] = api_base
        else:
            config["api_key"] = Prompt.ask("  HuggingFace API token")

    return config


# ─────────────────────────────────────────────
#  Start
# ─────────────────────────────────────────────

@main.command()
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize)")
@click.option("--port", "-p", type=int, default=None, help="Override port")
def start(foreground, port):
    """Start the Mnemo Cortex server."""
    console.print(BANNER)

    if not CONFIG_FILE.exists():
        console.print("[red]No config found. Run [bold]mnemo-cortex init[/bold] first![/]")
        sys.exit(1)

    if _is_running():
        console.print("[yellow]Mnemo Cortex is already running.[/]")
        console.print(f"  PID: {_get_pid()}")
        console.print("  Run [bold]mnemo-cortex stop[/] first, or [bold]mnemo-cortex status[/] to check.")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["AGENTB_CONFIG"] = str(CONFIG_FILE)
    if port is not None:
        env["MNEMO_PORT"] = str(port)

    cmd = [sys.executable, "-m", "agentb.server"]

    if foreground:
        console.print("[yellow]Starting in foreground... (Ctrl+C to stop)[/]")
        console.print()
        try:
            subprocess.run(cmd, env=env)
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped.[/]")
    else:
        log_fh = open(LOG_FILE, "a")
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")

        # Wait a moment and check
        time.sleep(2)
        if proc.poll() is None:
            console.print(f"[green]⚡ Mnemo Cortex started![/]")
            console.print(f"  PID:  {proc.pid}")
            console.print(f"  Log:  {LOG_FILE}")
            console.print(f"  URL:  http://localhost:{port or 50001}")

            # Auto-capture gate: start watcher automatically if enabled
            if os.environ.get("MNEMO_AUTO_CAPTURE", "").lower() in ("true", "1", "yes"):
                if not _is_watcher_running():
                    console.print()
                    console.print("  [green]MNEMO_AUTO_CAPTURE=true → starting session watcher...[/]")
                    ctx = click.Context(watch)
                    ctx.invoke(watch, backfill=False, backfill_count=10, foreground=False)
                else:
                    console.print(f"  Watcher:  [green]already running[/]")

            console.print()
            console.print("  [dim]mnemo-cortex status  — check health[/]")
            console.print("  [dim]mnemo-cortex logs    — watch logs[/]")
            console.print("  [dim]mnemo-cortex stop    — stop server[/]")
        else:
            console.print("[red]Failed to start. Check logs:[/]")
            console.print(f"  {LOG_FILE}")
            # Show last few lines
            try:
                lines = LOG_FILE.read_text(encoding="utf-8").strip().split("\n")[-5:]
                for line in lines:
                    console.print(f"  [dim]{line}[/]")
            except Exception:
                pass


# ─────────────────────────────────────────────
#  Stop
# ─────────────────────────────────────────────

@main.command()
def stop():
    """Stop the Mnemo Cortex server."""
    if not _is_running():
        console.print("[yellow]Mnemo Cortex is not running.[/]")
        PID_FILE.unlink(missing_ok=True)
        return

    pid = _get_pid()
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Stopped Mnemo Cortex (PID {pid})[/]")
    except ProcessLookupError:
        console.print("[yellow]Process already gone.[/]")
    PID_FILE.unlink(missing_ok=True)


# ─────────────────────────────────────────────
#  Status
# ─────────────────────────────────────────────

@main.command()
def status():
    """Check Mnemo Cortex health and stats."""
    console.print(BANNER)

    # Process status
    if _is_running():
        console.print(f"  Process: [green]running[/] (PID {_get_pid()})")
    else:
        console.print("  Process: [red]stopped[/]")
        console.print("  Run [bold]mnemo-cortex start[/] to launch.")
        return

    # Health check
    try:
        import httpx
        resp = httpx.get("http://localhost:50001/health", timeout=5.0)
        health = resp.json()

        status_color = {"ok": "green", "degraded": "yellow", "down": "red"}.get(health["status"], "red")
        console.print(f"  Status:  [{status_color}]{health['status']}[/{status_color}]")
        console.print(f"  Version: {health.get('version', 'unknown')}")
        console.print()

        # Reasoning
        r = health.get("reasoning", {})
        r_status = "[green]healthy[/]" if r.get("healthy") else "[red]down[/]"
        r_active = r.get("active", r.get("primary", "unknown"))
        console.print(f"  Reasoning:  {r_status} → {r_active}")
        if r.get("failed_over"):
            console.print(f"              [yellow]⚠ Failed over from {r.get('primary', '?')}[/]")
            if r.get("primary_retry_in"):
                console.print(f"              [dim]Primary retry in {r['primary_retry_in']}[/]")

        # Embedding
        e = health.get("embedding", {})
        e_status = "[green]healthy[/]" if e.get("healthy") else "[red]down[/]"
        e_active = e.get("active", e.get("primary", "unknown"))
        console.print(f"  Embedding:  {e_status} → {e_active}")

        # Sessions
        sessions = health.get("sessions", {})
        if sessions:
            console.print()
            console.print(f"  Sessions:   [cyan]{sessions.get('hot', 0)}[/] hot · "
                         f"[blue]{sessions.get('warm', 0)}[/] warm · "
                         f"[dim]{sessions.get('cold', 0)}[/] cold")

        # Agents
        agents = health.get("agents_configured", [])
        if agents:
            console.print(f"  Agents:     {', '.join(agents)}")

        # Watcher
        if _is_watcher_running():
            console.print(f"  Watcher:    [green]running[/] (PID {_get_watcher_pid()}) — auto-capturing sessions")
        else:
            console.print(f"  Watcher:    [yellow]stopped[/] — run [bold]mnemo-cortex watch[/] to auto-capture")

        # Refresh daemon
        if _is_refresh_running():
            console.print(f"  Refresh:    [green]running[/] (PID {_get_refresh_pid()}) — writing context to workspace")
        else:
            console.print(f"  Refresh:    [yellow]stopped[/] — run [bold]mnemo-cortex refresh --watch[/] to auto-inject")

        console.print()

    except Exception as e:
        console.print(f"  [red]Cannot reach server: {e}[/]")
        console.print("  Is it running? Check [bold]mnemo-cortex logs[/]")


# ─────────────────────────────────────────────
#  Logs
# ─────────────────────────────────────────────

@main.command()
@click.option("--lines", "-n", default=50, help="Number of lines to show")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def logs(lines, follow):
    """View Mnemo Cortex server logs."""
    if not LOG_FILE.exists():
        console.print("[yellow]No logs yet. Start the server first.[/]")
        return

    if follow:
        try:
            subprocess.run(["tail", "-f", "-n", str(lines), str(LOG_FILE)])
        except KeyboardInterrupt:
            pass
    else:
        try:
            output = subprocess.run(
                ["tail", "-n", str(lines), str(LOG_FILE)],
                capture_output=True, text=True,
            )
            console.print(output.stdout)
        except Exception as e:
            console.print(f"[red]Error reading logs: {e}[/]")


# ─────────────────────────────────────────────
#  Test
# ─────────────────────────────────────────────

@main.command()
@click.option("--agent", "-a", default=None, help="Agent ID to test with")
def test(agent):
    """Quick connectivity and functionality test."""
    console.print(BANNER)
    console.print("[bold]Running tests...[/]\n")

    base = "http://localhost:50001"
    passed = 0
    failed = 0

    def _test(name, fn):
        nonlocal passed, failed
        try:
            result = fn()
            console.print(f"  [green]✓[/] {name}: {result}")
            passed += 1
        except Exception as e:
            console.print(f"  [red]✗[/] {name}: {e}")
            failed += 1

    import httpx

    # Health
    _test("Health check", lambda: httpx.get(f"{base}/health", timeout=5).json()["status"])

    # Ingest
    _test("Ingest (live wire)", lambda: httpx.post(
        f"{base}/ingest",
        json={"prompt": "Test prompt from CLI", "response": "Test response", "agent_id": agent},
        timeout=5,
    ).json()["status"])

    # Context
    _test("Context search", lambda: f"{httpx.post(f'{base}/context', json={'prompt': 'test', 'agent_id': agent}, timeout=10).json()['total_found']} chunks found")

    # Sessions
    _test("Session listing", lambda: f"{len(httpx.get(f'{base}/sessions', params={'agent_id': agent} if agent else {}, timeout=5).json().get('hot', []))} hot sessions")

    console.print()
    if failed == 0:
        console.print(f"  [green bold]All {passed} tests passed! ⚡[/]")
    else:
        console.print(f"  [yellow]{passed} passed, {failed} failed[/]")


# ─────────────────────────────────────────────
#  Watch — Session Watcher
# ─────────────────────────────────────────────

WATCHER_PID_FILE = DATA_DIR / "watcher.pid"

@main.command()
@click.option("--backfill", "-b", is_flag=True, help="Backfill existing sessions first")
@click.option("--backfill-count", default=10, help="Number of recent sessions to backfill")
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground")
def watch(backfill, backfill_count, foreground):
    """Start the session watcher (auto-captures OpenClaw conversations)."""
    console.print(BANNER)

    if _is_watcher_running():
        console.print("[yellow]Watcher is already running.[/]")
        return

    if backfill:
        console.print("[cyan]Backfilling existing sessions...[/]")
        from agentb.watcher import backfill_sessions
        backfill_sessions(backfill_count)
        console.print()

    env = os.environ.copy()
    env.setdefault("MNEMO_URL", "http://localhost:50001")
    env.setdefault("MNEMO_AGENT_ID", "rocky")

    watcher_script = Path(__file__).parent / "watcher.py"
    cmd = [sys.executable, str(watcher_script)]

    if foreground:
        console.print("[yellow]Watcher running in foreground... (Ctrl+C to stop)[/]")
        try:
            subprocess.run(cmd, env=env)
        except KeyboardInterrupt:
            console.print("\n[yellow]Watcher stopped.[/]")
    else:
        log_file = DATA_DIR / "logs" / "watcher.log"
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        log_fh = open(log_file, "a")
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        WATCHER_PID_FILE.write_text(str(proc.pid), encoding="utf-8")

        time.sleep(1)
        if proc.poll() is None:
            console.print(f"[green]⚡ Session watcher started![/]")
            console.print(f"  PID: {proc.pid}")
            console.print(f"  Log: {log_file}")
            console.print(f"  Watching: ~/.openclaw/agents/main/sessions/")
            console.print()
            console.print("  [dim]Every exchange Rocky has is now auto-captured.[/]")
            console.print("  [dim]mnemo-cortex unwatch — stop the watcher[/]")
        else:
            console.print("[red]Watcher failed to start. Check logs.[/]")


@main.command()
def unwatch():
    """Stop the session watcher."""
    if not _is_watcher_running():
        console.print("[yellow]Watcher is not running.[/]")
        WATCHER_PID_FILE.unlink(missing_ok=True)
        return

    pid = _get_watcher_pid()
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Watcher stopped (PID {pid})[/]")
    except ProcessLookupError:
        console.print("[yellow]Watcher process already gone.[/]")
    WATCHER_PID_FILE.unlink(missing_ok=True)


# ─────────────────────────────────────────────
#  Refresh — Write Mnemo context to workspace
# ─────────────────────────────────────────────

REFRESH_PID_FILE = DATA_DIR / "refresh.pid"

@main.command()
@click.option("--workspace", "-w", default=None, help="Path to agent workspace (auto-detects OpenClaw)")
@click.option("--output", "-o", default="MNEMO-CONTEXT.md", help="Output filename")
@click.option("--agent", "-a", default=None, help="Agent ID")
@click.option("--watch", "watch_mode", is_flag=True, help="Keep refreshing every 60 seconds (daemon mode)")
@click.option("--interval", default=60, help="Refresh interval in seconds (with --watch)")
@click.option("--recent", "-n", default=15, help="Number of recent exchanges to include")
@click.option("--foreground", "-f", is_flag=True, help="Run daemon in foreground")
def refresh(workspace, output, agent, watch_mode, interval, recent, foreground):
    """Write Mnemo memory context to your agent's workspace.

    Creates a MNEMO-CONTEXT.md file that your agent reads at boot.
    No hooks required — works with any agent framework that reads workspace files.

    \b
    Examples:
      mnemo-cortex refresh                   # one-time write
      mnemo-cortex refresh --watch           # keep refreshing every 60s
      mnemo-cortex refresh --watch -f        # daemon in foreground
      mnemo-cortex refresh -w /path/to/ws    # custom workspace path
    """
    console.print(BANNER)

    # Auto-detect workspace
    workspace_path = _detect_workspace(workspace)
    if not workspace_path:
        console.print("[red]Cannot find agent workspace.[/]")
        console.print("  Specify with: [bold]mnemo-cortex refresh -w /path/to/workspace[/]")
        return

    # Detect Mnemo URL
    import httpx
    mnemo_url = os.environ.get("MNEMO_URL", "http://localhost:50001")
    try:
        health = httpx.get(f"{mnemo_url}/health", timeout=3).json()
        if health.get("status") not in ("ok", "degraded"):
            console.print(f"[red]Mnemo server unhealthy at {mnemo_url}[/]")
            return
    except Exception:
        console.print(f"[red]Cannot reach Mnemo at {mnemo_url}[/]")
        console.print("  Set MNEMO_URL environment variable or start the server.")
        return

    agent_id = agent or os.environ.get("MNEMO_AGENT_ID", "rocky")
    output_path = workspace_path / output

    if not watch_mode:
        # One-time refresh
        success = _do_refresh(mnemo_url, agent_id, recent, output_path)
        if success:
            console.print(f"[green]⚡ Context written to:[/] {output_path}")
            console.print(f"  Agent: {agent_id}")
            console.print(f"  Exchanges: up to {recent}")
            console.print()
            console.print("  [dim]Your agent will read this file at next boot.[/]")
        else:
            console.print("[yellow]No context available from Mnemo yet.[/]")
        return

    # Daemon mode
    if _is_refresh_running():
        console.print("[yellow]Refresh daemon is already running.[/]")
        return

    if foreground:
        console.print(f"[yellow]Refreshing {output_path} every {interval}s... (Ctrl+C to stop)[/]")
        try:
            while True:
                _do_refresh(mnemo_url, agent_id, recent, output_path)
                console.print(f"  [dim]{time.strftime('%H:%M:%S')} — refreshed[/]")
                time.sleep(interval)
        except KeyboardInterrupt:
            console.print("\n[yellow]Refresh daemon stopped.[/]")
    else:
        # Background daemon
        env = os.environ.copy()
        env["MNEMO_URL"] = mnemo_url
        env["MNEMO_AGENT_ID"] = agent_id

        refresh_script = Path(__file__).parent / "refresher.py"
        cmd = [
            sys.executable, str(refresh_script),
            str(workspace_path), output, str(recent), str(interval),
        ]

        log_file = DATA_DIR / "logs" / "refresh.log"
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        log_fh = open(log_file, "a")
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        REFRESH_PID_FILE.write_text(str(proc.pid), encoding="utf-8")

        time.sleep(1)
        if proc.poll() is None:
            console.print(f"[green]⚡ Refresh daemon started![/]")
            console.print(f"  PID: {proc.pid}")
            console.print(f"  Output: {output_path}")
            console.print(f"  Interval: every {interval}s")
            console.print(f"  Log: {log_file}")
            console.print()
            console.print("  [dim]mnemo-cortex unrefresh — stop the daemon[/]")
        else:
            console.print("[red]Refresh daemon failed to start. Check logs.[/]")


@main.command()
def unrefresh():
    """Stop the refresh daemon."""
    if not _is_refresh_running():
        console.print("[yellow]Refresh daemon is not running.[/]")
        REFRESH_PID_FILE.unlink(missing_ok=True)
        return

    pid = _get_refresh_pid()
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Refresh daemon stopped (PID {pid})[/]")
    except ProcessLookupError:
        console.print("[yellow]Refresh daemon process already gone.[/]")
    REFRESH_PID_FILE.unlink(missing_ok=True)


def _do_refresh(mnemo_url: str, agent_id: str, recent: int, output_path: Path) -> bool:
    """Fetch context from Mnemo and write to file. Returns True if context was written."""
    import httpx
    context_text = ""

    # Try /sessions/recent first
    try:
        resp = httpx.get(
            f"{mnemo_url}/sessions/recent",
            params={"agent_id": agent_id, "n": recent},
            timeout=5,
        )
        if resp.status_code == 200:
            context_text = resp.json().get("context", "")
    except Exception:
        pass

    # Fallback to /context search
    if not context_text.strip():
        try:
            resp = httpx.post(
                f"{mnemo_url}/context",
                json={"prompt": "recent project status active tasks", "agent_id": agent_id, "max_results": 3},
                timeout=8,
            )
            if resp.status_code == 200:
                chunks = resp.json().get("chunks", [])
                if chunks:
                    context_text = "\n\n---\n\n".join(
                        f"[{c.get('cache_tier', '?')}|{c.get('relevance', '?')}] {c.get('content', '')}"
                        for c in chunks
                    )
        except Exception:
            pass

    if not context_text.strip():
        return False

    # Write the file
    header = (
        "# ⚡ Mnemo Cortex — Memory Context\n"
        f"_Auto-refreshed at {time.strftime('%Y-%m-%d %H:%M:%S')}_\n"
        f"_Agent: {agent_id} | Source: {mnemo_url}_\n\n"
    )
    output_path.write_text(header + context_text + "\n", encoding="utf-8")
    return True


def _detect_workspace(explicit_path: str = None) -> Path:
    """Detect the agent workspace directory."""
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return p
        return None

    # Try OpenClaw default
    openclaw_ws = Path.home() / ".openclaw" / "workspace"
    if openclaw_ws.exists():
        return openclaw_ws

    # Try Agent Zero
    a0_ws = Path.home() / ".agent-zero" / "workspace"
    if a0_ws.exists():
        return a0_ws

    # Current directory as fallback
    return Path.cwd()


def _get_refresh_pid() -> int:
    try:
        return int(REFRESH_PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _is_refresh_running() -> bool:
    pid = _get_refresh_pid()
    if pid == 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _get_watcher_pid() -> int:
    try:
        return int(WATCHER_PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _is_watcher_running() -> bool:
    pid = _get_watcher_pid()
    if pid == 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def _get_pid() -> int:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _is_running() -> bool:
    pid = _get_pid()
    if pid == 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = check if process exists
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but we can't signal it


# ─────────────────────────────────────────────
#  Recall — Exact-match memory (SQLite FTS5)
# ─────────────────────────────────────────────

@main.group()
def recall():
    """Exact-match memory search via SQLite FTS5.

    Complements Mnemo's semantic search with precise keyword and entity recall.

    \b
    Commands:
      mnemo-cortex recall init      — Initialize recall index
      mnemo-cortex recall index     — Rebuild index from markdown files
      mnemo-cortex recall search    — Search memories by keyword
      mnemo-cortex recall remember  — Store a new memory
      mnemo-cortex recall reflect   — Generate entity summary pages
      mnemo-cortex recall pack      — Generate a memory pack for /new recovery
    """
    pass


def _get_workspace(workspace: str = None) -> Path:
    """Resolve workspace path."""
    if workspace:
        return Path(workspace).expanduser().resolve()
    # Auto-detect OpenClaw workspace
    default = Path.home() / ".openclaw" / "workspace"
    if default.exists():
        return default
    return Path.cwd()


@recall.command("init")
@click.option("--workspace", "-w", default=None, help="Path to agent workspace")
def recall_init(workspace):
    """Initialize the recall memory system in your workspace."""
    from agentb.recall.parser import iter_records
    from agentb.recall.store import connect, default_db_path, rebuild_index

    ws = _get_workspace(workspace)

    # Create standard directories
    (ws / "memory").mkdir(parents=True, exist_ok=True)
    (ws / "bank" / "entities").mkdir(parents=True, exist_ok=True)

    # Seed files if they don't exist
    seed_files = {
        ws / "bank" / "world.md": "# World\n\n",
        ws / "bank" / "experience.md": "# Experience\n\n",
        ws / "bank" / "opinions.md": "# Opinions\n\n",
    }
    for path, body in seed_files.items():
        if not path.exists():
            path.write_text(body, encoding="utf-8")

    conn = connect(default_db_path(ws))
    count = rebuild_index(conn, iter_records(ws))
    console.print(f"[green]⚡ Recall initialized![/] Indexed {count} memory records in {ws}")


@recall.command("index")
@click.option("--workspace", "-w", default=None, help="Path to agent workspace")
def recall_index(workspace):
    """Rebuild the recall index from all markdown memory files."""
    from agentb.recall.parser import iter_records
    from agentb.recall.store import connect, default_db_path, rebuild_index

    ws = _get_workspace(workspace)
    conn = connect(default_db_path(ws))
    count = rebuild_index(conn, iter_records(ws))
    console.print(f"[green]Indexed {count} memory records[/] → {default_db_path(ws)}")


@recall.command("search")
@click.argument("query")
@click.option("--workspace", "-w", default=None, help="Path to agent workspace")
@click.option("--limit", "-n", default=8, help="Max results")
@click.option("--since", default=None, help="Filter by date (e.g., '14d' or '2026-03-01')")
@click.option("--entity", "-e", default=None, help="Filter by entity name")
@click.option("--json-output", "--json", "as_json", is_flag=True, help="Output as JSON")
def recall_search(query, workspace, limit, since, entity, as_json):
    """Search memories by keyword. Fast exact-match via SQLite FTS5."""
    import json as json_mod
    from agentb.recall.store import connect, default_db_path, search
    from agentb.recall.utils import parse_since, relpath

    ws = _get_workspace(workspace)
    conn = connect(default_db_path(ws))
    rows = search(conn, query, limit=limit, since=parse_since(since), entity=entity)

    if not rows:
        console.print("[yellow]No matches found.[/]")
        return

    if as_json:
        payload = []
        for row in rows:
            payload.append({
                "kind": row["kind"],
                "date": row["date"],
                "title": row["title"],
                "text": row["text"],
                "source": f"{relpath(Path(row['path']), ws)}#L{row['line_start']}-L{row['line_end']}",
                "score": row["score"],
            })
        console.print(json_mod.dumps(payload, indent=2))
        return

    for i, row in enumerate(rows, start=1):
        src = f"{relpath(Path(row['path']), ws)}#L{row['line_start']}-L{row['line_end']}"
        stamp = f"{row['date']} " if row["date"] else ""
        console.print(f"  [cyan][{i}][/] [bold]{row['kind']}[/] {stamp}")
        console.print(f"      {row['text']}")
        console.print(f"      [dim]source: {src}[/]")
        console.print()


@recall.command("remember")
@click.option("--workspace", "-w", default=None, help="Path to agent workspace")
@click.option("--text", "-t", required=True, help="The memory to store")
@click.option("--kind", "-k", default="fact", help="Memory kind (fact, decision, lesson, etc.)")
@click.option("--entity", "-e", multiple=True, help="Associated entities (repeatable)")
@click.option("--confidence", "-c", type=float, default=None, help="Confidence score 0.0-1.0")
@click.option("--date", "-d", "rec_date", default=None, help="Override date (YYYY-MM-DD)")
def recall_remember(workspace, text, kind, entity, confidence, rec_date):
    """Store a new memory fact, decision, or lesson."""
    from datetime import date as date_mod
    from agentb.recall.models import MemoryRecord
    from agentb.recall.store import connect, default_db_path, append_record

    ws = _get_workspace(workspace)
    stamp = rec_date or date_mod.today().isoformat()
    daily_path = ws / "memory" / f"{stamp}.md"
    daily_path.parent.mkdir(parents=True, exist_ok=True)

    if not daily_path.exists():
        daily_path.write_text(f"# {stamp}\n\n## Retain\n", encoding="utf-8")

    # Build the bullet line
    tags = " ".join(f"@{e}" for e in entity)
    conf = f" c={confidence:.2f}" if confidence is not None else ""
    spacer = " " if tags else ""
    with daily_path.open("a", encoding="utf-8") as f:
        f.write(f"- {kind}{conf} {tags}{spacer}{text}\n")

    line_count = len(daily_path.read_text(encoding="utf-8").splitlines())

    rec = MemoryRecord(
        path=daily_path,
        line_start=line_count,
        line_end=line_count,
        kind=kind,
        text=text,
        entities=list(entity),
        confidence=confidence,
        date=stamp,
        title="Retain",
    )

    conn = connect(default_db_path(ws))
    append_record(conn, rec)
    console.print(f"[green]⚡ Remembered:[/] {kind} → {daily_path.name}")


@recall.command("reflect")
@click.option("--workspace", "-w", default=None, help="Path to agent workspace")
def recall_reflect(workspace):
    """Rebuild index and generate per-entity summary pages."""
    from agentb.recall.parser import iter_records
    from agentb.recall.reflect import write_entity_pages
    from agentb.recall.store import connect, default_db_path, rebuild_index

    ws = _get_workspace(workspace)
    conn = connect(default_db_path(ws))
    indexed = rebuild_index(conn, iter_records(ws))
    written = write_entity_pages(conn, ws)
    console.print(f"[green]Reflected {indexed} records → {written} entity pages[/]")


@recall.command("pack")
@click.option("--workspace", "-w", default=None, help="Path to agent workspace")
@click.option("--since", default="14d", help="How far back to include (e.g., '14d', '2026-03-01')")
@click.option("--limit", "-n", default=12, help="Max memories in pack")
def recall_pack(workspace, since, limit):
    """Generate a memory pack for /new recovery.

    Outputs recent memories as a markdown list that can be injected
    into a new session's context.
    """
    from agentb.recall.store import connect, default_db_path, recent_pack
    from agentb.recall.utils import parse_since, relpath

    ws = _get_workspace(workspace)
    conn = connect(default_db_path(ws))
    rows = recent_pack(conn, since=parse_since(since), limit=limit)

    if not rows:
        console.print("[yellow]No recent memories found.[/]")
        return

    console.print("[bold]# Memory Pack[/]\n")
    for row in rows:
        src = f"{relpath(Path(row['path']), ws)}#L{row['line_start']}-L{row['line_end']}"
        stamp = f"[{row['date']}] " if row["date"] else ""
        console.print(f"- {stamp}{row['kind']}: {row['text']} [dim]_(source: {src})_[/]")


main.add_command(doctor)
main.add_command(health)


# ─────────────────────────────────────────────
#  Dump — Developer Dump (Mnemo v4 Phase 1)
# ─────────────────────────────────────────────

@main.group()
def dump():
    """Inspect Developer Dump files (MCP bridge tool-call captures).

    \b
    Commands:
      mnemo-cortex dump list           — List dump files (size + mtime)
      mnemo-cortex dump tail <agent>   — Live-tail today's dump for an agent

    Dumps are written by the MCP bridge when MNEMO_DUMP=on. Default path:
    ~/.mnemo-cortex/dumps/<agent_id>/<YYYY-MM-DD>.jsonl
    Override with MNEMO_DUMP_DIR.
    """
    pass


def _dump_root() -> Path:
    return Path(os.environ.get(
        "MNEMO_DUMP_DIR",
        str(Path.home() / ".mnemo-cortex" / "dumps"),
    )).expanduser()


@dump.command("list")
def dump_list():
    """List dump files with size + line count + last modified."""
    root = _dump_root()
    if not root.exists():
        console.print(f"[yellow]No dumps yet at {root}.[/]")
        console.print("  Set MNEMO_DUMP=on in your MCP bridge env to start capturing.")
        return

    rows = []
    for agent_dir in sorted(root.iterdir()):
        if not agent_dir.is_dir():
            continue
        for f in sorted(agent_dir.glob("*.jsonl")):
            st = f.stat()
            try:
                with f.open("rb") as fh:
                    lines = sum(1 for _ in fh)
            except OSError:
                lines = -1
            rows.append((agent_dir.name, f.stem, st.st_size, lines, st.st_mtime))

    if not rows:
        console.print(f"[yellow]No dump files under {root}.[/]")
        return

    rows.sort(key=lambda r: r[4], reverse=True)
    table = Table(title=f"Developer Dump — {root}")
    table.add_column("agent", style="cyan")
    table.add_column("date")
    table.add_column("size", justify="right")
    table.add_column("lines", justify="right")
    table.add_column("modified", style="dim")
    for agent, date, size, lines, mtime in rows:
        size_h = f"{size:,} B" if size < 1024 else f"{size / 1024:.1f} KB"
        if size >= 1024 * 1024:
            size_h = f"{size / (1024 * 1024):.1f} MB"
        mtime_h = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        table.add_row(agent, date, size_h, str(lines), mtime_h)
    console.print(table)


@dump.command("tail")
@click.argument("agent_id")
@click.option("-n", "--lines", default=20, help="Lines to show before tailing")
@click.option("--no-follow", is_flag=True, help="Print and exit instead of following")
def dump_tail(agent_id, lines, no_follow):
    """Live-tail today's dump for AGENT_ID."""
    today = time.strftime("%Y-%m-%d")
    path = _dump_root() / agent_id / f"{today}.jsonl"
    if not path.exists():
        console.print(f"[yellow]No dump file for '{agent_id}' on {today}.[/]")
        console.print(f"  Expected: {path}")
        console.print("  Is MNEMO_DUMP=on in the bridge env? Has the agent made a tool call today?")
        return

    cmd = ["tail", "-n", str(lines)]
    if not no_follow:
        cmd.append("-f")
    cmd.append(str(path))
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass


# ─────────────────────────────────────────────
#  Migrate — Smart Ingestion reclassification (Mnemo v4)
# ─────────────────────────────────────────────

@main.group()
def migrate():
    """One-time store maintenance (Mnemo v4 Smart Ingestion).

    \b
    Reclassify uncategorized / 'unknown' / routine-log memories with the
    reasoning LLM so real memories (Tier 1) stop sharing recall slots with raw
    session logs (Tier 2). Rewrites only the category field — never embeddings.

    \b
      mnemo-cortex migrate reclassify --all --dry-run    # preview, write nothing
      mnemo-cortex migrate reclassify --agent cc         # one store
      mnemo-cortex migrate reclassify --all              # all stores (backup first)
    """
    pass


@migrate.command("reclassify")
@click.option("--agent", "-a", "agents", multiple=True, help="Agent id (repeatable).")
@click.option("--all", "all_agents", is_flag=True, help="Every agent store found on disk.")
@click.option("--dry-run", is_flag=True, help="Show projected before→after spread; write nothing.")
@click.option("--no-backup", is_flag=True, help="Skip the pre-migration snapshot (not recommended).")
@click.option("--unknown-only", is_flag=True, help="Only touch unknown/missing/flagged; leave routine logs.")
@click.option("--purge-noise", is_flag=True, help="Also delete empty/sentinel rows (never real session logs).")
def migrate_reclassify_cmd(agents, all_agents, dry_run, no_backup, unknown_only, purge_noise):
    """Reclassify uncategorized/unknown/routine-log memories via the LLM."""
    from agentb.config import load_config
    from agentb.migrate import migrate_reclassify

    config = load_config()

    if all_agents:
        base = Path(config.data_dir or DATA_DIR) / "agents"
        agent_ids = sorted(
            d.name for d in base.glob("*") if (d / "memory").is_dir()
        ) if base.exists() else []
    else:
        agent_ids = list(agents)

    if not agent_ids:
        console.print("[yellow]No agents selected. Use [bold]--agent <id>[/] or [bold]--all[/].[/]")
        return

    console.print(
        f"[bold]Reclassify[/] {'[yellow](dry run)[/] ' if dry_run else ''}"
        f"→ {', '.join(agent_ids)}"
    )
    migrate_reclassify(
        agent_ids, dry_run=dry_run, backup=not no_backup,
        include_routine=not unknown_only, purge_noise=purge_noise, config=config,
    )


@migrate.command("reindex")
@click.option("--agent", "-a", "agents", multiple=True, help="Agent id (repeatable).")
@click.option("--all", "all_agents", is_flag=True, help="Every agent store found on disk.")
@click.option("--dry-run", is_flag=True, help="Count what would be re-embedded; write nothing.")
@click.option("--no-backup", is_flag=True, help="Skip the pre-migration snapshot (not recommended).")
@click.option("--include-trajectories/--no-trajectories", default=True,
              help="Also re-embed the per-tenant trajectory index (default: on).")
def migrate_reindex_cmd(agents, all_agents, dry_run, no_backup, include_trajectories):
    """Re-embed every stored vector with the nomic task prefix (server must be STOPPED).

    \b
    One-time deploy step for the search_document:/search_query: prefix fix.
    Backs up memory/ + vec_index.sqlite + trajectories/ per tenant, re-embeds
    through the PRIMARY embedder only (aborts loudly if it goes down), then
    wipes the L1/L2 caches so no old-space vector survives. Idempotent.
    """
    from agentb.config import load_config, validate_agent_id
    from agentb.migrate import migrate_reindex, ReindexAbort

    config = load_config()

    if all_agents:
        base = Path(config.data_dir or DATA_DIR) / "agents"
        found = sorted(
            d.name for d in base.glob("*") if (d / "memory").is_dir()
        ) if base.exists() else []
        # Archived tenant snapshots (e.g. "rocky.archived-20260516") live in
        # the same dir but are not valid agent_ids — they're cold copies, not
        # served tenants, and validate_agent_id (C1) would abort the whole
        # run on the first one. Skip them, loudly.
        agent_ids = []
        for name in found:
            try:
                validate_agent_id(name)
                agent_ids.append(name)
            except ValueError:
                console.print(f"[yellow]Skipping non-tenant dir:[/] {name}")
    else:
        agent_ids = list(agents)

    if not agent_ids:
        console.print("[yellow]No agents selected. Use [bold]--agent <id>[/] or [bold]--all[/].[/]")
        return

    console.print(
        f"[bold]Reindex[/] {'[yellow](dry run)[/] ' if dry_run else ''}"
        f"→ {', '.join(agent_ids)}"
    )
    try:
        migrate_reindex(
            agent_ids, dry_run=dry_run, backup=not no_backup,
            include_trajectories=include_trajectories, config=config,
        )
    except ReindexAbort as e:
        console.print(f"[bold red]ABORTED:[/] {e}")
        raise SystemExit(1)


@migrate.command("vec-backfill")
@click.option("--agent", "-a", "agents", multiple=True, help="Agent id (repeatable).")
@click.option("--all", "all_agents", is_flag=True, help="Every agent store found on disk.")
def migrate_vec_backfill_cmd(agents, all_agents):
    """Populate vec_sources.category from disk (#468 one-time deploy step)."""
    from agentb.config import load_config
    from agentb.migrate import migrate_vec_backfill

    config = load_config()

    if all_agents:
        base = Path(config.data_dir or DATA_DIR) / "agents"
        agent_ids = sorted(
            d.name for d in base.glob("*") if (d / "memory").is_dir()
        ) if base.exists() else []
    else:
        agent_ids = list(agents)

    if not agent_ids:
        console.print("[yellow]No agents selected. Use [bold]--agent <id>[/] or [bold]--all[/].[/]")
        return

    console.print(f"[bold]Vec category backfill[/] → {', '.join(agent_ids)}")
    migrate_vec_backfill(agent_ids, config=config)


# ─────────────────────────────────────────────
# Cortex Stick — USB courier between installs
# ─────────────────────────────────────────────

@main.group()
def ledger():
    """Memory Ledger — tamper-evident chain over what the store was told.

    Every save seals the memory's testimony (summary, facts, decisions, who,
    when) into an append-only per-tenant ledger.jsonl. Verify walks the
    chain and checks every sealed record on disk. Seal adopts records the
    ledger has never seen (pre-ledger stores, Cortex Stick carries).

    \b
      mnemo-cortex ledger verify --agent cc        # chain + records, one tenant
      mnemo-cortex ledger verify --all             # every tenant on this host
      mnemo-cortex ledger seal --agent cc          # adopt unsealed records
    """
    pass


def _ledger_tenants(agent, all_agents) -> list[tuple[str, Path]]:
    """--all = every tenant under data_dir/agents PLUS every configured
    agent (a config data_dir override relocates a tenant out of the scan).
    Zero tenants is an error, not a clean report over nothing."""
    from agentb.config import load_config, get_agent_data_dir, validate_agent_id
    config = load_config()
    if all_agents:
        found: dict[str, Path] = {}
        root = Path(config.data_dir) / "agents"
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if not (d / "memory").is_dir():
                    continue
                # v4.20.1: an archived tenant dir (`rocky.archived-20260516`)
                # is not an agent_id the server accepts; feeding it to
                # /ledger/seal aborted the whole upgrade run on tenant 1 of
                # 26. Skip it out loud — nothing recalls from it anyway.
                try:
                    validate_agent_id(d.name)
                except ValueError:
                    # stderr: `verify --all --json` must stay pure JSON on stdout.
                    click.echo(f"  skipped {d.name}: not a valid agent_id (archived tenant?)", err=True)
                    continue
                found[d.name] = d / "memory"
        for name in config.agents:
            found[name] = get_agent_data_dir(config, name) / "memory"
        if not found:
            console.print(f"[red]No tenants found under {root} or in config.[/]")
            raise SystemExit(1)
        return sorted(found.items())
    if not agent:
        console.print("[red]Pass --agent <id> or --all.[/]")
        raise SystemExit(2)
    return [(agent, get_agent_data_dir(config, agent) / "memory")]


def _ledger_server_alive(config) -> bool:
    """Is a server holding this host's ledger tails? Supervised installs
    (systemd, launchd) never write the PID file `_is_running` reads, so the
    probe is the port itself. Refused-connection = down; anything else
    (timeout, non-200) is ambiguous and the caller must not touch the
    file — a disk seal beside a live server forks the chain."""
    import httpx
    host = config.server.host
    if host in ("", "0.0.0.0", "::"):
        host = "localhost"
    url = f"http://{host}:{config.server.port}/health"
    try:
        resp = httpx.get(url, timeout=5.0)
    except httpx.ConnectError:
        return False
    except Exception as exc:
        console.print(f"[red]Cannot tell whether the server is running ({url}: "
                      f"{type(exc).__name__}). Refusing to write the ledger file.[/]")
        raise SystemExit(1)
    if resp.status_code != 200:
        console.print(f"[red]{url} answered {resp.status_code} — server state ambiguous. "
                      "Refusing to write the ledger file.[/]")
        raise SystemExit(1)
    return True


def _ledger_print(agent_id: str, rep: dict) -> None:
    color = "green" if rep["ok"] else "red"
    console.print(f"  [bold]{agent_id}[/]: chain [{color}]{rep['chain']}[/] "
                  f"({rep['entries']} entries) · sealed {rep['sealed']} · "
                  f"altered {len(rep['altered'])} · missing {len(rep['missing'])} · "
                  f"unsealed {len(rep['unsealed'])}"
                  + (f" · [yellow]disputed {len(rep['disputed'])}[/]" if rep.get("disputed") else ""))
    if rep.get("broken_at") is not None:
        console.print(f"    [red]broken at seq {rep['broken_at']}: {rep['reason']}[/]")
    for label in ("altered", "missing", "disputed"):
        for mid in rep.get(label, [])[:8]:
            console.print(f"    [red]{label}[/] {mid}")
        if len(rep.get(label, [])) > 8:
            console.print(f"    [dim]… and {len(rep[label]) - 8} more {label}[/]")


@ledger.command("verify")
@click.option("--agent", default=None, help="Tenant to verify.")
@click.option("--all", "all_agents", is_flag=True, help="Every tenant under data_dir.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable report.")
def ledger_verify(agent, all_agents, as_json):
    """Walk the chain and check every sealed record on disk. Read-only.
    Exit 1 if any tenant is not ok."""
    from agentb.ledger import Ledger, ledger_path_for
    reports = {}
    for agent_id, memory_dir in _ledger_tenants(agent, all_agents):
        reports[agent_id] = Ledger(ledger_path_for(memory_dir)).verify(memory_dir).to_dict()
    if as_json:
        print(json.dumps(reports, indent=2))
    else:
        for agent_id, rep in reports.items():
            _ledger_print(agent_id, rep)
    if not all(r["ok"] for r in reports.values()):
        raise SystemExit(1)


@ledger.command("seal")
@click.option("--agent", default=None, help="Tenant to seal.")
@click.option("--all", "all_agents", is_flag=True, help="Every tenant under data_dir.")
def ledger_seal(agent, all_agents):
    """Adopt every unsealed record. Goes through the running server when
    one answers /health (it owns the chain's tail); writes the file
    directly only when the port refuses; refuses on anything ambiguous.
    A broken chain is never adopted onto — exit 1 with the report."""
    from agentb.config import load_config
    from agentb.ledger import Ledger, LedgerBroken, ledger_path_for
    targets = _ledger_tenants(agent, all_agents)
    config = load_config()
    if _ledger_server_alive(config):
        import httpx
        host = config.server.host if config.server.host not in ("", "0.0.0.0", "::") else "localhost"
        url = f"http://{host}:{config.server.port}/ledger/seal"
        headers = {"X-API-KEY": config.server.auth_token} if config.server.auth_token else {}
        failed = False
        for agent_id, _ in targets:
            # v4.20.2: no read cap — a first-time adopt of 17k records took
            # the server ~150 s and the old 120 s cap killed the CLI with a
            # traceback while the server finished the job. Transport errors
            # print red and the run continues.
            try:
                resp = httpx.post(url, json={"agent_id": agent_id}, headers=headers,
                                  timeout=httpx.Timeout(10.0, read=None))
            except httpx.HTTPError as exc:
                console.print(f"  [red]{agent_id}: {type(exc).__name__}: {exc} — the server may still "
                              "finish this tenant; rerun `ledger verify` after[/]")
                failed = True
                continue
            if resp.status_code == 409:
                rep = resp.json().get("detail", {})
                console.print(f"  [red]{agent_id}: chain broken — refusing to adopt[/]")
                _ledger_print(agent_id, rep)
                failed = True
                continue
            if resp.status_code != 200:
                # One tenant's refusal must not strand the other 25:
                # report it, keep going, exit 1 at the end.
                console.print(f"  [red]{agent_id}: server said {resp.status_code} {resp.text[:200]}[/]")
                failed = True
                continue
            rep = resp.json()
            console.print(f"  [bold]{agent_id}[/]: sealed {rep['sealed_now']} now")
            _ledger_print(agent_id, rep)
        if failed:
            raise SystemExit(1)
        return
    failed = False
    for agent_id, memory_dir in targets:
        led = Ledger(ledger_path_for(memory_dir))
        try:
            sealed = led.seal_unsealed(memory_dir)
        except LedgerBroken as exc:
            console.print(f"  [red]{agent_id}: chain broken — refusing to adopt[/]")
            _ledger_print(agent_id, exc.report.to_dict())
            failed = True
            continue
        console.print(f"  [bold]{agent_id}[/]: sealed {len(sealed)} now")
        _ledger_print(agent_id, led.verify(memory_dir).to_dict())
    if failed:
        raise SystemExit(1)


@main.group()
def stick():
    """Cortex Stick — USB courier sync between two full Mnemo installs.

    Both machines run full Mnemo; the stick carries the delta between them:
    memories, trajectories, the brain git repo, and a project pad. No cloud,
    no VPN — plug in, sync, carry, plug in.

    \b
      mnemo-cortex stick init --encrypt /media/you/USB   # provision, encrypted
      mnemo-cortex stick unlock                   # enroll another host (passphrase)
      mnemo-cortex stick sync                     # locate the stick and sync
      mnemo-cortex stick status                   # what would sync, per side
      mnemo-cortex stick watch                    # sync automatically on plug-in
      mnemo-cortex stick encrypt                  # upgrade a plaintext stick

    \b
    Encrypt your stick (AES-256-SIV, passphrase-derived key). A plaintext
    stick is a notebook full of your working memory — anyone holding it can
    read everything. The key lives on each host, never on the stick.
    """
    pass


def _stick_passphrase(passphrase_file, *, confirm: bool) -> str:
    """Passphrase from --passphrase-file (first line; for automation) or an
    interactive hidden prompt."""
    if passphrase_file:
        text = Path(passphrase_file).read_text(encoding="utf-8").splitlines()
        if not text or not text[0]:
            console.print(f"[red]Empty passphrase file: {passphrase_file}[/]")
            raise SystemExit(1)
        return text[0]
    return click.prompt("Stick passphrase", hide_input=True,
                        confirmation_prompt=confirm)


def _stick_locate(mount, host_cfg):
    from agentb.stick import find_stick, STICK_DIRNAME
    if mount:
        p = Path(mount)
        cand = p if p.name == STICK_DIRNAME else p / STICK_DIRNAME
        if (cand / "passport.json").is_file():
            return cand
        console.print(f"[red]No Cortex Stick at {p} (missing passport.json).[/]")
        return None
    found = find_stick(host_cfg.get("mount_roots") or None)
    if not found:
        console.print("[red]No Cortex Stick found under any mount root. "
                      "Plug it in, or pass the mount path explicitly.[/]")
    return found


def _stick_report(report) -> None:
    rows = [
        ("→ stick", report.to_stick), ("→ host", report.to_host),
        ("deleted on stick", report.deleted_on_stick),
        ("deleted on host", report.deleted_on_host),
        ("union-merged", report.merged_jsonl),
    ]
    for label, items in rows:
        if items:
            console.print(f"  [bold]{label}[/]: {len(items)}")
            for it in items[:8]:
                console.print(f"    [dim]{it}[/]")
            if len(items) > 8:
                console.print(f"    [dim]… and {len(items) - 8} more[/]")
    console.print(f"  [bold]brain[/]: {report.brain}")
    if report.facts_to_host or report.facts_to_stick:
        console.print(f"  [bold]facts[/]: {report.facts_to_host} → host, "
                      f"{report.facts_to_stick} → stick")
    if report.history_to_host or report.history_to_stick:
        console.print(f"  [bold]fact history[/]: {report.history_to_host} → host, "
                      f"{report.history_to_stick} → stick")
    for w in report.warnings:
        console.print(f"  [yellow]⚠ {w}[/]")
    for c in report.conflicts:
        console.print(f"  [bold red]⚡ CONFLICT[/] {c}")
    if not report.changed and not report.warnings:
        console.print("  [dim]nothing to carry — already in sync[/]")


def _notify_desktop(title: str, body: str) -> None:
    """Best-effort desktop toast for unattended watch mode.

    Linux notify-send, macOS osascript, or a Windows toast through the
    WinRT notifier (no module install; needs an interactive session, which
    is why the Task Scheduler unit runs "only when the user is logged on");
    silently a no-op when none applies (or the session bus is gone) — the
    console line remains the source of truth, this is just the glanceable
    copy.
    """
    import os
    import shutil
    import subprocess
    body = body.replace('"', "'")
    try:
        if os.name == "nt":
            from xml.sax.saxutils import escape
            xml = ('<toast><visual><binding template="ToastGeneric">'
                   f'<text>{escape(title)}</text><text>{escape(body)}</text>'
                   '</binding></visual></toast>')
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, "
                "Windows.UI.Notifications, ContentType = WindowsRuntime] "
                "| Out-Null; "
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom."
                "XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
                "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; "
                f"$x.LoadXml('{xml}'); "
                "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
                "\\WindowsPowerShell\\v1.0\\powershell.exe'; "
                "[Windows.UI.Notifications.ToastNotificationManager]::"
                "CreateToastNotifier($id).Show("
                "[Windows.UI.Notifications.ToastNotification]::new($x))"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                timeout=15, check=False, capture_output=True)
        elif shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "--app-name=Cortex Stick",
                 "--icon=drive-removable-media", title, body],
                timeout=10, check=False, capture_output=True)
        elif shutil.which("osascript"):
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{body}" with title "{title}"'],
                timeout=10, check=False, capture_output=True)
    except Exception:
        pass


def _volume_present_nt(drive: str) -> bool:
    return os.path.exists(drive + "\\")


def _eject_stick(stick_dir, extra_roots=None) -> tuple[bool, str]:
    """Unmount/eject the volume that holds the stick, so "safe to remove" is
    literal. Returns (ok, detail); never raises. Refuses anything that is
    not a removable volume: on POSIX the mount must sit UNDER one of the
    stick mount roots (never the root itself, never `/`), on Windows the
    drive must be a plain letter and not the system drive.

    Why: a FAT volume yanked while mounted keeps its dirty bit, and the next
    Windows desk nags "scan and fix" (S303: kernel logged "not properly
    unmounted" twice in one carry). Linux: `udisksctl unmount -b <dev>`
    (device via findmnt; plain `umount` if udisks is absent); macOS:
    `diskutil unmount <mount>`; Windows: Shell.Application InvokeVerb
    ("Eject") on the drive letter. Success is verified by the volume being
    gone, not by the command's exit code — Windows' eject is asynchronous.
    """
    import re
    import shutil
    from pathlib import PureWindowsPath
    from agentb.stick import candidate_mount_roots
    try:
        if os.name == "nt":
            # PureWindowsPath only: a concrete Path() here would pick
            # WindowsPath, which Python 3.11 refuses to instantiate off-host
            # (the cross-host test fakes os.name; 4.18.5).
            drive = PureWindowsPath(str(stick_dir)).drive  # e.g. 'E:'
            if not re.fullmatch(r"[A-Za-z]:", drive or ""):
                return False, f"not a drive letter: {stick_dir}"
            system = os.environ.get("SystemDrive", "C:")
            if drive.upper() == system.upper():
                return False, f"{drive} is the system drive — refusing to eject"
            # The verb is offered as "E&ject" (accelerator ampersand) and
            # InvokeVerb('Eject') matched nothing while returning 0 — proven
            # on the first Windows eject (S304). Enumerate, strip '&', DoIt.
            ps = ("$it = (New-Object -ComObject Shell.Application).Namespace(17)"
                  f".ParseName('{drive}'); "
                  "if (-not $it) { Write-Error 'drive not found in shell'; exit 2 }; "
                  "$v = @($it.Verbs() | Where-Object { ($_.Name -replace '&','') "
                  "-ieq 'Eject' }); "
                  "if ($v.Count -eq 0) { Write-Error 'no Eject verb offered'; exit 3 }; "
                  "$v[0].DoIt()")
            argv = ["powershell", "-NoProfile", "-NonInteractive",
                    "-Command", ps]
            still_present = lambda: _volume_present_nt(drive)  # noqa: E731
        else:
            mount = Path(stick_dir).resolve()
            while not os.path.ismount(mount) and mount.parent != mount:
                mount = mount.parent
            # floor: the mount must be a child of a stick mount root. Without
            # this the walk-up reaches `/` (always a mount) and the command
            # targets the system disk.
            roots = [r.resolve() for r in candidate_mount_roots(extra_roots)]
            if not any(mount != r and mount.is_relative_to(r) for r in roots):
                return False, (f"{mount} is not under a removable mount root "
                               "— refusing to unmount")
            if shutil.which("diskutil"):
                argv = ["diskutil", "unmount", str(mount)]
            elif shutil.which("udisksctl") and shutil.which("findmnt"):
                dev = subprocess.run(
                    ["findmnt", "-n", "-o", "SOURCE", "--target", str(mount)],
                    timeout=10, check=False, capture_output=True, text=True,
                ).stdout.strip()
                if not dev:
                    return False, f"findmnt found no device for {mount}"
                argv = ["udisksctl", "unmount", "-b", dev]
            else:
                argv = ["umount", str(mount)]
            still_present = lambda: os.path.ismount(mount)  # noqa: E731
        r = subprocess.run(argv, timeout=60, check=False,
                           capture_output=True, text=True)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            return False, (msg[:200] or f"exit {r.returncode}")
        for _ in range(60):          # 30 s: Windows' Eject returns before the letter drops
            if not still_present():
                return True, "ejected"
            time.sleep(0.5)
        return False, ("eject command returned 0 but the volume is still mounted "
                       "— something holds it open (a cloud-sync client?)")
    except Exception as e:           # noqa: BLE001 — a sync already landed; never raise past it
        return False, f"{type(e).__name__}: {e}"


def _stick_run_sync(mount, tenants, brain, force, dry_run, notify=False,
                    eject=False):
    """Shared by `stick sync` and `stick watch`. Returns True on success.

    With eject=True the volume is unmounted/ejected after the verified sync
    (never on a dry run, never after a refusal, never when the sync left
    conflict losers on the stick — those want a human at a mounted stick)
    and the console line + toast say so; an eject failure is announced
    loudly AND returns False, because the human's next move is to unmount
    by hand before pulling."""
    from agentb.config import load_config
    from agentb.stick import StickError, load_host_config, sync as stick_sync_run

    config = load_config()
    data_dir = Path(config.data_dir)
    host_cfg = load_host_config(data_dir)
    stick_dir = _stick_locate(mount, host_cfg)
    if not stick_dir:
        return False
    brain_repo = brain or host_cfg.get("brain_repo")
    try:
        report = stick_sync_run(
            data_dir, stick_dir,
            tenants=list(tenants) or host_cfg.get("tenants"),
            brain_repo=Path(brain_repo).expanduser() if brain_repo else None,
            pad=host_cfg.get("pad", True),
            facts=host_cfg.get("facts", True),
            host_id=host_cfg.get("host_id"),
            force=force, dry_run=dry_run,
        )
    except StickError as e:
        console.print(f"[bold red]SYNC REFUSED[/] {e}")
        if notify:
            # a refusal in unattended mode MUST surface — it means the stick
            # needs a human (torn generation, guard trip, wrong key)
            _notify_desktop("Cortex Stick — SYNC REFUSED", str(e)[:180])
        return False
    verb = "[yellow]DRY RUN[/]" if dry_run else "[bold]Synced[/]"
    console.print(f"{verb} {stick_dir}")
    _stick_report(report)
    imported = [p for p in report.to_host if "/memory/" in p]
    if imported and not dry_run:
        console.print(
            f"  [dim]{len(imported)} new memories imported — recallable now "
            "via disk truth; embeddings catch up on the server's next "
            "backfill (or run: mnemo-cortex migrate reindex).[/]"
        )
    if not dry_run:
        title = "Cortex Stick — synced, safe to remove"
        eject_ok, eject_detail = None, ""
        if eject and report.conflicts:
            console.print("  [bold yellow]⚠ conflicts preserved on the stick[/] — "
                          "left mounted, see state/conflicts/ before pulling")
            title = "Cortex Stick — synced, CONFLICTS: left mounted, see state/conflicts/"
        elif eject:
            console.print("  [green]✓ verified[/] (hashes readback-verified) — ejecting…")
            eject_ok, eject_detail = _eject_stick(
                stick_dir, extra_roots=host_cfg.get("mount_roots") or None)
            if eject_ok:
                console.print("  [green]✓ ejected[/] — pull it now")
                title = "Cortex Stick — synced + ejected, pull it now"
            else:
                console.print(f"  [bold yellow]⚠ EJECT FAILED[/] ({eject_detail}) "
                              "— unmount by hand before pulling")
                title = "Cortex Stick — synced, EJECT FAILED: unmount by hand"
        else:
            console.print("  [green]✓ safe to remove[/] (hashes readback-verified)")
        if notify:
            bits = [f"{len(report.to_host)} in, {len(report.to_stick)} out"]
            if report.facts_to_host or report.facts_to_stick:
                bits.append(f"facts {report.facts_to_host} in / "
                            f"{report.facts_to_stick} out")
            if report.history_to_host or report.history_to_stick:
                bits.append(f"history {report.history_to_host} in / "
                            f"{report.history_to_stick} out")
            if report.brain not in ("not configured", "skipped", "clean"):
                bits.append(f"brain {report.brain}")
            if report.conflicts:
                bits.append(f"⚠ {len(report.conflicts)} conflict(s)")
            if eject_ok is False:
                bits.append(f"eject: {eject_detail[:120]}")
            _notify_desktop(title, "; ".join(bits))
        if eject_ok is False:
            return False
    return True


@stick.command("init")
@click.argument("mount", type=click.Path(exists=True, file_okay=False))
@click.option("--name", default="cortex-stick", help="Human name for this stick.")
@click.option("--encrypt", is_flag=True,
              help="Provision encrypted (AES-256-SIV; prompts for a passphrase).")
@click.option("--passphrase-file", default=None, type=click.Path(exists=True),
              help="Read the passphrase from a file's first line (automation).")
def stick_init_cmd(mount, name, encrypt, passphrase_file):
    """Provision a Cortex Stick at MOUNT (creates <mount>/cortex/)."""
    from agentb.config import load_config
    from agentb.stick import StickError, init_stick, unlock_stick
    passphrase = None
    if encrypt or passphrase_file:
        passphrase = _stick_passphrase(passphrase_file, confirm=True)
    try:
        stick_dir = init_stick(Path(mount), name=name, passphrase=passphrase)
        if passphrase is not None:
            unlock_stick(stick_dir, Path(load_config().data_dir), passphrase)
    except StickError as e:
        console.print(f"[red]{e}[/]")
        raise SystemExit(1)
    console.print(f"[green]✓[/] Provisioned Cortex Stick at [bold]{stick_dir}[/]")
    if passphrase is not None:
        console.print(
            "[green]✓ encrypted[/] — this host is enrolled. On the other "
            "machine, plug in and run: mnemo-cortex stick unlock")
        console.print("[dim]The key never touches the stick. Losing the "
                      "passphrase = losing the courier (your machines keep "
                      "their data).[/]")
    else:
        console.print("[yellow]⚠ PLAINTEXT stick[/] — anyone holding it can "
                      "read everything on it. Recommended: "
                      "mnemo-cortex stick encrypt")
    console.print("[dim]Now run: mnemo-cortex stick sync[/]")


@stick.command("unlock")
@click.argument("mount", required=False)
@click.option("--passphrase-file", default=None, type=click.Path(exists=True),
              help="Read the passphrase from a file's first line (automation).")
def stick_unlock_cmd(mount, passphrase_file):
    """Enroll THIS host on an encrypted stick (one time per host).

    Prompts for the stick passphrase, derives the key, verifies it against
    the stick's passport, and stores it under {data_dir}/stick-keys/ (0600).
    After that, sync/watch on this host need no passphrase.
    """
    from agentb.config import load_config
    from agentb.stick import StickError, load_host_config, unlock_stick
    data_dir = Path(load_config().data_dir)
    host_cfg = load_host_config(data_dir)
    stick_dir = _stick_locate(mount, host_cfg)
    if not stick_dir:
        raise SystemExit(1)
    passphrase = _stick_passphrase(passphrase_file, confirm=False)
    try:
        key_path = unlock_stick(stick_dir, data_dir, passphrase)
    except StickError as e:
        console.print(f"[red]{e}[/]")
        raise SystemExit(1)
    console.print(f"[green]✓ unlocked[/] — key stored at [bold]{key_path}[/]. "
                  "Now run: mnemo-cortex stick sync")


@stick.command("encrypt")
@click.argument("mount", required=False)
@click.option("--passphrase-file", default=None, type=click.Path(exists=True),
              help="Read the passphrase from a file's first line (automation).")
def stick_encrypt_cmd(mount, passphrase_file):
    """Upgrade a PLAINTEXT stick to encrypted, in place.

    Every truth file on the stick (memories, trajectories, pad, conflict
    archive) is rewritten as AES-256-SIV ciphertext; a couriered brain
    converts from a bare repo to an encrypted git bundle. Interrupted runs
    are resumable — run the same command with the same passphrase.

    Other hosts then enroll once with `stick unlock`. Note: replaced
    plaintext may linger in the stick's free space (flash wear leveling) —
    zero-fill the free space if the history matters.
    """
    from agentb.config import load_config
    from agentb.stick import (StickError, encrypt_stick, load_host_config,
                              save_stick_key)
    data_dir = Path(load_config().data_dir)
    host_cfg = load_host_config(data_dir)
    stick_dir = _stick_locate(mount, host_cfg)
    if not stick_dir:
        raise SystemExit(1)
    passphrase = _stick_passphrase(passphrase_file, confirm=True)
    try:
        result = encrypt_stick(stick_dir, passphrase)
        import json as _json
        stick_id = _json.loads(
            (stick_dir / "passport.json").read_text(encoding="utf-8")).get("stick_id", "")
        key_path = save_stick_key(data_dir, stick_id, result["key"])
    except StickError as e:
        console.print(f"[bold red]ENCRYPT REFUSED[/] {e}")
        raise SystemExit(1)
    console.print(
        f"[green]✓ encrypted[/] {result['files_encrypted']} file(s) on "
        f"[bold]{stick_dir}[/] — this host's key stored at {key_path}.")
    console.print("[dim]On the other machine: mnemo-cortex stick unlock. "
                  "Then run a sync here to refresh inventories.[/]")


@stick.command("brain-clone")
@click.argument("dest", type=click.Path())
@click.argument("mount", required=False)
def stick_brain_clone_cmd(dest, mount):
    """Bootstrap the couriered brain repo onto this machine at DEST.

    The encrypted twin of `git clone <stick>/brain/brain.git` — decrypts the
    stick's brain bundle in a temp dir and clones from it. Works on
    plaintext sticks too (clones the bare repo directly).
    """
    from agentb.config import load_config
    from agentb.stick import StickError, clone_brain_from_stick, load_host_config
    data_dir = Path(load_config().data_dir)
    host_cfg = load_host_config(data_dir)
    stick_dir = _stick_locate(mount, host_cfg)
    if not stick_dir:
        raise SystemExit(1)
    try:
        cloned = clone_brain_from_stick(stick_dir, data_dir, Path(dest))
    except StickError as e:
        console.print(f"[red]{e}[/]")
        raise SystemExit(1)
    console.print(f"[green]✓[/] Brain cloned to [bold]{cloned}[/] — set it as "
                  f"brain_repo in {data_dir / 'stick.json'} to courier it.")


@stick.command("sync")
@click.argument("mount", required=False)
@click.option("--agent", "-a", "tenants", multiple=True,
              help="Tenant to sync (repeatable; default: all).")
@click.option("--brain", default=None,
              help="Brain git repo path (or set brain_repo in {data_dir}/stick.json).")
@click.option("--force", is_flag=True,
              help="Override the mass-delete guard. Read the refusal first.")
@click.option("--dry-run", is_flag=True, help="Show the delta, change nothing.")
@click.option("--eject", is_flag=True,
              help="Unmount/eject the stick after a verified sync "
                   "(what `stick watch` does by default).")
def stick_sync_cmd(mount, tenants, brain, force, dry_run, eject):
    """Bidirectional courier sync with the stick (auto-located if no MOUNT)."""
    if not _stick_run_sync(mount, tenants, brain, force, dry_run, eject=eject):
        raise SystemExit(1)


@stick.command("status")
@click.argument("mount", required=False)
def stick_status_cmd(mount):
    """Pending delta in both directions (a dry-run sync) + stick passport."""
    from agentb.config import load_config
    from agentb.stick import load_host_config
    config = load_config()
    host_cfg = load_host_config(Path(config.data_dir))
    stick_dir = _stick_locate(mount, host_cfg)
    if not stick_dir:
        raise SystemExit(1)
    import json as _json
    passport = _json.loads((stick_dir / "passport.json").read_text(encoding="utf-8"))
    manifest = _json.loads((stick_dir / "manifest.json").read_text(encoding="utf-8"))
    console.print(f"[bold]{passport.get('name')}[/] "
                  f"(id {passport.get('stick_id')}, gen "
                  f"{manifest.get('generation')})")
    enc = passport.get("enc")
    if enc:
        state = enc.get("state", "?")
        color = "green" if state == "ready" else "red"
        console.print(f"  [{color}]🔒 encrypted[/] ({enc.get('alg')}, "
                      f"state {state})")
    else:
        console.print("  [yellow]⚠ plaintext[/] — consider: "
                      "mnemo-cortex stick encrypt")
    for hid, meta in sorted(passport.get("hosts", {}).items()):
        age_h = (time.time() - meta.get("last_sync", 0)) / 3600
        console.print(f"  host [bold]{hid}[/] — last sync {age_h:.1f}h ago "
                      f"(gen {meta.get('generation')})")
    console.print("[bold]Pending delta:[/]")
    _stick_run_sync(mount, (), None, False, True)


@stick.command("repair")
@click.argument("mount", required=False)
def stick_repair_cmd(mount):
    """Rebuild the stick's manifest from its actual contents.

    The escape hatch after a mid-sync yank ("TORN GENERATION" refusal):
    accepts what's on the stick as truth, re-hashes everything, clears a
    stale lock. The next sync 3-way-merges from the repaired state — nothing
    on either machine is deleted by the repair itself.
    """
    from agentb.config import load_config
    from agentb.stick import StickError, load_host_config, repair_manifest
    host_cfg = load_host_config(Path(load_config().data_dir))
    stick_dir = _stick_locate(mount, host_cfg)
    if not stick_dir:
        raise SystemExit(1)
    try:
        manifest = repair_manifest(stick_dir)
    except StickError as e:
        console.print(f"[red]{e}[/]")
        raise SystemExit(1)
    console.print(f"[green]✓ repaired[/] — manifest rebuilt over "
                  f"{len(manifest['files'])} file(s), "
                  f"now generation {manifest['generation']}. "
                  "Run: mnemo-cortex stick sync")


@stick.command("watch")
@click.option("--poll", default=15, help="Seconds between stick probes (default 15).")
@click.option("--interval", default=600,
              help="Re-sync period while the stick stays plugged in (default 600).")
@click.option("--notify", is_flag=True,
              help="Desktop toast on each sync and on SYNC REFUSED "
                   "(notify-send / osascript; no-op where unavailable).")
@click.option("--eject/--no-eject", default=True, show_default=True,
              help="Unmount/eject the stick after each verified sync so "
                   "'safe to remove' is literal. --no-eject keeps the stick "
                   "mounted and re-syncs every --interval while it stays in.")
def stick_watch_cmd(poll, interval, notify, eject):
    """Foreground watcher: sync on plug-in, eject when verified.

    Run it under a systemd user unit / Task Scheduler for background courier
    behavior — plug in, it syncs, it ejects; pull out, it waits for the next
    plug-in. With --notify each sync (and any refusal) also raises a desktop
    toast, so the courier is zero-terminal: plug in, watch the corner of the
    screen. Because the default ejects, --interval only matters under
    --no-eject (an ejected stick is no longer found until re-plugged).
    """
    from agentb.config import load_config
    from agentb.stick import find_stick, load_host_config
    host_cfg = load_host_config(Path(load_config().data_dir))
    console.print(f"[bold]Cortex Stick watch[/] — probing every {poll}s "
                  f"(Ctrl-C to stop)")
    present = False
    last_sync = 0.0
    while True:
        stick_dir = find_stick(host_cfg.get("mount_roots") or None)
        if stick_dir and (not present or time.time() - last_sync >= interval):
            console.print(f"[dim]{time.strftime('%H:%M:%S')}[/] "
                          f"stick {'present' if present else 'detected'} — syncing")
            try:
                _stick_run_sync(str(stick_dir), (), None, False, False,
                                notify=notify, eject=eject)
            except Exception as e:   # noqa: BLE001 — the watcher must outlive one bad sync
                # A refusal is a StickError and already toasted inside; this
                # is the unexpected kind (an unreadable host file, a codec
                # dependency gone). Before 4.17.4 it killed the watcher with
                # a traceback nobody saw — the human's only signal was the
                # ABSENCE of a toast. Now: traceback to the log, a toast to
                # the human, and wait for the next plug-in (or `interval`)
                # instead of hammering the same failure every poll.
                console.print_exception()
                console.print(f"[red]sync FAILED:[/] {type(e).__name__}: {e}")
                if notify:
                    _notify_desktop("Cortex Stick — sync FAILED, nothing moved",
                                    f"{type(e).__name__}: {str(e)[:160]}")
            last_sync = time.time()
        if not stick_dir and present:
            console.print(f"[dim]{time.strftime('%H:%M:%S')}[/] "
                          "stick removed — waiting for next plug-in")
        present = bool(stick_dir)
        time.sleep(poll)


@main.command("muse")
@click.option("--agent", "-a", "agents", multiple=True, required=True, help="Agent id (repeatable).")
@click.option("--limit", default=30, help="Session logs to read per agent (default 30).")
def muse_cmd(agents, limit):
    """Audition the Muse (ALWAYS a dry run): print the idea seeds it would extract.

    \b
    Reads each agent's unprocessed session logs through the creative lens and
    prints the notes WITHOUT saving anything or marking sources processed.
    Live extraction runs inside the server maintenance loop once muse.enabled
    is set in agentb.yaml — this command is the review instrument for making
    that call. Safe against a live server: no vec-index or embedder access.
    """
    import asyncio
    from dataclasses import replace

    from agentb.analyst import muse_tenant
    from agentb.config import load_config, get_agent_data_dir
    from agentb.providers import create_resilient_reasoning

    config = load_config()
    muse_cfg = replace(config.muse, max_memories_per_cycle=limit)
    reasoner = create_resilient_reasoning(config.reasoning)  # own instance — never
                                                             # touches the live breaker

    async def _run():
        for agent_id in agents:
            memory_dir = get_agent_data_dir(config, agent_id) / "memory"
            if not memory_dir.is_dir():
                console.print(f"[yellow]No memory dir for '{agent_id}' — skipped.[/]")
                continue
            stats = await muse_tenant(
                agent_id, memory_dir, None, reasoner, None,
                config=muse_cfg, dry_run=True,
            )
            notes = stats.get("notes", [])
            console.print(
                f"\n[bold]🎨 Muse audition — {agent_id}[/] "
                f"(read {stats['scanned']} log(s) → {len(notes)} idea seed(s))"
            )
            for n in notes:
                console.print(f"  [cyan]•[/] {n['summary']}")
                if n["key_facts"]:
                    console.print(f"    [dim]{', '.join(n['key_facts'])}[/]")
            if stats["failed"]:
                console.print(
                    "  [bold red]LLM pass FAILED[/] — this is an error, not a "
                    "zero-ideas result. Check provider config/env (see log lines above)."
                )
            elif not notes and stats["scanned"]:
                console.print("  [dim](no idea seeds in this batch — zero is a valid answer)[/]")
            if not stats["scanned"]:
                console.print("  [dim](no unprocessed session logs to read)[/]")

    asyncio.run(_run())


if __name__ == "__main__":
    main()
