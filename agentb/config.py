"""
AgentB Configuration v0.3.0
Multi-tenant isolation, provider fallback chains, persona modes.
"""

import os
import re
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# agent_id is interpolated into on-disk tenant paths, so it must be a bare
# token — no path separators, no '..', no absolute prefixes. Without this a
# request-supplied agent_id like "../../etc/cron.d/x" or "/etc/cron.d/x"
# escapes the data root (pathlib discards the left operand on an absolute
# right operand). See get_agent_data_dir.
_AGENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def validate_agent_id(agent_id: str) -> str:
    """Return agent_id unchanged if it is a safe tenant token, else raise
    ValueError. Callers building HTTP responses should map this to a 400."""
    if not isinstance(agent_id, str) or not _AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError(
            f"Invalid agent_id: must match [A-Za-z0-9_-] (1-64 chars), "
            f"got {agent_id!r}"
        )
    return agent_id


# session_id is interpolated into session transcript paths the same way
# agent_id is (SessionManager.get_session_transcript). Generated ids look like
# "2026-07-06_121245_a1b2c3"; a bit more headroom than agent_id for long ids.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def validate_session_id(session_id: str) -> str:
    """Return session_id unchanged if it is a safe path token, else raise
    ValueError. Callers building HTTP responses should map this to a 400."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(
            f"Invalid session_id: must match [A-Za-z0-9_-] (1-128 chars), "
            f"got {session_id!r}"
        )
    return session_id


DEFAULT_CONFIG_PATHS = [
    Path("agentb.yaml"),
    Path("agentb.yml"),
    Path.home() / ".config" / "agentb" / "agentb.yaml",
    Path("/etc/agentb/agentb.yaml"),
]


@dataclass
class ProviderConfig:
    provider: str = "ollama"
    model: str = ""
    api_key: str = ""
    api_base: str = ""
    timeout: float = 30.0
    extra: dict = field(default_factory=dict)


@dataclass
class ResilientProviderConfig:
    primary: ProviderConfig = field(default_factory=ProviderConfig)
    fallbacks: list[ProviderConfig] = field(default_factory=list)
    circuit_breaker_threshold: int = 3
    circuit_breaker_cooldown: float = 60.0


@dataclass
class PersonaConfig:
    name: str = "default"
    preflight: str = "balanced"          # aggressive | balanced | permissive
    context_bias: str = "neutral"        # factual | neutral | associative
    max_confidence_for_pass: float = 0.7
    allow_speculative: bool = False
    l1_similarity_override: Optional[float] = None
    l2_similarity_override: Optional[float] = None
    custom_system_prompt: str = ""


DEFAULT_PERSONAS = {
    "default": PersonaConfig(
        name="default", preflight="balanced", context_bias="neutral",
        max_confidence_for_pass=0.7,
    ),
    "strict": PersonaConfig(
        name="strict", preflight="aggressive", context_bias="factual",
        max_confidence_for_pass=0.9, allow_speculative=False,
        l1_similarity_override=0.8, l2_similarity_override=0.6,
        custom_system_prompt=(
            "You are in STRICT mode. Aggressively fact-check all claims. "
            "Flag any unverified numbers, costs, dates, or API references. "
            "Prefer WARN over PASS when uncertain. Enforce concise outputs."
        ),
    ),
    "creative": PersonaConfig(
        name="creative", preflight="permissive", context_bias="associative",
        max_confidence_for_pass=0.5, allow_speculative=True,
        l1_similarity_override=0.6, l2_similarity_override=0.35,
        custom_system_prompt=(
            "You are in CREATIVE mode. The agent is brainstorming or doing creative work. "
            "Do NOT flag speculative ideas as inaccurate. Only WARN on hard contradictions "
            "of known facts. ENRICH with creative associations and related past work."
        ),
    ),
}


@dataclass
class StorageConfig:
    backend: str = "json"
    path: str = ""
    connection_string: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class CacheConfig:
    l1_max_bundles: int = 50
    l1_ttl_seconds: int = 86400
    l1_similarity_threshold: float = 0.75
    l2_similarity_threshold: float = 0.5
    l3_similarity_threshold: float = 0.4
    # v4.1.1: L3 is the disk-walk escape hatch that EMBEDS every prefilter-passing
    # file — O(store size) ollama calls. Harmless when L3 rarely runs, but a
    # session_log-dominated store (cc) whose VEC top-k is all hidden falls through
    # to L3 on every query → 20s, past the bridge timeout. Cap the embeds (recency
    # first) so L3 stays bounded. Interim until vec category-pushdown (#468) keeps
    # session_log out of VEC's top-k so L3 isn't reached at all.
    l3_max_candidates: int = 80
    # #468: category-filtered VEC search over-fetches top_k * this from the kNN
    # then filters by the category column, so a session_log-dominated store still
    # returns enough on-category hits to fill the budget without the L3 disk-walk.
    # Bump for very thin categories (e.g. topology ~5-9% of a store).
    vec_category_overfetch_multiplier: int = 5


@dataclass
class RankingConfig:
    # Composite recall ranking (v4.1). Similarity keeps the majority share so
    # the other signals re-order plausible matches without letting an
    # irrelevant memory win. Set enabled=False for raw tier-order/similarity
    # behavior (pre-v4.1).
    enabled: bool = True
    w_similarity: float = 0.55
    w_recency: float = 0.20
    w_importance: float = 0.15
    w_access: float = 0.10
    recency_half_life_days: float = 30.0


@dataclass
class ClassificationConfig:
    # Smart Ingestion (v4.0). When enabled, /writeback classifies uncategorized
    # memories with the reasoning LLM instead of defaulting to "unknown".
    # Disable to keep the legacy regex-only behavior (zero LLM calls at save time).
    enabled: bool = True
    max_input_chars: int = 1500       # truncate memory text before the classify call
    dreamer_max_per_cycle: int = 200  # cap reclassifications per nightly maintenance pass


@dataclass
class AnalysisConfig:
    # The Analyst (v4.1, Phase 2): periodically distills unprocessed Tier-2
    # session logs into Tier-1 notes. Conservative by design — see analyst.py.
    enabled: bool = True
    interval_cycles: int = 12         # maintenance cycles between passes (~hourly)
    max_memories_per_cycle: int = 30  # session logs read per pass per tenant
    max_batch_chars: int = 12000      # LLM input budget per pass
    per_memory_chars: int = 1200      # truncation per source log
    max_notes_per_batch: int = 10     # hard cap on notes accepted per pass
    dedup_similarity: float = 0.90    # cosine vs nearest existing memory


@dataclass
class MuseConfig:
    # The Muse (v4.8, creative harness): the Analyst's sibling lens — reads the
    # same Tier-2 session logs through a creative lens and emits `idea`
    # memories (connections, what-ifs, inspirations the user voiced). Own
    # muse_processed marker, so both lenses read each log exactly once,
    # independently. Default OFF pending Guy's-Gate review of dry-run output
    # (mnemo-cortex muse --agent <id>). See analyst.py.
    enabled: bool = False
    interval_cycles: int = 24         # maintenance cycles between passes (~2-hourly)
    max_memories_per_cycle: int = 30  # session logs read per pass per tenant
    max_batch_chars: int = 24000      # LLM input budget per pass (riffs are long)
    per_memory_chars: int = 4000      # truncation per source log — MUST be big
                                      # enough to carry the role-aware capture
                                      # budgets (user turns 2000 chars); at the
                                      # Analyst's 1200 the riff body would be
                                      # cut before the creative lens ever reads it
    max_notes_per_batch: int = 6      # ideas are rarer than facts; cap lower
    dedup_similarity: float = 0.90    # cosine vs nearest existing memory


@dataclass
class ExpansionConfig:
    # The Thesaurus Loop (v4.2): query expansion on a WHIFF. The standard recall
    # runs first; only when it comes back empty or weak do we fan the query into
    # a few alternative phrasings (one isolated Flash call), search each, and
    # fuse by max-relevance. Escalation means good searches pay nothing, so
    # default-ON is safe. Disable for the exact pre-v4.2 single-query behavior.
    enabled: bool = True
    # A first pass is a "whiff" when its best hit barely rises above the pack:
    # top_relevance - median_relevance < this. RELATIVE shape, not an absolute
    # score — the v4.3.0 absolute relevance_floor (0.5) sat INSIDE this embedder's
    # compressed noise band (gibberish ~0.50, real on-topic 0.51-0.58, overlapping)
    # and so fired 0× in production. Retuned for IGOR-2's local
    # nomic-embed-text (v4.5.3): that band is even tighter (~0.49-0.62), so
    # measured top-vs-pack gaps run clear-standout on-topic 0.05-0.07, flat-but-
    # on-topic ~0.02, whiff ~0.01. 0.02 catches every true whiff + empty while
    # sparing flat on-topic pools the escalation tax; the near-free false-positive
    # on a uniform pool is still accepted (one Flash call, ~$0.001, max-relevance
    # merge makes the merged result identical to not expanding). (v4.4.0 gap-not-
    # floor; v4.5.3 nomic retune; was relevance_floor.)
    gap_threshold: float = 0.02
    max_variants: int = 4          # alternative phrasings requested from Flash
    # Hard cap on the expansion LLM call; expire → no expansion (graceful). 2.5s:
    # live OpenRouter Flash latency straddles ~1s, and 800ms timed out on exactly
    # the whiffs expansion exists to rescue. A whiff already returned nothing, so
    # spending up to this long to try is worth it; still well under bridge timeout.
    timeout_ms: int = 2500
    min_query_words: int = 3       # skip expansion for short/entity-lookup queries
    model: str = "google/gemini-2.5-flash"  # fast model for phrasing generation (OpenRouter)
    # api_key / api_base default to whatever OpenRouter provider is already
    # configured for reasoning (resolved at server startup); set here only to
    # override. No key anywhere → expansion silently no-ops.
    api_key: str = ""
    api_base: str = ""


# Endpoints a scoped token may be granted. Deliberately a closed set: these are
# exactly the handlers that call _enforce_scope() on the request body's agent_id.
# Granting any other endpoint would hand out access with NO tenant pin, so the
# config loader rejects it at startup rather than trusting a future reviewer to
# notice.
SCOPABLE_ENDPOINTS = frozenset({
    "/context", "/writeback", "/trajectory/save", "/trajectory/recall",
    "/preflight",
})


@dataclass
class ScopedToken:
    """A bearer token pinned to one agent tenant and a subset of endpoints.

    The master server.auth_token keeps full access; scoped tokens exist so a
    single less-trusted caller (a gateway, a shared automation) can never reach
    beyond its own tenant even if its token leaks."""
    token: str
    agent_id: str
    endpoints: list


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 50001
    cors_origins: list = field(default_factory=lambda: ["*"])
    auth_token: str = ""
    scoped_tokens: list = field(default_factory=list)
    # Fail-closed guard: refuse to bind a non-loopback interface with no auth
    # configured. Set true ONLY for a deliberately open deployment behind an
    # external gatekeeper (private network / reverse proxy that adds auth).
    allow_unauthenticated: bool = False
    # Reject request bodies larger than this (DoS guard). Generous default —
    # no legitimate memory write approaches it; it only stops abusive payloads
    # from being embedded/indexed/written to disk. 0 disables the check.
    max_body_bytes: int = 16 * 1024 * 1024


@dataclass
class AgentConfig:
    data_dir: str = ""
    persona: str = "default"
    read_only: bool = False


@dataclass
class AgentBConfig:
    reasoning: ResilientProviderConfig = field(default_factory=ResilientProviderConfig)
    embedding: ResilientProviderConfig = field(default_factory=ResilientProviderConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    muse: MuseConfig = field(default_factory=MuseConfig)
    expansion: ExpansionConfig = field(default_factory=ExpansionConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    data_dir: str = ""
    log_level: str = "info"
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    personas: dict[str, PersonaConfig] = field(default_factory=dict)


def _resolve_env(value) -> str:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return str(value) if value is not None else ""


def _parse_scoped_tokens(entries) -> list:
    """Validate server.scoped_tokens at load time. Every rejection here is a
    request the auth middleware would otherwise mis-handle, so fail LOUD on
    startup instead of quietly at request time."""
    tokens: list[ScopedToken] = []
    for i, e in enumerate(entries or []):
        if not isinstance(e, dict):
            raise ValueError(f"server.scoped_tokens[{i}]: expected a mapping")
        token = _resolve_env(e.get("token", ""))
        agent_id = str(e.get("agent_id") or "").strip()
        endpoints = e.get("endpoints") or []
        if not token:
            # An empty token would "match" requests with no auth header at all.
            raise ValueError(
                f"server.scoped_tokens[{i}]: token is empty (unset env var?)")
        if not agent_id:
            raise ValueError(f"server.scoped_tokens[{i}]: agent_id is required")
        if not isinstance(endpoints, list) or not endpoints:
            raise ValueError(
                f"server.scoped_tokens[{i}]: endpoints must be a non-empty list")
        bad = sorted(set(endpoints) - SCOPABLE_ENDPOINTS)
        if bad:
            raise ValueError(
                f"server.scoped_tokens[{i}]: endpoint(s) {bad} cannot be scoped "
                f"— only {sorted(SCOPABLE_ENDPOINTS)} enforce the agent pin")
        tokens.append(ScopedToken(token=token, agent_id=agent_id,
                                  endpoints=list(endpoints)))
    return tokens


def _build_provider(data: dict) -> ProviderConfig:
    return ProviderConfig(
        provider=data.get("provider", "ollama"),
        model=data.get("model", ""),
        api_key=_resolve_env(data.get("api_key", "")),
        api_base=_resolve_env(data.get("api_base", "")),
        timeout=data.get("timeout", 30.0),
        extra=data.get("extra", {}),
    )


def _build_resilient(data: dict) -> ResilientProviderConfig:
    if "primary" in data:
        primary = _build_provider(data["primary"])
    else:
        primary = _build_provider(data)
    fallbacks = [_build_provider(fb) for fb in data.get("fallbacks", [])]
    return ResilientProviderConfig(
        primary=primary,
        fallbacks=fallbacks,
        circuit_breaker_threshold=data.get("circuit_breaker_threshold", 3),
        circuit_breaker_cooldown=data.get("circuit_breaker_cooldown", 60.0),
    )


def _build_persona(name: str, data: dict) -> PersonaConfig:
    return PersonaConfig(
        name=name,
        preflight=data.get("preflight", "balanced"),
        context_bias=data.get("context_bias", "neutral"),
        max_confidence_for_pass=data.get("max_confidence_for_pass", 0.7),
        allow_speculative=data.get("allow_speculative", False),
        l1_similarity_override=data.get("l1_similarity_override"),
        l2_similarity_override=data.get("l2_similarity_override"),
        custom_system_prompt=data.get("custom_system_prompt", ""),
    )


def load_config(path: Optional[str] = None) -> AgentBConfig:
    config_path = None
    if path:
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
    else:
        env_path = os.environ.get("AGENTB_CONFIG")
        if env_path:
            config_path = Path(env_path)
        else:
            for candidate in DEFAULT_CONFIG_PATHS:
                if candidate.exists():
                    config_path = candidate
                    break

    if not config_path or not config_path.exists():
        return _apply_defaults(AgentBConfig())

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    return _parse_config(raw)


def _parse_config(raw: dict) -> AgentBConfig:
    cfg = AgentBConfig()
    if "reasoning" in raw and raw["reasoning"]:
        cfg.reasoning = _build_resilient(raw["reasoning"])
    if "embedding" in raw and raw["embedding"]:
        cfg.embedding = _build_resilient(raw["embedding"])
    if "storage" in raw and raw["storage"]:
        s = raw["storage"]
        cfg.storage = StorageConfig(
            backend=s.get("backend", "json"),
            path=_resolve_env(s.get("path", "")),
            connection_string=_resolve_env(s.get("connection_string", "")),
        )
    if "cache" in raw and raw["cache"]:
        c = raw["cache"]
        cfg.cache = CacheConfig(**{k: c[k] for k in c if hasattr(CacheConfig, k)})
    if "classification" in raw and raw["classification"]:
        cl = raw["classification"]
        cfg.classification = ClassificationConfig(
            **{k: cl[k] for k in cl if hasattr(ClassificationConfig, k)})
    if "ranking" in raw and raw["ranking"]:
        rk = raw["ranking"]
        cfg.ranking = RankingConfig(
            **{k: rk[k] for k in rk if hasattr(RankingConfig, k)})
    if "analysis" in raw and raw["analysis"]:
        an = raw["analysis"]
        cfg.analysis = AnalysisConfig(
            **{k: an[k] for k in an if hasattr(AnalysisConfig, k)})
    if "muse" in raw and raw["muse"]:
        mu = raw["muse"]
        cfg.muse = MuseConfig(
            **{k: mu[k] for k in mu if hasattr(MuseConfig, k)})
    if "expansion" in raw and raw["expansion"]:
        ex = raw["expansion"]
        ex_kwargs = {k: ex[k] for k in ex if hasattr(ExpansionConfig, k)}
        for sk in ("api_key", "api_base"):
            if sk in ex_kwargs:
                ex_kwargs[sk] = _resolve_env(ex_kwargs[sk])
        cfg.expansion = ExpansionConfig(**ex_kwargs)
    if "server" in raw and raw["server"]:
        s = raw["server"]
        cfg.server = ServerConfig(
            host=s.get("host", "0.0.0.0"),
            port=s.get("port", 50001),
            cors_origins=s.get("cors_origins", ["*"]),
            auth_token=_resolve_env(s.get("auth_token", "")),
            scoped_tokens=_parse_scoped_tokens(s.get("scoped_tokens", [])),
            allow_unauthenticated=s.get(
                "allow_unauthenticated", ServerConfig.allow_unauthenticated),
            # was silently ignored from YAML before v4.1 — the dataclass
            # default always won
            max_body_bytes=s.get("max_body_bytes", ServerConfig.max_body_bytes),
        )
    if "data_dir" in raw:
        cfg.data_dir = _resolve_env(raw["data_dir"])
    if "log_level" in raw:
        cfg.log_level = raw["log_level"]
    cfg.personas = dict(DEFAULT_PERSONAS)
    if "personas" in raw and raw["personas"]:
        for name, pdata in raw["personas"].items():
            if pdata:
                cfg.personas[name] = _build_persona(name, pdata)
    if "agents" in raw and raw["agents"]:
        for name, adata in raw["agents"].items():
            if adata:
                cfg.agents[name] = AgentConfig(
                    data_dir=_resolve_env(adata.get("data_dir", "")),
                    persona=adata.get("persona", "default"),
                    read_only=adata.get("read_only", False),
                )
    return _apply_defaults(cfg)


def _apply_defaults(cfg: AgentBConfig) -> AgentBConfig:
    if not cfg.data_dir:
        cfg.data_dir = str(Path.home() / ".agentb")
    if not cfg.storage.path:
        cfg.storage.path = cfg.data_dir
    p = cfg.reasoning.primary
    if not p.model:
        p.model = "qwen2.5:32b-instruct" if p.provider == "ollama" else "gpt-4o-mini"
    if not p.api_base and p.provider == "ollama":
        p.api_base = "http://localhost:11434"
    e = cfg.embedding.primary
    if not e.model:
        e.model = "nomic-embed-text" if e.provider == "ollama" else "text-embedding-3-small"
    if not e.api_base and e.provider == "ollama":
        e.api_base = "http://localhost:11434"
    for name, persona in DEFAULT_PERSONAS.items():
        if name not in cfg.personas:
            cfg.personas[name] = persona
    return cfg


def get_agent_data_dir(cfg: AgentBConfig, agent_id: Optional[str] = None) -> Path:
    if agent_id is not None:
        validate_agent_id(agent_id)
    if agent_id and agent_id in cfg.agents:
        agent_cfg = cfg.agents[agent_id]
        if agent_cfg.data_dir:
            return Path(agent_cfg.data_dir)
        return Path(cfg.data_dir) / "agents" / agent_id
    elif agent_id:
        return Path(cfg.data_dir) / "agents" / agent_id
    return Path(cfg.data_dir) / "agents" / "default"


def get_persona(cfg: AgentBConfig, persona_name: Optional[str] = None,
                agent_id: Optional[str] = None) -> PersonaConfig:
    if persona_name and persona_name in cfg.personas:
        return cfg.personas[persona_name]
    if agent_id and agent_id in cfg.agents:
        agent_persona = cfg.agents[agent_id].persona
        if agent_persona in cfg.personas:
            return cfg.personas[agent_persona]
    return cfg.personas.get("default", DEFAULT_PERSONAS["default"])


