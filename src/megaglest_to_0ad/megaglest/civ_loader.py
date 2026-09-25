"""Load MegaGlest factions, units and upgrades into typed models.

The loader is tolerant by design: it reads known fields defensively and
collects anything unrecognized into ``unmapped`` lists (logged, not fatal).
Asset references are resolved against the pack, honoring ``$COMMONDATAPATH``
and ``$TECHSPATH`` macros; existence checks happen in the asset inventory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import PackStructureError, ParseError
from ..utils.constants import LOADING_SCREEN_GLOBS
from ..utils.file_utils import resolve_pack_path
from .parser import MegaglestPack
from .xmlutil import XmlNode, parse_xml

LOGGER = logging.getLogger(__name__)

_SOUND_ENABLED_TRUE = {"true", "1"}


@dataclass
class AttackStats:
    """Attack skill parameters."""

    strength: float = 0.0
    variance: float = 0.0
    range: float = 0.0

    attack_type: str = ""
    start_time: float = 0.0
    fields: list[str] = field(default_factory=list)
    projectile: bool = False
    projectile_particle: Path | None = None
    splash_radius: float = 0.0
    splash_particle: Path | None = None
    sounds: list[Path] = field(default_factory=list)


def _resolve_cancel_image(base: Path, ref: str, macros: dict[str, Path]) -> Path:
    """Resolve an ``image-cancel`` reference with a faction-root fallback.

    The megapack ships upgrade icons one level above where its own XMLs
    point (``../../../cancel.bmp`` from ``factions/<civ>/upgrades/<u>/``
    lands on ``factions/cancel.bmp``, but the file lives at
    ``factions/<civ>/cancel.bmp``). Prefer the exact path; when it is
    missing, fall back to the same basename in the faction directory —
    mirroring the engine's multi-root search.
    """
    resolved = resolve_pack_path(base, ref, macros)
    if not resolved.exists():
        fallback = base.parents[1] / Path(ref).name
        if fallback.exists():
            LOGGER.debug("cancel image fallback: %s -> %s", resolved, fallback)
            return fallback
    return resolved

@dataclass
class SkillDef:
    """A single MegaGlest skill (move/attack/build/harvest/die/...)."""

    type: str
    name: str
    ep_cost: float = 0.0
    speed: float = 0.0
    anim_speed: float = 0.0
    animation: Path | None = None
    animation_ref: str | None = None
    sounds: list[Path] = field(default_factory=list)
    particles: list[Path] = field(default_factory=list)
    attack: AttackStats | None = None
    unmapped: list[str] = field(default_factory=list)


@dataclass
class CommandDef:
    """A command (train/attack/move/morph) exposed on a unit."""

    type: str
    name: str
    image: Path | None = None
    skill_refs: dict[str, str] = field(default_factory=dict)
    produced_unit: str | None = None
    produced_upgrade: str | None = None
    morph_unit: str | None = None
    discount: float = 0.0
    requirements: list[str] = field(default_factory=list)


@dataclass
class UnitDef:
    """A MegaGlest unit or building."""

    name: str
    directory: Path
    xml_path: Path
    is_building: bool = False
    is_flying: bool = False
    parameters: dict[str, Any] = field(default_factory=dict)
    parameters_raw: dict[str, Any] = field(default_factory=dict)
    skills: dict[str, SkillDef] = field(default_factory=dict)
    commands: list[CommandDef] = field(default_factory=list)
    image: Path | None = None
    image_cancel: Path | None = None
    selection_sounds: list[Path] = field(default_factory=list)
    command_sounds: list[Path] = field(default_factory=list)
    unmapped_parameters: list[str] = field(default_factory=list)


@dataclass
class UpgradeDef:
    """A MegaGlest upgrade (technology)."""

    name: str
    directory: Path
    xml_path: Path
    time: float = 0.0
    image: Path | None = None
    image_cancel: Path | None = None
    unit_requirements: list[str] = field(default_factory=list)
    upgrade_requirements: list[str] = field(default_factory=list)
    resource_requirements: dict[str, int] = field(default_factory=dict)
    effects: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)


@dataclass
class Faction:
    """A MegaGlest faction: metadata plus its units and upgrades."""

    name: str
    directory: Path
    xml_path: Path
    loading_screen: Path | None = None
    music: Path | None = None
    starting_resources: dict[str, int] = field(default_factory=dict)
    starting_units: list[tuple[str, int]] = field(default_factory=list)
    ai_behavior: dict[str, dict[str, int]] = field(default_factory=dict)
    units: dict[str, UnitDef] = field(default_factory=dict)
    upgrades: dict[str, UpgradeDef] = field(default_factory=dict)
    particles: list[Path] = field(default_factory=list)


def load_faction(pack: MegaglestPack, faction_dir: Path) -> Faction:
    """Load one faction from its directory inside the pack."""
    name = faction_dir.name
    xml_path = _entity_xml(faction_dir, name, "faction")
    root = parse_xml(xml_path)
    if root.tag != "faction":
        LOGGER.warning(
            "Faction XML root is <%s>, expected <faction>", root.tag, extra={"faction": name}
        )

    faction = Faction(name=name, directory=faction_dir, xml_path=xml_path)
    macros = pack.macro_map()

    for child in root.children:
        tag = child.tag
        if tag == "starting-resources":
            faction.starting_resources = _named_amounts(child)
        elif tag == "starting-units":
            faction.starting_units = _named_amount_pairs(child)
        elif tag == "ai-behavior":
            for group in child.children:
                faction.ai_behavior[group.tag] = _named_amounts(group)
        elif tag == "music":
            ref = child.path_attr()
            if ref:
                faction.music = resolve_pack_path(faction_dir, ref, macros)
        elif tag == "commander":
            # Present in some packs; no mapping yet.
            pass

    faction.loading_screen = _find_loading_screen(faction_dir)

    units_dir = faction_dir / "units"
    for directory in _entity_dirs(units_dir):
        unit = load_unit(pack, directory)
        faction.units[unit.name] = unit
        if unit.unmapped_parameters:
            LOGGER.debug(
                "Unmapped unit parameters",
                extra={"unit": unit.name, "tags": unit.unmapped_parameters},
            )

    upgrades_dir = faction_dir / "upgrades"
    for directory in _entity_dirs(upgrades_dir):
        upgrade = load_upgrade(pack, directory)
        faction.upgrades[upgrade.name] = upgrade

    particles_dir = faction_dir / "particles"
    if particles_dir.is_dir():
        faction.particles = sorted(particles_dir.glob("*.xml"))

    LOGGER.info(
        "Loaded faction",
        extra={
            "faction": name,
            "units": len(faction.units),
            "upgrades": len(faction.upgrades),
            "particles": len(faction.particles),
        },
    )
    return faction


def load_unit(pack: MegaglestPack, unit_dir: Path) -> UnitDef:
    """Load one unit/building from its directory."""
    name = unit_dir.name
    xml_path = _entity_xml(unit_dir, name, "unit")
    root = parse_xml(xml_path)
    if root.tag != "unit":
        LOGGER.warning("Unit XML root is <%s>, expected <unit>", root.tag, extra={"unit": name})

    unit = UnitDef(name=name, directory=unit_dir, xml_path=xml_path)
    macros = pack.macro_map()

    for child in root.children:
        tag = child.tag
        if tag == "parameters":
            (
                unit.parameters,
                unit.parameters_raw,
                unit.unmapped_parameters,
            ) = _parse_parameters(child, unit_dir, macros)
        elif tag == "skills":
            unit.skills = _parse_skills(child, unit_dir, macros)
        elif tag == "commands":
            unit.commands = _parse_commands(child, unit_dir, macros)
        else:
            LOGGER.debug("Unmapped unit section", extra={"unit": name, "tag": tag})

    unit.image = unit.parameters.get("image")
    unit.image_cancel = unit.parameters.get("image_cancel")
    unit.selection_sounds = unit.parameters.get("selection_sounds", [])
    unit.command_sounds = unit.parameters.get("command_sounds", [])
    unit.is_building = _classify_building(unit)
    unit.is_flying = "air" in (unit.parameters.get("fields") or [])
    return unit


def load_upgrade(pack: MegaglestPack, upgrade_dir: Path) -> UpgradeDef:
    """Load one upgrade from its directory."""
    name = upgrade_dir.name
    xml_path = _entity_xml(upgrade_dir, name, "upgrade")
    root = parse_xml(xml_path)
    if root.tag != "upgrade":
        LOGGER.warning(
            "Upgrade XML root is <%s>, expected <upgrade>", root.tag, extra={"upgrade": name}
        )

    upgrade = UpgradeDef(name=name, directory=upgrade_dir, xml_path=xml_path)
    macros = pack.macro_map()

    for child in root.children:
        tag = child.tag
        if tag == "image":
            ref = child.path_attr()
            if ref:
                upgrade.image = resolve_pack_path(upgrade_dir, ref, macros)
        elif tag == "image-cancel":
            ref = child.path_attr()
            if ref:
                upgrade.image_cancel = _resolve_cancel_image(upgrade_dir, ref, macros)
        elif tag == "time":
            upgrade.time = child.float_value(0.0)
        elif tag == "unit-requirements":
            upgrade.unit_requirements = _names(child)
        elif tag == "upgrade-requirements":
            upgrade.upgrade_requirements = _names(child)
        elif tag == "resource-requirements":
            upgrade.resource_requirements = _named_amounts(child)
        elif tag == "effects":
            upgrade.effects = _names(child)
        elif tag in {
            "max-hp",
            "max-ep",
            "sight",
            "attack-strength",
            "attack-range",
            "armor",
            "move-speed",
            "production-speed",
        }:
            key = tag.replace("-", "_")
            value: Any = child.float_value(0.0)
            if child.attr("start-percentage") is not None:
                value = {"value": value, "start_percentage": child.float_value(0.0)}
            upgrade.stats[key] = value
        else:
            upgrade.unmapped.append(tag)

    if upgrade.unmapped:
        LOGGER.debug("Unmapped upgrade fields", extra={"upgrade": name, "tags": upgrade.unmapped})
    return upgrade


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _entity_dirs(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted(
        (p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name,
    )


def _entity_xml(directory: Path, name: str, expected_root: str) -> Path:
    """Locate the XML for an entity; fall back to scanning for the root tag."""
    preferred = directory / f"{name}.xml"
    if preferred.is_file():
        return preferred
    for candidate in sorted(directory.glob("*.xml")):
        try:
            root = parse_xml(candidate)
        except ParseError:
            continue
        if root.tag == expected_root:
            return candidate
    raise PackStructureError(f"No {expected_root} XML found in {directory}")


def _find_loading_screen(faction_dir: Path) -> Path | None:
    for pattern in LOADING_SCREEN_GLOBS:
        candidate = faction_dir / pattern
        if candidate.is_file():
            return candidate
    return None


def _names(node: XmlNode) -> list[str]:
    """``name`` attribute of every named child element."""
    return [child.name_attr() or "" for child in node.children if child.name_attr()]


def _named_amount_pairs(node: XmlNode) -> list[tuple[str, int]]:
    """``name`` -> amount pairs, reading amount/minimum/value attributes."""
    pairs: list[tuple[str, int]] = []
    for child in node.children:
        name = child.name_attr()
        if not name:
            continue
        raw = child.attr("amount") or child.attr("minimum") or child.value()
        try:
            amount = int(str(raw))
        except (TypeError, ValueError):
            amount = 0
        pairs.append((name, amount))
    return pairs


def _named_amounts(node: XmlNode) -> dict[str, int]:
    """``name`` -> amount for every child element."""
    return dict(_named_amount_pairs(node))


def _parse_parameters(
    node: XmlNode, base: Path, macros: dict[str, Path]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Map ``<parameters>`` children into a typed dict; unknown tags logged."""
    params: dict[str, Any] = {}
    unmapped: list[str] = []

    for child in node.children:
        tag = child.tag
        if tag == "size":
            params["size"] = child.int_value(0)
        elif tag == "height":
            params["height"] = child.float_value(0.0)
        elif tag == "ai-build-size":
            params["ai_build_size"] = child.int_value(0)
        elif tag == "max-hp":
            params["max_hp"] = child.int_value(0)
            if child.attr("regeneration") is not None:
                params["max_hp_regeneration"] = child.int_value(0)
        elif tag == "max-ep":
            params["max_ep"] = child.int_value(0)
            if child.attr("regeneration") is not None:
                params["max_ep_regeneration"] = child.int_value(0)
        elif tag == "armor":
            params["armor"] = child.int_value(0)
        elif tag == "armor-type":
            params["armor_type"] = str(child.value() or "")
        elif tag == "sight":
            params["sight"] = child.float_value(0.0)
        elif tag == "time":
            params["time"] = child.float_value(0.0)
        elif tag in {"multi-selection", "uniform-selection", "cellmap", "meeting-point"}:
            params[tag.replace("-", "_")] = child.bool_value(False)
        elif tag == "levels":
            params["levels"] = [
                {"name": level.name_attr() or "", "kills": level.int_value(0)}
                for level in child.get_all("level")
            ]
        elif tag == "fields":
            params["fields"] = [
                str(field_node.value() or "") for field_node in child.get_all("field")
            ]
        elif tag == "properties":
            params["properties"] = [str(prop.value() or "") for prop in child.get_all("property")]
        elif tag == "light":
            params["light_enabled"] = child.bool_value(False)
        elif tag == "unit-requirements":
            params["unit_requirements"] = _names(child)
        elif tag == "upgrade-requirements":
            params["upgrade_requirements"] = _names(child)
        elif tag == "resource-requirements":
            params["resource_requirements"] = _named_amounts(child)
        elif tag == "resources-stored":
            params["resources_stored"] = _named_amounts(child)
        elif tag == "image":
            ref = child.path_attr()
            params["image"] = resolve_pack_path(base, ref, macros) if ref else None
        elif tag == "image-cancel":
            ref = child.path_attr()
            params["image_cancel"] = _resolve_cancel_image(base, ref, macros) if ref else None
        elif tag in {"selection-sounds", "command-sounds"}:
            params[tag.replace("-", "_")] = _sound_paths(child, base, macros)
        else:
            unmapped.append(tag)

    return params, node.as_dict(), unmapped


