# Install Mnemo Cortex for Claude Desktop on Windows

Mnemo Cortex gives Claude a memory that can survive a closed chat. Claude can save useful facts, decisions, and preferences, then recall them in a later conversation.

This guide connects Claude Desktop to a Mnemo Cortex server. It does not install the server. If you do not have one yet, begin with the [Mnemo Cortex install guide](../README.md#install-guide).

You can ask the Claude Desktop app that is already open to help. Paste this into a chat:

> Help me install Mnemo Cortex from this Windows guide, one step at a time. I will click buttons and enter private tokens myself. Do not ask me to paste a token into chat.

## Step 1: The easy way

The MCP Bundle works without cloning a repository, editing JSON, or installing Node.js.

1. [Download the Mnemo Cortex MCP Bundle](../integrations/claude-desktop/mnemo-cortex.mcpb).
2. Open **Settings > Extensions > Advanced settings** in Claude Desktop.
3. Choose **Install Extension**, select `mnemo-cortex.mcpb`, and approve it.
4. Complete the setup fields:
   - **Mnemo Cortex Server URL**: keep `http://localhost:50001` if the server runs on this PC. For another computer, use its full address, such as `http://192.168.1.50:50001`.
   - **Agent ID**: `claude-desktop` is a good default. Give each app or computer a different ID.
   - **Cross-Agent Sharing**: `separate` searches this agent's memories, `always` searches every agent, and `never` disables cross-agent search.
5. If the server requires an API key, create the token file in [Add authentication](#add-authentication). Never put the key in chat.
6. Quit Claude Desktop from its tray icon and reopen it. Closing only the window does not restart extensions.

The bundle contains its own runtime and dependencies, so this path does not need Node.js.

> Release note: the bundle must be rebuilt from the current bridge before this guide is linked publicly. Until that rebuild is published, use the manual path for an auth-enabled server.

## Prove that memory works

In a new chat, ask:

> Use mnemo_save to remember: My Mnemo Cortex Windows setup works.

Open a second new chat and ask:

> Use mnemo_recall to find what I asked you to remember about my Windows setup.

You should see a Mnemo tool call, not only a promise from Claude. If the second chat recalls the sentence, the connection works.

## Step 2: If you need more control

Use the manual path to inspect the bridge, pin a Git commit, add optional directories, limit tools, or connect to a custom server.

### Install the bridge

You need Claude Desktop, a reachable Mnemo Cortex server, [Node.js](https://nodejs.org/) 18 or newer, and a local copy of the repository.

Check Node.js in PowerShell:

```powershell
node --version
```

If `node` is not recognized, install the current Node.js LTS release and open a new PowerShell window.

Clone and install:

```powershell
Set-Location $env:USERPROFILE
git clone https://github.com/GuyMannDude/mnemo-cortex.git
Set-Location "$env:USERPROFILE\mnemo-cortex\integrations\mcp-bridge"
npm install
```

If Git is unavailable, download the repository ZIP from GitHub and extract it as `%USERPROFILE%\mnemo-cortex`.

Run `npm install` in the bridge directory. Skipping it causes `ERR_MODULE_NOT_FOUND` when Claude loads the bridge.

### Open the Claude Desktop config

The Windows config is:

```text
%APPDATA%\Claude\claude_desktop_config.json
```

You can also use **Settings > Developer > Edit Config**.

Make a backup first:

```powershell
Copy-Item "$env:APPDATA\Claude\claude_desktop_config.json" "$env:APPDATA\Claude\claude_desktop_config.json.backup"
```

Preserve any existing entries under `mcpServers`. Add `mnemo-cortex` beside them rather than replacing the object.

Paste this request to Claude if you want help merging JSON:

> Merge the mnemo-cortex entry below into my Claude Desktop config. Preserve every existing mcpServers entry. Use my real Windows user-folder path, but do not ask me for an API key.

### Add the bridge

Replace `YOUR-NAME` with your Windows user-folder name:

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": [
        "C:/Users/YOUR-NAME/mnemo-cortex/integrations/mcp-bridge/server.js"
      ],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "claude-desktop",
        "MNEMO_SHARE": "separate"
      }
    }
  }
}
```

Point directly at `integrations/mcp-bridge/server.js`. Do not use the old `integrations/openclaw-mcp` path; Windows symlinks are unreliable.

Forward slashes avoid JSON escaping. If you use backslashes, double each one:

```json
"C:\\Users\\YOUR-NAME\\mnemo-cortex\\integrations\\mcp-bridge\\server.js"
```

| Setting | Meaning |
|---|---|
| `command` | Starts Node.js. Use the full path to `node.exe` if it is not on `PATH`. |
| `args` | Points directly to the bridge server file. |
| `MNEMO_URL` | The complete local or remote server URL. |
| `MNEMO_AGENT_ID` | A unique name for this Claude Desktop installation. |
| `MNEMO_SHARE` | `separate`, `always`, or `never`. |

For a remote server, change the URL, for example:

```json
"MNEMO_URL": "http://192.168.1.50:50001"
```

The remote computer must accept connections from this PC. Never expose an unauthenticated server directly to the public internet.

### Add authentication

For a server that requires an API key, the recommended Windows token file is:

```text
%USERPROFILE%\.mnemo-auth-token
```

Create it without putting the token in PowerShell history:

```powershell
$token = Read-Host "Paste the Mnemo Cortex API key"
[IO.File]::WriteAllText("$env:USERPROFILE\.mnemo-auth-token", $token)
Remove-Variable token
```

The bridge reads `MNEMO_AUTH_TOKEN` first, then the token file. `HOME` is often unset on Windows, so it falls back to `USERPROFILE`.

You may instead add a managed token to the config's `env` object:

```json
"MNEMO_AUTH_TOKEN": "YOUR-API-KEY"
```

The file is usually safer because it keeps the secret out of the config. Never commit either secret to Git.

### Optional environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BRAIN_DIR` | set explicitly | Enables brain and session tools when the directory exists. |
| `WIKI_DIR` | set explicitly | Enables legacy wiki tools when present. |
| `DREAM_DIR` | set explicitly | Supplies the latest dream brief when present. |
| `HARNESS_ENABLED_TOOLS` | empty | Comma-separated tool allow-list. Empty registers all available tools. |

