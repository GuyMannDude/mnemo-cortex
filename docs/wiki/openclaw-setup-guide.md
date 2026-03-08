# OpenClaw Setup Guide — Paste-and-Go
## For people who just want to type and have it work

---

## BEFORE YOU START

You need:
- A computer (Mac, Linux, or Windows with WSL)
- A terminal app open
- An API key from ONE of: Anthropic, OpenAI, Google, or OpenRouter
- 10 minutes

---

## STEP 1: Check if Node.js is installed

Paste this into your terminal:

```bash
node --version
```

**If you see `v22.x.x` or higher:** Skip to Step 3.

**If you see `v18.x.x` or `v20.x.x`:** You need to upgrade. Go to Step 2.

**If you see `command not found`:** Go to Step 2.

---

## STEP 2: Install Node.js 22

### Mac:
```bash
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.bashrc
nvm install 22
nvm use 22
node --version
```

### Ubuntu/Debian Linux:
```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node --version
```

### Windows WSL:
```bash
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.bashrc
nvm install 22
nvm use 22
node --version
```

**You should see `v22.x.x`. If you do, move on.**

---

## STEP 3: Install OpenClaw

Paste this:

```bash
npm install -g openclaw@latest
```

Wait for it to finish. Then verify:

```bash
openclaw --version
```

**You should see a version number like `2026.3.2`. If you do, move on.**

---

## STEP 4: Run the Setup Wizard

This is the big one. Paste this:

```bash
openclaw onboard --install-daemon
```

The wizard will walk you through several questions. Here's what to expect:

### Question: "Choose your AI provider"
Pick whichever you have an API key for:
- **Anthropic** — if you have a Claude API key (starts with `sk-ant-`)
- **OpenAI** — if you have an OpenAI key (starts with `sk-`)
- **Google** — if you have a Gemini key (starts with `AI`)
- **OpenRouter** — if you want access to multiple models (starts with `sk-or-`)

### Question: "Enter your API key"
Paste your API key when asked. It won't show on screen — that's normal, it's hidden for security.

### Question: "Install as a system service?"
Say **yes**. This makes OpenClaw start automatically when your computer boots.

### Question: "Enable recommended hooks?"
Say **yes**. These give you session memory and logging.

**When the wizard finishes, move on.**

---

## STEP 5: Verify It's Running

Paste this:

```bash
openclaw status
```

**You should see something like:**
```
Service: systemd (enabled)
Gateway: running
```

If it says "running" — you're done with the backend.

**If it says "stopped" or errors out:**
```bash
openclaw gateway start
openclaw status
```

---

## STEP 6: Open the Web Chat

Paste this to find your dashboard URL:

```bash
echo "Open this in your browser: http://127.0.0.1:18789/"
```

Open that URL in your web browser. You should see the OpenClaw chat interface.

Type "Hello" and hit enter. If your AI responds — **you're fully operational.**

---

## STEP 7: Connect Your Browser (Optional but Recommended)

This lets OpenClaw control a browser tab for web research and automation.

1. Install the OpenClaw Chrome extension from the Chrome Web Store
   (search "OpenClaw" or ask your AI to give you the link)

2. Click the extension icon in Chrome

3. Set the port to `18789`

4. Enter your gateway token. Find it with:
```bash
grep -i "token" ~/.openclaw/openclaw.json | head -3
```

5. Click Save. The extension should show "Connected."

---

## STEP 8: Give Your AI a Name and Personality (Optional)

OpenClaw reads special files from its workspace to know who it is.
Create a personality:

```bash
cat > ~/.openclaw/workspace/SOUL.md << 'EOF'
# Who You Are

You're not a chatbot. You're a capable assistant with opinions.

Be genuinely helpful — skip the "Great question!" filler.
Have opinions. Disagree when you think something is wrong.
Be resourceful — try to figure things out before asking.
Be concise when needed, thorough when it matters.

Each session you wake up fresh. Your memory files are how you persist.
Read them. Update them. They're how you remember.
EOF
```

And a memory file:

```bash
mkdir -p ~/.openclaw/workspace/memory
cat > ~/.openclaw/workspace/MEMORY.md << 'EOF'
# Long-Term Memory

This file is where you store important facts, decisions, and context
that should survive across sessions. Update it as you learn.

## About My Human
- Name: [fill this in]
- Projects: [fill this in]
EOF
```

---

## TROUBLESHOOTING

### "openclaw: command not found"
```bash
# Check if npm installed it somewhere weird
npm list -g openclaw
# Try with npx instead
npx openclaw onboard --install-daemon
```

### "Gateway won't start"
```bash
openclaw gateway stop
sleep 3
openclaw gateway start
openclaw status
```

### "AI not responding in chat"
```bash
# Check if your API key is set
openclaw secrets list
# Re-enter it if needed
openclaw secrets configure
```

### "Port 18789 already in use"
```bash
# Something else is using the port. Find it:
lsof -i :18789
# Kill it:
kill $(lsof -ti :18789)
# Start again:
openclaw gateway start
```

### "I want to change my AI model"
```bash
# Open the config
nano ~/.openclaw/openclaw.json
# Find the "models" section and change the model ID
# Save and restart:
openclaw gateway restart
```

---

## WHAT'S NEXT

Once you're running:

1. **Type `/new` in the chat** to start a fresh session anytime
2. **Enable session memory:** `openclaw hooks enable session-memory`
   This saves your conversations so the AI remembers between sessions
3. **Check out skills:** Your AI can install tools and abilities
   Just tell it: "What skills do you have?"
4. **Join the community:** OpenClaw Discord and GitHub for help and ideas

---

## THE QUICK VERSION (for impatient people)

If you just want the minimum commands:

```bash
npm install -g openclaw@latest
openclaw onboard --install-daemon
# answer the wizard questions
openclaw status
# open http://127.0.0.1:18789/ in your browser
# type "Hello" — if it responds, you're done
```

That's it. Five commands and a browser tab. You're running your own AI assistant.

---

Created by the Project Sparks crew — Guy, Rocky 🦞, and Opie ⚡
https://projectsparks.ai
