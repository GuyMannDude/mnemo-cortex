# 🪪 Developer's Passport

> A reference-grade safety layer for ingesting user behavioral claims into an agent's context.
>
> Five MCP tools. A review queue. 32 detectors. Evidence-grounded promotion.
> Everything an AI needs to learn how you work — without letting a bad claim
> sneak in through the back door.

**Status: beta.** The machinery is real and the 200-entry eval corpus shows
it working. This release is named **Developer's Passport** on purpose: it's
aimed at developers building agent systems who want a known-good pattern for
safe behavioral-claim ingestion. When the browser + hosted story is ready for
normal users, the name loses the possessive. Today, it's for devs.

---

## 🎯 What This Gives You

**Mnemo remembers what happened. Passport remembers who the user is.**

A developer dropping Passport into their stack gets:

- **5 MCP tools** for reading and writing a user's working-style profile
- **A review queue** — nothing lands in the stable profile without an explicit
  promote, so the user is always the gate
- **32 detectors** screening every incoming observation (secrets, PII, prompt
  injection, generic fluff, duplicates)
- **Provenance buckets** (`trusted_local`, `trusted_curated_import`,
  `semi_trusted_remote`, `untrusted_web`) that drive a policy layer resolving
  every observation into one of four dispositions: `allow`, `review_required`,
  `local_only`, `hard_block`
- **Git-tracked audit** — every promote, forget, override is a commit with a
  reason
- **A portable YAML file** (`passport_shared_behavior.yaml`) that's the
  shareable version of the profile — what an external AI actually consumes

---

## 🧰 The Five Tools

| Tool | What it does |
|---|---|
| `passport_get_user_context` | Read the user's stable profile at session start. Returns a prompt block the AI can consume plus structured claims. |
| `passport_observe_behavior` | Propose a new behavioral claim with evidence rows. Never auto-lands — routes to the pending queue with a disposition. |
| `passport_list_pending_observations` | Show what's queued for review. |
| `passport_promote_observation` | User signs off on a pending observation; it becomes a stable claim. |
| `passport_forget_or_override` | Mark a stable claim as wrong. Audit preserved; the original never disappears from history. |

All five are exposed over MCP via the reference integration at
[`integrations/openclaw-mcp/server.js`](../integrations/openclaw-mcp/server.js).
The Python-side REST API lives under `/passport/*` and is mounted by
`agentb_bridge` (see that repo for the HTTP host layer).

---

## 🚀 5-Minute Dev Quickstart

```bash
# 1. Clone and install mnemo-cortex
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex
pip install -e .

# 2. First run creates the config skeleton at ~/.mnemo/passport/
python3 -c "from passport import config; config.load_policy(); print('ok')"
ls ~/.mnemo/passport/
#   policy.yaml  detectors.yaml  denylist.local.yaml  redaction_map.local.yaml

# 3. Observe a test behavior (directly via Python — or call the MCP tool)
python3 <<'PY'
from passport import pending, validation, storage
from passport.models import Observation, Evidence

obs = Observation(
    observation_id="obs_test",
    proposed_claim="Prefers concise bullet summaries over prose",
    type="preference",
    scope=[],
    confidence=0.7,
    proposed_target_section="stable_core.communication",
    source_platform="cc",
    source_session_id="quickstart-001",
    evidence=[
        Evidence(evidence_id="ev1", session_id="quickstart-001",
                 turn_ref="t-1", excerpt="user: just give me the bullets"),
        Evidence(evidence_id="ev2", session_id="quickstart-001",
                 turn_ref="t-2", excerpt="user: skip the preamble"),
    ],
)
stable = storage.load_stable()
vr = validation.validate_observation(obs, stable)
print("disposition:", vr.disposition, "| reasons:", vr.reason_codes)
PY

# 4. (Optional) Run the eval harness against the shipped corpus
#    The 200-entry labeled corpus is held separately — it contains
#    detector-bait tokens that trip public secret scanners. Open an
#    issue if you want access for tuning your own policy.
python3 -m tests.passport.corpus_score
```

What you should see in step 3: `disposition: allow` with the observation
landing in the pending queue. Step 4 requires the eval corpus (see the
comment); when present, it reports accuracy + macro-F1 across 200 labeled
examples spanning benign, toxic, edge, and adversarial cases.

---

## 📊 Current Eval Numbers

Against the shipped 200-entry corpus in `tests/passport/corpus/`:

| Metric | Value |
|---|---|
| Overall accuracy | **53.0%** |
| Macro-F1 | **0.458** |
| F1(`hard_block`) | 0.771 |
| F1(`allow`) | 0.520 |
| F1(`review_required`) | 0.422 |
| F1(`local_only`) | 0.118 |

Per-file: benign 52%, toxic 68%, edge 32%, adversarial 60%. The `hard_block`
protection for actually-dangerous content is strong; the `local_only` class
is where the current policy is weakest. These are the honest numbers — the
tuning loop is genuinely open for contributions.

---

## 🔧 Configuration

Four YAML files in `$MNEMO_PASSPORT_DIR` (default `~/.mnemo/passport/`):

| File | Purpose | Sync policy |
|---|---|---|
| `policy.yaml` | Rules, bucket defaults, disposition map | Repo-safe |
| `detectors.yaml` | Enabled detector IDs + severity overrides | Repo-safe |
| `denylist.local.yaml` | Private nouns (client names, internal domains) | Never synced |
| `redaction_map.local.yaml` | Noun→category mappings for safe redaction | Never synced |

Defaults are embedded in `passport/config.py`. First access writes a
skeleton; after that, the user edits by hand. `config.reload()` clears the
caches after edits so you don't need a restart.

---

## 🚧 Known Gaps

Honest list, not a roadmap:

- **No Phase 2 classifier.** The current validator is deterministic rule +
  detector logic. The Karpathy-loop tuning harness is there; the learned
  classifier cascade isn't built.
- **No hosted HTTP MCP wrapper.** Today's integration is stdio subprocess
  (`openclaw-mcp/server.js`). A public streamable-HTTP MCP server speaking
  to claude.ai / ChatGPT custom connectors is a future release.
- **No review UI.** Pending observations are reviewed via `list_pending` +
  `promote_observation` tool calls. A web review panel is a future add.
- **`local_only` F1 is weak (0.118).** The policy change that boosted
  `allow` and `review_required` cost us here — see the tradeoffs in the eval
  output. An observation that a detector catches as sensitive is still
  correctly routed; this is about the *default-disposition* edges.
- **Per-user private repo sync.** The design slot exists (portable profile is
  separate from the raw store), but the automated push-to-user-repo isn't
  built. Manual export works today.

---

## 🧪 For Contributors

- **Detectors:** `detectors/` — named registry, YAML-toggleable severity.
  Adding a detector is a one-file change plus a policy-map entry.
- **Eval corpus:** `../tests/passport/corpus/` — 200 labeled entries across
  `benign.yaml`, `toxic.yaml`, `edge.yaml`, `adversarial.yaml`. Proposing a
  policy tweak? Run `corpus_score.py` and report the delta.
- **Reference MCP integration:** `../integrations/openclaw-mcp/server.js` —
  the cleanest example of how to expose Passport tools to an MCP client.

---

## 🧬 Why This Lives Inside Mnemo Cortex

Packaging decision from 2026-04-17: Passport is a **feature** of Mnemo, not
a sibling product. One install, one binary, one brand. The behavioral data
lives locally by design — a portable profile is a separate export, not a
sync-to-cloud coupling.

Public code, public spec. Your identity stays yours.

---

*Part of [Mnemo Cortex](../README.md). Same MIT license. Same install.
Same crustacean energy. 🦞*
