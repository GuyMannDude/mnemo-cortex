# `robot.info` — v0.2

> A single structured file at a well-known location that gives an AI
> agent a full, authoritative report on a product — so the agent can
> answer a user's questions about it without scraping the website or
> guessing from the README.

> **v0.2 (2026-05-30)** adds an optional **user-facing layer** — plain-language
> sections (`why`, `install_steps`, `works_with`, `extensibility`, `tips`,
> `changelog`, `support`) an AI reads to explain a product to a *non-developer*
> user. v0.1's developer-facing fields (`capabilities`, `exposes`, …) are
> unchanged; the new sections are **purely additive**, so a v0.1 file is a valid
> v0.2 file with the user-facing layer simply absent. See "User-facing layer."

**Companion conventions:** `robot.install` (non-interactive setup),
`llms.txt` ([llmstxt.org](https://llmstxt.org/), LLM-friendly docs
index — `robot.info` cross-references it).

## Why

When a user asks an AI agent *"What's <product>? Does it work with
<their stack>? How do I install it?"*, the agent has three options today:

1. **Guess from training data.** Often stale or wrong.
2. **Scrape the README / homepage.** Slow, format-fragile, easy to misread.
3. **Read a structured manifest if one exists.** Fast, accurate, predictable.

`llms.txt` solves (3) for *documentation* — it gives the agent an index
of clean-text docs to read. `robot.info` solves (3) for *product
identity and capability* — name, version, what it does, what it
exposes, how to install, common Q&A, related products. Different
shape, complementary purpose.

## Where to put it

Two locations, same content:

- **Repository root:** `./robot.info`
- **Project website root:** `https://example.com/robot.info`

Agents look at both. A repo-only file is fine for projects without a
site; a site-only file is fine for closed-source products.

Optionally also expose at `/.well-known/robot.info` for compliance
with [RFC 8615](https://datatracker.ietf.org/doc/html/rfc8615) — same
content, same file.

## Format

`robot.info` is a **single JSON object**, UTF-8 encoded. JavaScript-style
`// line comments` are stripped before parsing — manifests stay
annotatable without sacrificing standard JSON parsers (the same
convention `robot.install` uses).

The top-level keys are listed below. Required keys are **bold**.
Everything else is optional but recommended.

### Identity

- **`robot_info_version`** *(string)* — spec version this file targets, e.g. `"0.2"`.
- **`name`** *(string)* — human-readable product name.
- **`tagline`** *(string)* — one-line pitch (≤ 140 chars).
- `summary` *(string)* — 2-4 sentence description for an agent to paraphrase to a user.
- **`version`** *(string)* — current product version (semver or otherwise).
- `license` *(string)* — SPDX identifier when applicable (e.g. `"MIT"`).
- `homepage` *(URL)* — canonical landing page.
- `source` *(URL)* — source-code repository.
- `contact` *(string)* — maintainer email or URL.
- `maintainer` *(string)* — organization or individual responsible.

### Capability surface

- `capabilities` *(array of strings)* — bullet list of what the product does. One thought per item.
- `exposes` *(object)* — APIs / tools / endpoints the product offers.
  - `rest_api` *(object, optional)* — `{ default_port, base_path, endpoints: [{path, method, purpose}, …] }`
  - `mcp_tools` *(array, optional)* — `[{name, purpose}, …]` for MCP-server products.
  - `cli` *(array, optional)* — `[{command, purpose}, …]` for tools shipped with binaries.
  - Other categories welcome (`graphql`, `grpc`, etc.) — use the same `{purpose, …}` shape.

### Install

- `install` *(object)*
  - `robot_install` *(path)* — relative path to the `robot.install` manifest, or `null` if not supported.
  - `robot_install_sh` *(path)* — relative path to the installer script.
  - `manual_docs` *(URL)* — fallback human-readable install guide.
  - `platforms` *(array of strings)* — e.g. `["linux", "macos", "windows-wsl2"]`.
  - `runtime` *(string)* — language + version requirement, e.g. `"python>=3.11"`.

### Compatibility

- `compatibility` *(object)* — hosts / models / runtimes the product is verified against.
  - `mcp_hosts` *(array)* — for MCP servers: list of compatible client apps.
  - `models` *(string or array)* — model constraints.
  - `protocols` *(array)* — e.g. `["MCP/2025-03-26", "OAuth 2.1"]`.

### Privacy & safety

- `privacy` *(object)*
  - `telemetry` *(string)* — `"none"`, `"opt-in"`, `"required"`, or a URL describing the policy.
  - `data_location` *(string)* — where user data lives by default.
  - `auth` *(string)* — auth model summary.
  - `outbound_calls` *(array)* — list of third-party services the product reaches, with purpose.

### Related products

- `related` *(array of objects)* — `[{name, url, purpose}, …]` for cross-linking sibling products.

### Common questions

- `common_questions` *(array of objects)* — `[{q, a}, …]`. Real questions a user is likely to ask
  the agent about this product. Keep answers tight (1-3 sentences). These are the highest-value
  field of the whole manifest — they're what lets the agent answer without scraping.

### User-facing layer (v0.2)

The v0.1 fields above are written for an agent reasoning about a product. These
sections are written for the **human the agent is helping** — plain language, no
jargon, each one answering a question a real user would ask. All optional; use
the ones that add value and don't merely restate a v0.1 field. A user who
doesn't know what a terminal is should be able to follow along when an AI reads
these aloud.

- `why` *(string)* — plain-language pitch. Read when a user asks *"what is this?"*
  or *"why would I use it?"* No jargon — "saves things your AI should remember,"
  not "FTS5-indexed memory coprocessor."
- `install_steps` *(array of objects)* — `[{step, instruction}, …]`. Numbered,
  written for someone who has never opened a terminal. Describe the *easiest real*
  path, not the most powerful one. Never invent a path that doesn't exist (no
  placeholder `pip install foo` if the package isn't published).
- `works_with` *(array of objects)* — `[{name, how}, …]`. The "how" is the
  value: a one-line "here's how to connect it" per host/platform. Complements
  v0.1 `compatibility` (which lists names without the how).
- `extensibility` *(string)* — read when a user asks *"how do I customize / extend
  this?"* For extensible products; omit when not applicable.
- `tips` *(array of strings)* — the hidden 80%. Things users rarely discover from
  docs. An AI surfaces these *when timely*, not as a feature dump. Keep them
  current — a stale tip ("search is keyword-only") is worse than none.
- `changelog` *(array of objects)* — `[{version, date, changes:[…]}, …]`. The
  most recent few releases in plain language. Read when a user asks *"what's
  new?"* Keeps the agent current without a stale-training-data problem.
- `support` *(object)* — where to get help: `github_issues`, `homepage`, `docs`,
  etc.

### Provenance

- `generated_at` *(ISO 8601 timestamp)* — when this file was last written.
- `spec_url` *(URL, optional)* — link back to this spec doc.

## Linking from `llms.txt`

The [llms.txt spec](https://llmstxt.org/) allows custom H2 sections.
Add `robot.info` and `robot.install` as a sibling section so an agent
that found the site via `llms.txt` also discovers the structured
manifests:

```markdown
## Agent Manifests
- [robot.info](https://example.com/robot.info): Structured product report (this convention)
- [robot.install](https://example.com/robot.install): Non-interactive install manifest
```

Conversely, `robot.info` can reference `llms.txt` by listing it in its
own `related` array under `purpose: "LLM-friendly docs index"`.

## Versioning

`robot_info_version` follows simple semver-ish rules:

- **Patch (`0.1.x`)** — added optional fields, no breaking changes.
- **Minor (`0.x.0`)** — added required fields, deprecated old ones with a grace period.
- **Major (`x.0.0`)** — breaking changes to existing field shapes.

Agents reading the file should tolerate unknown fields (forward
compatibility) and fall back to documented defaults when a field
they expect is absent.

## Validation

A reference validator is not yet published. Until one exists, agents
should:

1. JSON-parse the file (after stripping `//` line comments).
2. Verify `robot_info_version` is present and the major version is
   one the agent supports.
3. Verify required fields are present and string-typed.
4. Tolerate everything else.

## Why not just use `llms.txt`?

`llms.txt` is documentation-shaped — markdown index, prose-friendly,
optimized for an LLM to *read*. `robot.info` is product-shaped — JSON,
structured, optimized for an agent to *act on*. The pair is
deliberate: an agent uses `llms.txt` to read deeply about how a
product works, and uses `robot.info` to answer a user's quick
factual questions without round-tripping through prose.

## Why not just use Schema.org / OpenGraph?

Both are great for *web pages*, weak for *AI-agent products*. A
schema.org `SoftwareApplication` doesn't have fields for "MCP tools
exposed" or "common Q&A pairs" or "compatible LLM hosts." `robot.info`
is opinionated about exactly the shape an agent needs.

Future revisions may add a small JSON-LD adapter so the same data
renders in schema.org form when convenient.

## Where it's in use

- [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) — testbed; first to v0.2.
- [Disco-Bus](https://github.com/GuyMannDude/disco-bus) — v0.2.
- [sparks-widget](https://github.com/GuyMannDude/sparks-widget) (Peter Widget) — v0.2.

Rolling out across the rest of the Project Sparks public products as each
one gets touched.
