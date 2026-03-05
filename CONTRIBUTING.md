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
- Your `agentb.yaml` config (redact API keys!)
- Output of `mnemo-cortex status`

### Add a Provider
Want to add support for a new LLM or embedding provider? Great — it's designed for this.

1. Add your provider class to `agentb/providers.py`
2. Inherit from `ReasoningProvider` or `EmbeddingProvider`
3. Implement `generate()` or `embed()` and `health_check()`
4. Add it to `REASONING_MAP` or `EMBEDDING_MAP`
5. Add a config example to `agentb.yaml.example`
6. Write a test in `tests/test_agentb.py`

### Add a Framework Adapter
Mnemo Cortex works with any agent framework. If you use one we don't have an adapter for:

1. Create `adapters/your-framework/`
2. Add a SKILL.md, hook, or integration guide
3. Document the setup in the adapter README

### Improve the Core
The roadmap is in the README. Pick something from the TODO list or propose your own improvement.

## Development Setup

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Run tests
PYTHONPATH=. pytest tests/ -v

# Run the server locally
cp agentb.yaml.example agentb.yaml
python -m agentb.server
```

## Code Style

- Python 3.10+ (type hints, f-strings, pathlib)
- Functions under 50 lines when possible
- Docstrings on public classes and functions
- Tests for new features (we're at 56 and counting)

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

- **Guy Hoffman** — Creator, Project Sparks
- **Rocky Moltman** 🦞 — AI agent, chief tester, and the reason this exists
- **Opie (Claude)** — Architecture, code, and the one who never sleeps
- **You?** — We'd love to have you

---

*"Every AI agent has amnesia. Help us fix that."*
