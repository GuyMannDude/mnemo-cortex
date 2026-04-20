# 🦺 Passport Lane

```
    🚧  UNDER CONSTRUCTION  🚧
    ═══════════════════════════
    Hard hats on. We're building.
    ═══════════════════════════
```

> **Hey, give us a day or two** — *good code takes time. Yours will be done in milliseconds.* ⚡
>
> Passport Lane is a feature inside Mnemo Cortex. The foundation is poured
> (Phase 1.5 shipped ✅), but the walls are still going up. The `/passport/*`
> routes respond, but real classifier intelligence and human review UI are
> still in the oven. Treat this as **alpha**.
>
> 💡 **Good news for Mnemo Cortex users:** installing Mnemo does NOT auto-enable
> Passport. The regular memory flow (`mnemo_save`, `mnemo_search`, `mnemo_recall`)
> doesn't touch Passport Lane at all. The routes only fire when you call them
> explicitly. So if you're here for memory, you're not going to trip over
> construction equipment.

---

## 🎯 What Passport Lane Is

A **portable behavioral identity layer** inside Mnemo Cortex. It stores how
you think, talk, and work — your style, preferences, tolerances — and makes
that profile available to any MCP-connected AI at session start.

**Mnemo Cortex remembers what happened. Passport remembers who you are.**

Four tools. That's the whole API:

| Tool | What it does |
|---|---|
| `get_user_context` | Any AI reads your behavioral profile at session start |
| `observe_behavior` | Any AI writes a structured, evidence-cited observation |
| `update_preference` | Set an explicit preference (with user sign-off) |
| `forget_or_override` | Mark a pattern as wrong. Audit preserved. |

---

## 🏗️ Build Status

| Layer | State |
|---|---|
| Phase 1 — skeleton (routes, storage, git commits, basic validation) | ✅ Shipped |
| Phase 1.5 — full-observation scan, per-evidence provenance, 32 detectors | ✅ Shipped |
| Phase 2 — real classifier cascade (nano → mini → full) | 🚧 Not built |
| Phase 3 — human review UI | 🚧 Not built |
| Chrome extension | 🚧 Not built |
| Per-user private identity repo sync | 🚧 Not built |
| Home / work passport split | 🚧 Not built |
| Eval corpus + tuning loop | 🦞 Corpus landed, loop parked |

**Road map vibe:** normal humans first, enterprise later. Get Passport
feeling good for one person with their own data before we worry about
SSO, compliance classifiers, and audit retention.

---

## 🚦 Should You Poke At It?

### 🔴 Probably skip for now if…
- You need production-grade behavioral identity **today**
- You need enterprise compliance (SOC 2, HIPAA, SSO)
- You need a polished UI for reviewing pending observations
- You need the classifier to make nuanced calls on mixed-sensitivity content

### 🟢 Poke around if…
- You're curious how portable AI identity could actually work
- You want to read the detector and policy design
- You're evaluating Mnemo Cortex and wondering where it's headed
- You're willing to file issues on rough edges (we'll love you for it)

---

## 🧰 For Developers

**Spec:** lives in Guy's private `sparks-brain-guy` repo. Ask if you're collaborating.

**Detectors:** `detectors/` — named registry, YAML-toggleable severity, 32 shipped.

**Policy:** four YAML files in `~/.mnemo/passport/` — `policy.yaml`,
`detectors.yaml` (repo-safe), plus `denylist.local.yaml` and
`redaction_map.local.yaml` (user-owned, gitignored).

**Eval corpus:** `../tests/passport/corpus/` — 200 labeled examples. 48%
baseline accuracy against the current validator, which means the tuning
loop has work to do. (That's the point.)

---

## 🧬 Why Lives Here Instead of Its Own Repo

Packaging decision made 2026-04-17: Passport is a **feature** of Mnemo Cortex,
not a sibling product. One install, one binary, one brand. "A Mnemo in every
Bot." ™️

Public code, public spec. The behavioral data a user would eventually store
via Passport is designed to live in a **separate per-user private GitHub
repo** — that's not built yet, but the design is clear: your identity stays
yours.

---

*Part of [Mnemo Cortex](../README.md). Same MIT license. Same install.
Same crustacean energy. 🦞*

*🚧 → ✅ coming soon.*
