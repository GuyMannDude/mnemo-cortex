"""
AgentB Configuration v0.3.0
Multi-tenant isolation, provider fallback chains, persona modes.
"""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


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


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 50001
    cors_origins: list = field(default_factory=lambda: ["*"])
    auth_token: str = ""


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
    server: ServerConfig = field(default_factory=ServerConfig)
    data_dir: str = ""
    log_level: str = "info"
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    personas: dict[str, PersonaConfig] = field(default_factory=dict)


def _resolve_env(value) -> str:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return str(value) if value is not None else ""


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
    if "reasoning" in raw:
        cfg.reasoning = _build_resilient(raw["reasoning"])
    if "embedding" in raw:
        cfg.embedding = _build_resilient(raw["embedding"])
    if "storage" in raw:
        s = raw["storage"]
        cfg.storage = StorageConfig(
            backend=s.get("backend", "json"),
            path=_resolve_env(s.get("path", "")),
            connection_string=_resolve_env(s.get("connection_string", "")),
        )
    if "cache" in raw:
        c = raw["cache"]
        cfg.cache = CacheConfig(**{k: c[k] for k in c if hasattr(CacheConfig, k)})
    if "server" in raw:
        s = raw["server"]
        cfg.server = ServerConfig(
            host=s.get("host", "0.0.0.0"),
            port=s.get("port", 50001),
            cors_origins=s.get("cors_origins", ["*"]),
            auth_token=_resolve_env(s.get("auth_token", "")),
        )
    if "data_dir" in raw:
        cfg.data_dir = _resolve_env(raw["data_dir"])
    if "log_level" in raw:
        cfg.log_level = raw["log_level"]
    cfg.personas = dict(DEFAULT_PERSONAS)
    if "personas" in raw:
        for name, pdata in raw["personas"].items():
            cfg.personas[name] = _build_persona(name, pdata)
    if "agents" in raw:
        for name, adata in raw["agents"].items():
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
