# Contributing to Mnemo Cortex

Thanks for wanting to help! Mnemo Cortex is a community project and we welcome contributions of all kinds.

## Quick Links

- **Issues:** https://github.com/GuyMannDude/mnemo-cortex/issues
- **Discussions:** https://github.com/GuyMannDude/mnemo-cortex/discussions

## Ways to Contribute

### Report Bugs
Open an issue with:
- What you expected to happen
- What actually happened
- Your `~/.agentb/agentb.yaml` config (redact API keys!)
- Output of `mnemo-cortex doctor`

### Add a Provider
Want to add support for a new LLM or embedding provider? Great — it's designed for this.

1. Add your provider class to `agentb/providers.py`
2. Inherit from `ReasoningProvider` or `EmbeddingProvider`
3. Implement `generate()` or `embed()` and `health_check()`
4. Add it to `REASONING_MAP` or `EMBEDDING_MAP`
5. Add a config example to `agentb.yaml.example`
6. Write a test in `tests/`

### Add an Integration
Mnemo Cortex works with any MCP-capable agent host. If you use one we don't have an integration for:

1. Create `integrations/your-host/`
2. Write a `README.md` covering install, verify, gotchas, and env vars
3. Add a "Next step" pointer to `THE-LANE-PROTOCOL.md` at the end (matches the rest of the integrations)
4. Open a PR — we'll add it to the README's "Get Started" list

### Improve the Core
The roadmap is in the README. Pick something from the TODO list or propose your own improvement.

## Development Setup

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Initialize config + start the server
mnemo-cortex init
mnemo-cortex start --foreground

# Run tests (in a second terminal)
pytest tests/ -v
```

## Code Style

- Python 3.11+ (type hints, f-strings, pathlib)
- Functions under 50 lines when possible
- Docstrings on public classes and functions
- Tests for new features

## Pull Request Process

1. Fork the repo
2. Create a branch (`git checkout -b feature/my-thing`)
3. Make your changes
4. Run the tests (`pytest tests/ -v`)
5. Open a PR with a clear description of what you changed and why

## Donations

Mnemo Cortex is free and open source. If it helps you, consider supporting the project:
- [GitHub Sponsors](https://github.com/sponsors/GuyMannDude)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

## The Team

- **Guy Hutchins** — Creator, Project Sparks
- **Rocky Moltman** 🦞 — AI agent, chief tester, and the reason this exists
- **Opie (Claude)** — Architecture, code, and the one who never sleeps
- **You?** — We'd love to have you

---

*"Every AI agent has amnesia. Help us fix that."*