def _sound_paths(node: XmlNode, base: Path, macros: dict[str, Path]) -> list[Path]:
    """Resolve every ``sound@path`` under a sounds container."""
    paths: list[Path] = []
    for sound in node.get_all("sound"):
        ref = sound.path_attr()
        if ref:
            paths.append(resolve_pack_path(base, ref, macros))
    return paths


def _parse_skills(node: XmlNode, base: Path, macros: dict[str, Path]) -> dict[str, SkillDef]:
    skills: dict[str, SkillDef] = {}
    for child in node.get_all("skill"):
        skill = _parse_skill(child, base, macros)
        if skill.name:
            skills[skill.name] = skill
    return skills


def _parse_skill(node: XmlNode, base: Path, macros: dict[str, Path]) -> SkillDef:
    type_node = node.get("type")
    name_node = node.get("name")
    skill = SkillDef(
        type=str(type_node.value() or "") if type_node else "",
        name=str(name_node.value() or "") if name_node else "",
    )
    if not skill.name:
        LOGGER.warning("Skill without a name", extra={"type": skill.type})

    for child in node.children:
        tag = child.tag
        if tag in {"type", "name"}:
            # Handled above when constructing the SkillDef.
            pass
        elif tag == "ep-cost":
            skill.ep_cost = child.float_value(0.0)
        elif tag == "speed":
            skill.speed = child.float_value(0.0)
        elif tag == "anim-speed":
            skill.anim_speed = child.float_value(0.0)
        elif tag == "animation":
            ref = child.path_attr()
            if ref:
                skill.animation_ref = ref
                skill.animation = resolve_pack_path(base, ref, macros)
        elif tag == "sound":
            _collect_sound_files(skill.sounds, child, base, macros)
        elif tag == "particles":
            for particle_file in child.get_all("particle-file"):
                ref = particle_file.path_attr()
                if ref:
                    skill.particles.append(resolve_pack_path(base, ref, macros))
        elif tag == "projectile":
            attack = skill.attack = skill.attack or AttackStats()
            attack.projectile = child.bool_value(False)
            _collect_nested_sounds(attack.sounds, child, base, macros)
            particle = child.get("particle")
            if particle is not None and particle.path_attr():
                attack.projectile_particle = resolve_pack_path(base, particle.path_attr(), macros)
        elif tag == "splash":
            attack = skill.attack = skill.attack or AttackStats()
            _collect_nested_sounds(attack.sounds, child, base, macros)
            radius = child.get("radius")
            if radius is not None:
                attack.splash_radius = radius.float_value(0.0)
            particle = child.get("particle")
            if particle is not None and particle.path_attr():
                attack.splash_particle = resolve_pack_path(base, particle.path_attr(), macros)
        elif tag.startswith("attack-"):
            attack = skill.attack = skill.attack or AttackStats()
            _parse_attack_field(attack, child)
        elif tag == "fade":
            # Visual-only; nothing to map.
            pass
        else:
            skill.unmapped.append(tag)
    return skill


