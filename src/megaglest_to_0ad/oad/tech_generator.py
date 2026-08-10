"""Technology JSON generation (``simulation/data/technologies/{civ}/{name}.json``).

Keys are lowercase per the 0.29/Millennium A.D. schema: genericName,
description, cost, requirements, requirementsTooltip, icon, researchTime,
tooltip, modifications, affects. MegaGlest upgrades carry no description
field, so description and tooltip are synthesized from the stat deltas.
Tech references (requirements ``tech``/``all``, Researcher tokens) use the
``{civ}/{name}`` form matching per-civ tech subfolders (Millennium A.D.
convention, e.g. ``"tech": "umay/gather_increase_1"``).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.config import Settings
from ..core.media_conversion import MediaConversionStats
from ..megaglest.civ_loader import Faction, UpgradeDef
from .common import HP_SCALE, SPEED_SCALE, TIME_SCALE, humanize_name, resource_cost
from .mod_builder import sanitize_mod_name


def generate_techs(
    faction: Faction,
    mod_dir: Path,
    stats: MediaConversionStats,
    settings: Settings,
) -> list[Path]:
    """Write one tech per faction upgrade; returns the written JSON paths."""
    del settings  # tech schema is version-stable
    civ = sanitize_mod_name(faction.name)
    written: list[Path] = []
    for name, upgrade in sorted(faction.upgrades.items()):
        payload = _build_tech(civ, name, upgrade, faction.units)
        path = mod_dir / "simulation/data/technologies" / civ / f"{sanitize_mod_name(name)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


def _build_tech(
    civ: str, name: str, upgrade: UpgradeDef, faction_units: dict[str, object]
) -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["genericName"] = humanize_name(name)
    modifications = _modifications(upgrade)
    affects = [f"units/{civ}/{e}" for e in upgrade.effects if e in faction_units]
    payload["description"] = _synthesize_description(name, modifications, affects)
    cost = resource_cost(upgrade.resource_requirements)
    if cost:
        payload["cost"] = cost
    requirements, tooltip = _requirements(civ, upgrade)
    if requirements:
        payload["requirements"] = requirements
        payload["requirementsTooltip"] = tooltip
    if upgrade.image is not None:
        payload["icon"] = f"technologies/{sanitize_mod_name(name)}.png"
    payload["researchTime"] = max(1, round(upgrade.time / TIME_SCALE))
    if modifications:
        payload["modifications"] = modifications
    if affects and modifications:
        payload["affects"] = affects
    payload["tooltip"] = _synthesize_tooltip(modifications, affects)
    return payload


def _modifications(upgrade: UpgradeDef) -> list[dict[str, object]]:
    stats = upgrade.stats
    out: list[dict[str, object]] = []

    max_hp = stats.get("max_hp")
    if isinstance(max_hp, dict):
        pct = float(max_hp.get("start_percentage", 100))
        value = float(max_hp.get("value", 0))
        # MG semantics: value is a percentage bonus relative to the unit's
        # starting HP (``start-percentage`` of the base max).
        out.append({"value": "Health/Max", "multiply": round(pct * (100 + value) / 10000, 3)})

    sight = stats.get("sight")
    if sight:
        out.append({"value": "Vision/Range", "add": float(sight)})

    armor = stats.get("armor")
    if armor:
        for kind in ("Hack", "Pierce", "Crush"):
            out.append({"value": f"Resistance/Entity/Damage/{kind}", "add": float(armor)})

    strength = stats.get("attack_strength")
    if strength:
        out.append({"value": "Attack/Melee/Damage/Hack", "add": float(strength) / HP_SCALE})
        out.append({"value": "Attack/Ranged/Damage/Pierce", "add": float(strength) / HP_SCALE})

    move = stats.get("move_speed")
    if move:
        out.append({"value": "UnitMotion/WalkSpeed", "add": float(move) / SPEED_SCALE})
    return out


def _requirements(civ: str, upgrade: UpgradeDef) -> tuple[dict[str, object] | None, str]:
    techs = list(upgrade.upgrade_requirements)
    entities = list(upgrade.unit_requirements)
    if not techs and not entities:
        return None, ""
    reqs: dict[str, object] = {}
    if len(techs) == 1:
        reqs["tech"] = f"{civ}/{sanitize_mod_name(techs[0])}"
    elif len(techs) > 1:
        reqs["all"] = [f"{civ}/{sanitize_mod_name(t)}" for t in techs]
    if entities:
        reqs["entities"] = [f"units/{civ}/{sanitize_mod_name(e)}" for e in entities]
    parts = [f"Requires {humanize_name(t)}" for t in techs]
    if entities:
        parts.append(f"Requires {humanize_name(entities[0])}")
    return reqs, ". ".join(parts) + "."


def _synthesize_description(
    name: str, modifications: list[dict[str, object]], affects: list[str]
) -> str:
    subject = "units" if affects else "this faction"
    return f"Upgrade for {subject}: {_summarize(modifications) or f'improves {name}.'}"


def _synthesize_tooltip(modifications: list[dict[str, object]], affects: list[str]) -> str:
    if not modifications:
        return "No effect."
    summary = _summarize(modifications) or "Improves unit statistics."
    if affects:
        names = ", ".join(sorted({p.split("/")[-1] for p in affects}))
        return f"{summary}. Affects {names}."
    return summary + ". Affects the whole faction."


def _summarize(modifications: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for mod in modifications:
        path = mod.get("value", "")
        amount = mod.get("add", mod.get("multiply"))
        if path.endswith("Health/Max") and "multiply" in mod:
            parts.append(f"+{int((amount - 1) * 100)}% max health")
        elif path.endswith("Vision/Range"):
            parts.append(f"+{amount} vision range")
        elif "Resistance" in path:
            parts.append(f"+{amount} armor")
        elif "Damage" in path:
            parts.append(f"+{amount} {path.rsplit('/', 1)[-1]} damage")
        elif path.endswith("WalkSpeed"):
            parts.append(f"+{amount} walk speed")
    return ", ".join(parts)