Claude Desktop does not expand `%USERPROFILE%` inside arbitrary JSON strings. Use full paths such as `C:/Users/YOUR-NAME/mnemo-plan/brain`.

Save the config, quit Claude Desktop from the tray, reopen it, and run the [proof test](#prove-that-memory-works).

## Windows notes

- Mnemo Cortex runs `node` directly. It does not need `npx`.
- If another MCP server uses `npx`, Windows should launch it through `cmd /c`:

```json
{
  "command": "cmd",
  "args": ["/c", "npx", "-y", "PACKAGE-NAME"]
}
```

A bare `"command": "npx"` commonly fails on Windows.

- Use absolute paths.
- Prefer forward slashes in JSON, or double every backslash.
- Run `npm install` inside `integrations/mcp-bridge`.
- Restart Claude Desktop from the tray after config changes.

## Troubleshooting

### Connection refused or "Mnemo Cortex unreachable"

The server is stopped, the URL is wrong, or a firewall blocks it. Test the health endpoint:

```powershell
Invoke-RestMethod http://localhost:50001/health
```

Use the remote address instead of `localhost` when appropriate.

### 401 Unauthorized

The key is missing or wrong.

- Confirm `%USERPROFILE%\.mnemo-auth-token` exists and contains only the key.
- A configured `MNEMO_AUTH_TOKEN` takes priority over the file.
- Restart Claude Desktop after changing either source.
- Never paste the key into chat or an issue report.

### The bridge does not load

1. Run `node --version`. Manual installs need Node.js 18 or newer.
2. Confirm the server file exists:
   ```powershell
   Test-Path "$env:USERPROFILE\mnemo-cortex\integrations\mcp-bridge\server.js"
   ```
3. Run `npm install` in `integrations/mcp-bridge`.
4. Use the direct `mcp-bridge/server.js` path, not the legacy symlink.
5. Quit Claude Desktop from the tray and reopen it.

The bridge log is:

```text
%APPDATA%\Claude\logs\mcp-server-mnemo-cortex.log
```

Look for `Connected to Mnemo Cortex`, `401`, `unreachable`, `ERR_MODULE_NOT_FOUND`, or a missing Node.js path.

### Claude promises to remember, but no tool runs

A conversational promise is not persistent memory. Explicitly ask for `mnemo_save`, then use `mnemo_recall` in a new chat. If the tools are absent, restart Claude Desktop and inspect the log.

## What to do next

Read the [Session Guide](../SESSION-GUIDE.md) for practical memory habits.

See [Anthropic's local MCP server guide](https://support.anthropic.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop) for Claude Desktop extension controls and the [MCP Bundle project](https://github.com/modelcontextprotocol/mcpb) for the bundle format.