def _collect_nested_sounds(
    target: list[Path],
    node: XmlNode,
    base: Path,
    macros: dict[str, Path],
) -> None:
    """Collect ``sound-file`` paths from ``sound`` children of a container."""
    for sound in node.get_all("sound"):
        _collect_sound_files(target, sound, base, macros)


def _collect_sound_files(
    target: list[Path],
    sound: XmlNode,
    base: Path,
    macros: dict[str, Path],
) -> None:
    """Append a ``<sound>`` element's resolved ``sound-file`` paths."""
    enabled = (sound.attr("enabled") or "true").lower()
    if enabled not in _SOUND_ENABLED_TRUE:
        return
    for sound_file in sound.get_all("sound-file"):
        ref = sound_file.path_attr()
        if ref:
            target.append(resolve_pack_path(base, ref, macros))


def _parse_attack_field(attack: AttackStats, node: XmlNode) -> None:
    tag = node.tag
    if tag == "attack-strength":
        attack.strength = node.float_value(0.0)
    elif tag == "attack-var":
        attack.variance = node.float_value(0.0)
    elif tag == "attack-range":
        attack.range = node.float_value(0.0)
    elif tag == "attack-type":
        attack.attack_type = str(node.value() or "")
    elif tag == "attack-start-time":
        attack.start_time = node.float_value(0.0)
    elif tag == "attack-fields":
        attack.fields = [str(field_node.value() or "") for field_node in node.get_all("field")]


