"""Render the stable passport into structured JSON + AL §10 prompt block."""
from __future__ import annotations

from passport import storage


def _iter_active_claims(stable: dict):
    """Yield claim dicts where status == 'active', with section label."""
    core = stable.get("stable_core") or {}
    for section_name, items in core.items():
        if isinstance(items, list):
            for item in items:
                if item.get("status") == "active":
                    yield item, f"stable_core.{section_name}"
    for item in stable.get("negative_constraints") or []:
        if item.get("status") == "active":
            yield item, "negative_constraints"


def _filter_scope(claim: dict, scopes: list[str] | None) -> bool:
    if not scopes:
        return True
    claim_scopes = set(claim.get("scope") or [])
    return bool(claim_scopes.intersection(scopes))


def render_structured(
    scopes: list[str] | None = None,
    platform: str | None = None,  # noqa: ARG001  Phase 1 has no adapter filtering
    max_claims: int = 20,
) -> dict:
    stable = storage.load_stable()
    claims: list[dict] = []
    for item, _section in _iter_active_claims(stable):
        if _filter_scope(item, scopes):
            claims.append({
                "claim_id": item.get("claim_id"),
                "claim": item.get("claim"),
                "type": item.get("type"),
                "scope": item.get("scope"),
                "confidence": item.get("confidence"),
            })
    claims.sort(key=lambda c: -(c.get("confidence") or 0.0))
    claims = claims[:max_claims]

    overlays_raw = stable.get("situational_overlays") or []
    overlays = [
        {
            "overlay_id": o.get("overlay_id"),
            "name": o.get("name"),
            "traits": o.get("traits") or [],
        }
        for o in overlays_raw
        if o.get("status") == "active"
    ]

    return {
        "owner_id": stable.get("owner_id"),
        "passport_version": stable.get("passport_version", "0.1"),
        "claims": claims,
        "overlays": overlays,
    }


def render_prompt_block(
    scopes: list[str] | None = None,
    platform: str | None = None,
    max_claims: int = 20,
) -> str:
    data = render_structured(scopes=scopes, platform=platform, max_claims=max_claims)
    lines: list[str] = []
    if not data["claims"] and not data["overlays"]:
        return "User working-style passport:\n- (no claims on file yet)"
    lines.append("User working-style passport:")
    for c in data["claims"]:
        lines.append(f"- {c['claim']}")
    if data["overlays"]:
        lines.append("")
        lines.append("Active overlay:")
        for o in data["overlays"]:
            traits = ", ".join(o["traits"])
            lines.append(f"- {o['name']}: {traits}")
    return "\n".join(lines)
