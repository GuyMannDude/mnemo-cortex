# Install Mnemo Cortex for Claude Desktop on Linux

Mnemo Cortex gives Claude a memory that can survive a closed chat. Claude can save useful facts, decisions, and preferences, then recall them in a later conversation.

This guide connects Claude Desktop to a Mnemo Cortex server. It does not install the server. If you do not have one yet, begin with the [Mnemo Cortex install guide](../README.md#install-guide).

(On Windows? Use the [Windows guide](install-claude-desktop-windows.md) instead.)

You can ask the Claude Desktop app that is already open to help. Paste this into a chat:

> Help me install Mnemo Cortex from this Linux guide, one step at a time. I will click buttons and enter private tokens myself. Do not ask me to paste a token into chat.

## Step 1: The easy way

The MCP Bundle works without cloning a repository, editing JSON, or installing Node.js.

1. [Download the Mnemo Cortex MCP Bundle](../integrations/claude-desktop/mnemo-cortex.mcpb).
2. Open **Settings > Extensions > Advanced settings** in Claude Desktop.
3. Choose **Install Extension**, select `mnemo-cortex.mcpb`, and approve it.
4. Complete the setup fields:
   - **Mnemo Cortex Server URL**: keep `http://localhost:50001` if the server runs on this machine. For another computer, use its full address, such as `http://192.168.1.50:50001`.
   - **Agent ID**: `claude-desktop` is a good default. Give each app or computer a different ID.
   - **Cross-Agent Sharing**: `separate` searches this agent's memories, `always` searches every agent, and `never` disables cross-agent search.
5. If the server requires an API key, enter it in the **API Key** field, or create the token file in [Add authentication](#add-authentication). Never put the key in chat.
6. Quit Claude Desktop fully and reopen it. If your desktop has a tray/indicator icon, quit from there -- closing only the window does not restart extensions.

The bundle contains its own runtime and dependencies, so this path does not need Node.js.

> The bundle ships the current bridge and supports servers that require an API key: enter the key in the **API Key** setup field, or leave it empty and use the token file described in [Add authentication](#add-authentication).

## Prove that memory works

In a new chat, ask:

> Use mnemo_save to remember: My Mnemo Cortex Linux setup works.

Open a second new chat and ask:

> Use mnemo_recall to find what I asked you to remember about my Linux setup.

Claude Desktop asks permission the first time it runs each new tool. Approve the prompt for `mnemo_save`, and again for `mnemo_recall` -- a missed or dismissed prompt looks exactly like memory not working, because Claude quietly tries something else instead.

You should see a Mnemo tool call, not only a promise from Claude. If the second chat recalls the sentence, the connection works.

## Step 2: If you need more control

Use the manual path to inspect the bridge, pin a Git commit, add optional directories, limit tools, or connect to a custom server.

### Install the bridge

You need Claude Desktop, a reachable Mnemo Cortex server, [Node.js](https://nodejs.org/) 18 or newer, and a local copy of the repository.

Check Node.js in a terminal:

```bash
node --version
```

If `node` is not found, install it from your distribution's packages (`apt install nodejs npm`, `dnf install nodejs`, etc.) or with [nvm](https://github.com/nvm-sh/nvm), then open a new terminal.

Clone and install:

```bash
cd ~
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd ~/mnemo-cortex/integrations/mcp-bridge
npm install
```

If Git is unavailable, download the repository ZIP from GitHub and extract it as `~/mnemo-cortex`.

Run `npm install` in the bridge directory. Skipping it causes `ERR_MODULE_NOT_FOUND` when Claude loads the bridge.

### Open the Claude Desktop config

The Linux config is:

```text
~/.config/Claude/claude_desktop_config.json
```

You can also use **Settings > Developer > Edit Config**.

Make a backup first:

```bash
cp ~/.config/Claude/claude_desktop_config.json ~/.config/Claude/claude_desktop_config.json.backup
```

Preserve any existing entries under `mcpServers`. Add `mnemo-cortex` beside them rather than replacing the object.

Paste this request to Claude if you want help merging JSON:

> Merge the mnemo-cortex entry below into my Claude Desktop config. Preserve every existing mcpServers entry. Use my real home-directory path, but do not ask me for an API key.

### Add the bridge

Replace `YOUR-NAME` with your username:

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": [
        "/home/YOUR-NAME/mnemo-cortex/integrations/mcp-bridge/server.js"
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

Point directly at `integrations/mcp-bridge/server.js`, and use the full absolute path -- `~` is not expanded inside the JSON config.

| Setting | Meaning |
|---|---|
| `command` | Starts Node.js. Use the full path to `node` (from `which node`) if Claude Desktop does not inherit your `PATH` -- common when Claude Desktop is launched from the desktop rather than a terminal. |
| `args` | Points directly to the bridge server file. |
| `MNEMO_URL` | The complete local or remote server URL. |
| `MNEMO_AGENT_ID` | A unique name for this Claude Desktop installation. |
| `MNEMO_SHARE` | `separate`, `always`, or `never`. |

For a remote server, change the URL, for example:

```json
"MNEMO_URL": "http://192.168.1.50:50001"
```

The remote computer must accept connections from this machine. Never expose an unauthenticated server directly to the public internet.

### Add authentication

For a server that requires an API key, the recommended token file is:

```text
~/.mnemo-auth-token
```

Create it without putting the token in your shell history:

```bash
read -rs -p "Paste the Mnemo Cortex API key: " MNEMO_KEY && \
  printf '%s' "$MNEMO_KEY" > ~/.mnemo-auth-token && \
  chmod 600 ~/.mnemo-auth-token && unset MNEMO_KEY && echo " saved."
```

The bridge reads `MNEMO_AUTH_TOKEN` first, then the token file.

You may instead add a managed token to the config's `env` object:

```json
"MNEMO_AUTH_TOKEN": "YOUR-API-KEY"
```

The file is usually safer because it keeps the secret out of the config, and `chmod 600` keeps it readable by you alone. Never commit either secret to Git.

### Optional environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BRAIN_DIR` | set explicitly | Enables brain and session tools when the directory exists. |
| `WIKI_DIR` | set explicitly | Enables legacy wiki tools when present. |
| `DREAM_DIR` | set explicitly | Supplies the latest dream brief when present. |
| `HARNESS_ENABLED_TOOLS` | empty | Comma-separated tool allow-list. Empty registers all available tools. |

Use full absolute paths such as `/home/YOUR-NAME/my-brain` -- the config does not expand `~` or environment variables.

Save the config, quit Claude Desktop fully, reopen it, and run the [proof test](#prove-that-memory-works).

## Linux notes

- Use full absolute paths everywhere in the config -- no `~`, no `$HOME`.
- Claude Desktop launched from the desktop may not inherit your shell's `PATH` (nvm setups especially). If the bridge does not load, set `command` to the absolute path from `which node`.
- Quit fully after config changes -- from the tray/indicator icon if your desktop shows one; closing the window alone does not restart MCP servers.
- Keep `~/.mnemo-auth-token` at `chmod 600`.
- Run `npm install` inside `integrations/mcp-bridge`.

## Troubleshooting

### Connection refused or "Mnemo Cortex unreachable"

The server is stopped, the URL is wrong, or a firewall blocks it. Test the health endpoint:

```bash
curl http://localhost:50001/health
```

Use the remote address instead of `localhost` when appropriate.

### 401 Unauthorized

The key is missing or wrong.

- Confirm `~/.mnemo-auth-token` exists and contains only the key.
- A configured `MNEMO_AUTH_TOKEN` takes priority over the file.
- Restart Claude Desktop after changing either source.
- Never paste the key into chat or an issue report.

### The bridge does not load

1. Run `node --version`. Manual installs need Node.js 18 or newer.
2. Confirm the server file exists:
   ```bash
   ls ~/mnemo-cortex/integrations/mcp-bridge/server.js
   ```
3. Run `npm install` in `integrations/mcp-bridge`.
4. Check the `command` path -- desktop-launched Claude may not find `node` on `PATH`; use the absolute path from `which node`.
5. Quit Claude Desktop fully and reopen it.

The bridge log is:

```text
~/.config/Claude/logs/mcp-server-mnemo-cortex.log
```

Look for `Connected to Mnemo Cortex`, `401`, `unreachable`, `ERR_MODULE_NOT_FOUND`, or a missing Node.js path.

### Claude promises to remember, but no tool runs

A conversational promise is not persistent memory. Explicitly ask for `mnemo_save`, then use `mnemo_recall` in a new chat. If the tools are absent, restart Claude Desktop and inspect the log.

Also check tool permissions: each tool starts as "ask" and Claude Desktop prompts on its first use. If a prompt was dismissed, Claude may route around the blocked tool without saying so. Review the extension's permission settings and approve the memory tools.

## What to do next

Have your Claude read [CORTEX-OS.md](../CORTEX-OS.md) -- the agent-side operating manual: the startup ritual, what to save, when to recall. Read the [Session Guide](../SESSION-GUIDE.md) for the human-side memory habits.

See [Anthropic's local MCP server guide](https://support.anthropic.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop) for Claude Desktop extension controls and the [MCP Bundle project](https://github.com/modelcontextprotocol/mcpb) for the bundle format.