def _parse_commands(node: XmlNode, base: Path, macros: dict[str, Path]) -> list[CommandDef]:
    commands: list[CommandDef] = []
    for child in node.get_all("command"):
        type_node = child.get("type")
        name_node = child.get("name")
        command = CommandDef(
            type=str(type_node.value() or "") if type_node else "",
            name=str(name_node.value() or "") if name_node else "",
        )
        for sub in child.children:
            tag = sub.tag
            if tag == "image":
                ref = sub.path_attr()
                if ref:
                    command.image = resolve_pack_path(base, ref, macros)
            elif tag == "produced-unit":
                command.produced_unit = sub.name_attr()
            elif tag == "produced-upgrade":
                command.produced_upgrade = sub.name_attr()
            elif tag == "morph-unit":
                command.morph_unit = sub.name_attr()
            elif tag == "discount":
                command.discount = sub.float_value(0.0)
            elif tag in {"unit-requirements", "upgrade-requirements"}:
                command.requirements.extend(_names(sub))
            elif tag.endswith("-skill"):
                ref = sub.value()
                if ref:
                    command.skill_refs[tag[: -len("-skill")]] = str(ref)
            # other children (images, requirements) carry no mapping here
        commands.append(command)
    return commands


def _classify_building(unit: UnitDef) -> bool:
    """Heuristic: buildings are static producers, not field units."""
    params = unit.parameters
    if params.get("ai_build_size"):
        return True
    if any(skill.type == "move" for skill in unit.skills.values()):
        # Mobile producers (MG bard summons treants/ents) are field
        # units, not buildings: MG buildings never move.
        return False
    if "burnable" in (params.get("properties") or []):
        return True
    if any(skill.type in {"produce", "be_built"} for skill in unit.skills.values()):
        return True
    if any(command.type == "produce" for command in unit.commands):
        return True
    if int(params.get("size") or 0) >= 3:
        LOGGER.debug(
            "Classified as building by size", extra={"unit": unit.name, "size": params.get("size")}
        )
        return True
    return False
