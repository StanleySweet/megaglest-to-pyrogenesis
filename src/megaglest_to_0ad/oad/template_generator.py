"""Simulation template generation (``simulation/templates/…/{civ}/{name}.xml``).

Parents are plain ``template_*`` chains that exist in BOTH the 0.28 and 0.29
public mods (0.29's ``civ/{civ}|{class}|`` modifiers need civ-specific
override dirs a converted civ does not ship). Components mirror Millennium
A.D. structures and 0.29 unit templates; armor uses the ``Resistance``
component (0.28 and 0.29 both), not the obsolete ``Armour``.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from ..core.config import Settings
from ..core.media_conversion import MediaConversionStats
from ..megaglest.civ_loader import Faction, UnitDef
from .actor_generator import _unit_models
from .common import (
    HP_SCALE,
    SPEED_SCALE,
    TILE_METERS,
    TIME_SCALE,
    humanize_name,
    resource_cost,
    town_centre_candidate,
)
from .mod_builder import sanitize_mod_name


def generate_templates(
    faction: Faction,
    mod_dir: Path,
    stats: MediaConversionStats,
    settings: Settings,
) -> list[Path]:
    """Write one template per unit/building; returns the written XML paths."""
    del settings  # parent chains are version-stable
    civ = sanitize_mod_name(faction.name)
    tc = town_centre_candidate(faction)
    written: list[Path] = []
    for name, unit in sorted(faction.units.items()):
        root = _build_template(faction, civ, unit, tc, stats)
        sub = "structures" if unit.is_building else "units"
        path = mod_dir / "simulation/templates" / sub / civ / f"{sanitize_mod_name(name)}.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        tree = etree.ElementTree(root)
        etree.indent(tree, space="  ")
        path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="utf-8"))
        written.append(path)
    return written


def _parent_for(unit: UnitDef, tc: UnitDef | None) -> str:
    if unit.is_building:
        if tc is not None and unit.name == tc.name:
            return "template_structure_civic_civil_centre"
        if any(cmd.type == "produce" for cmd in unit.commands):
            return "template_structure_military_barracks"
        if any(cmd.type == "upgrade" for cmd in unit.commands):
            return "template_structure_economic"
        return "template_structure"
    attacks = [skill.attack for skill in unit.skills.values() if skill.attack]
    move_speed = max(
        (skill.speed for skill in unit.skills.values() if skill.type == "move"),
        default=0.0,
    )
    if move_speed > 400:
        return "template_unit_cavalry_melee"
    if any(a.range > 4 or a.projectile for a in attacks):
        return "template_unit_infantry_ranged"
    if attacks:
        return "template_unit_infantry_melee"
    return "template_unit_support"


def _build_template(
    faction: Faction, civ: str, unit: UnitDef, tc: UnitDef | None, stats: MediaConversionStats
) -> etree._Element:
    root = etree.Element("Entity", parent=_parent_for(unit, tc))
    if unit.is_building:
        _add_cost(root, unit, build_time=True)
        _add_footprint(root, unit, stats)
        _add_health(root, unit)
        _add_identity(root, civ, unit)
        _add_obstruction(root, unit, stats)
        _add_researcher(root, civ, unit)
        _add_trainer(root, civ, unit)
    else:
        # The engine's generated entity grammar sequences its optional
        # component refs in registration (alphabetical) order, so components
        # must be emitted in that order: Attack, Builder, Cost, Health,
        # Identity, Promotion, Resistance, UnitMotion, Vision.
        _add_attack(root, unit)
        _add_builder(root, civ, unit)
        _add_cost(root, unit, build_time=False)
        _add_health(root, unit)
        _add_identity(root, civ, unit)
        _add_promotion(root, civ, unit)
        _add_resistance(root, unit)
        _add_motion(root, unit)
        _add_vision(root, unit)
    _add_visual_actor(root, civ, unit, stats)
    return root


def _add_cost(root: etree._Element, unit: UnitDef, build_time: bool) -> None:
    resources = resource_cost(unit.parameters.get("resource_requirements", {}))
    if not resources and not build_time:
        return
    cost = etree.SubElement(root, "Cost")
    # BuildTime is required by the 0.28+ Cost schema for every cost block;
    # units fall back to a 10 s default when the pack has no time value.
    build_seconds = unit.parameters.get("time", 0.0)
    if not build_time and build_seconds <= 0:
        build_seconds = 10.0
    time_node = etree.SubElement(cost, "BuildTime")
    time_node.text = str(max(1, round(build_seconds / TIME_SCALE)))
    if resources:
        res = etree.SubElement(cost, "Resources")
        for name in ("food", "wood", "stone", "metal"):
            if name in resources:
                node = etree.SubElement(res, name)
                node.text = str(resources[name])
    # Population is required by the 0.28+ Cost schema; buildings do not
    # consume population.
    population = etree.SubElement(cost, "Population")
    population.text = "0" if build_time else "1"


def _add_footprint(root: etree._Element, unit: UnitDef, stats: MediaConversionStats) -> None:
    width, depth, height = _placement_dims(unit, stats)
    fp = etree.SubElement(root, "Footprint")
    etree.SubElement(fp, "Square", width=_fmt(width), depth=_fmt(depth))
    height_node = etree.SubElement(fp, "Height")
    height_node.text = _fmt(height)


def _placement_dims(
    unit: UnitDef, stats: MediaConversionStats
) -> tuple[float, float, float]:
    """(width, depth, height) metres for Footprint and Obstruction.

    Prefers the base model's measured bounding box (the G3D rest pose is in
    metres); falls back to the legacy ``size * TILE_METERS`` guess for units
    whose model was never converted.
    """
    base = _unit_models(unit, stats)[0]
    if base is not None:
        footprint = stats.models[base].footprint
        if footprint is not None:
            return footprint
    size = float(unit.parameters.get("size", 4))
    meters = max(4.0, size * TILE_METERS)
    return meters, meters, float(unit.parameters.get("height", 2.0))


def _add_health(root: etree._Element, unit: UnitDef) -> None:
    health = etree.SubElement(root, "Health")
    node = etree.SubElement(health, "Max")
    node.text = str(max(1, round(unit.parameters.get("max_hp", 100) / HP_SCALE)))
    # RegenRate/IdleRegenRate/DeathType/Unhealable are required by the
    # 0.28+ Health schema.
    etree.SubElement(health, "RegenRate").text = "0"
    etree.SubElement(health, "IdleRegenRate").text = "0"
    etree.SubElement(health, "DeathType").text = "corpse"
    etree.SubElement(health, "Unhealable").text = "false"


def _add_resistance(root: etree._Element, unit: UnitDef) -> None:
    armor = unit.parameters.get("armor", 0)
    if armor <= 0:
        return
    resistance = etree.SubElement(root, "Resistance")
    entity = etree.SubElement(resistance, "Entity")
    damage = etree.SubElement(entity, "Damage")
    for kind in ("Hack", "Pierce", "Crush"):
        node = etree.SubElement(damage, kind)
        node.text = _fmt(armor / HP_SCALE)


def _add_attack(root: etree._Element, unit: UnitDef) -> None:
    attacks = [(skill, skill.attack) for skill in unit.skills.values() if skill.attack]
    if not attacks:
        return
    attack = etree.SubElement(root, "Attack")
    for skill, stats in attacks:
        ranged = stats.range > 4 or stats.projectile
        kind = "Ranged" if ranged else "Melee"
        node = etree.SubElement(attack, kind)
        # AttackName/MaxRange/RepeatTime are required by the 0.28+ Attack
        # schema. RepeatTime derives from the skill's animation duration
        # (100 / anim_speed seconds per cycle).
        etree.SubElement(node, "AttackName").text = kind
        damage = etree.SubElement(node, "Damage")
        damage_kind = "Pierce" if stats.attack_type == "pierce" else "Hack"
        dmg_node = etree.SubElement(damage, damage_kind)
        dmg_node.text = _fmt(stats.strength / HP_SCALE)
        rng = etree.SubElement(node, "MaxRange")
        rng.text = _fmt(max(1.0, stats.range * TILE_METERS))
        if skill.anim_speed > 0:
            repeat = max(500, round(1000 * 100.0 / skill.anim_speed))
        else:
            repeat = 1000
        etree.SubElement(node, "RepeatTime").text = str(repeat)

def _add_identity(root: etree._Element, civ: str, unit: UnitDef) -> None:
    label = humanize_name(unit.name)
    identity = etree.SubElement(root, "Identity")
    etree.SubElement(identity, "Civ").text = civ
    etree.SubElement(
        identity, "SelectionGroupName"
    ).text = f"units/{civ}/{sanitize_mod_name(unit.name)}"
    etree.SubElement(identity, "GenericName").text = label
    etree.SubElement(identity, "SpecificName").text = label
    etree.SubElement(identity, "Icon").text = f"units/{civ}/{sanitize_mod_name(unit.name)}.png"
    # Undeletable is required by the 0.28+ Identity schema.
    etree.SubElement(identity, "Undeletable").text = "false"


def _add_obstruction(root: etree._Element, unit: UnitDef, stats: MediaConversionStats) -> None:
    width, depth, _height = _placement_dims(unit, stats)
    obstruction = etree.SubElement(root, "Obstruction")
    etree.SubElement(obstruction, "Static", width=_fmt(width), depth=_fmt(depth))
    # The 0.28+ Obstruction schema requires the full block-flag set.
    for name, value in (
        ("Active", "true"),
        ("BlockMovement", "true"),
        ("BlockPathfinding", "false"),
        ("BlockFoundation", "true"),
        ("BlockConstruction", "true"),
        ("DeleteUponConstruction", "false"),
        ("DisableBlockMovement", "false"),
        ("DisableBlockPathfinding", "false"),
    ):
        etree.SubElement(obstruction, name).text = value


def _add_motion(root: etree._Element, unit: UnitDef) -> None:
    speed = max(
        (skill.speed for skill in unit.skills.values() if skill.type == "move"),
        default=0.0,
    )
    if speed <= 0:
        return
    motion = etree.SubElement(root, "UnitMotion")
    walk = etree.SubElement(motion, "WalkSpeed")
    walk.text = _fmt(speed / SPEED_SCALE)
    # FormationController/InstantTurnAngle/Acceleration/PassabilityClass/
    # Weight are required by the 0.28+ UnitMotion schema.
    etree.SubElement(motion, "FormationController").text = "false"
    etree.SubElement(motion, "InstantTurnAngle").text = "1.0"
    etree.SubElement(motion, "Acceleration").text = "3.0"
    etree.SubElement(motion, "PassabilityClass").text = "default"
    etree.SubElement(motion, "Weight").text = "10"


def _add_vision(root: etree._Element, unit: UnitDef) -> None:
    sight = float(unit.parameters.get("sight", 0))
    if sight <= 0:
        return
    vision = etree.SubElement(root, "Vision")
    rng = etree.SubElement(vision, "Range")
    # The 0.28+ Vision schema types Range as nonNegativeInteger.
    rng.text = str(max(1, round(sight * TILE_METERS)))


def _add_builder(root: etree._Element, civ: str, unit: UnitDef) -> None:
    if not any(skill.type == "build" for skill in unit.skills.values()):
        return


def _add_promotion(root: etree._Element, civ: str, unit: UnitDef) -> None:
    """Promotion for every unit.

    Morphing units promote to their target at 100 XP. Non-morph units still
    need the component: public's ``template_unit_cavalry``/``template_unit_infantry``
    ship a RequiredXp-only Promotion (no Entity) that fails the schema once
    merged into our templates, so every unit must override it. Promoting to
    itself with an unreachable threshold is a no-op.
    """
    promotion = etree.SubElement(root, "Promotion")
    morphs = [cmd.morph_unit for cmd in unit.commands if cmd.morph_unit]
    if morphs:
        etree.SubElement(promotion, "Entity").text = f"units/{civ}/{morphs[0]}"
        etree.SubElement(promotion, "RequiredXp").text = "100"
    else:
        etree.SubElement(promotion, "Entity").text = f"units/{civ}/{sanitize_mod_name(unit.name)}"
        etree.SubElement(promotion, "RequiredXp").text = "1000000"


def _add_trainer(root: etree._Element, civ: str, unit: UnitDef) -> None:
    produced = sorted({cmd.produced_unit for cmd in unit.commands if cmd.produced_unit})
    if not produced:
        return
    trainer = etree.SubElement(root, "Trainer")
    entities = etree.SubElement(trainer, "Entities", datatype="tokens")
    entities.text = "\n".join(f"units/{civ}/{name}" for name in produced)


def _add_researcher(root: etree._Element, civ: str, unit: UnitDef) -> None:
    # <name> is the display label; <produced-upgrade> is the canonical
    # upgrade id matching faction.upgrades keys (they differ, e.g. forge's
    # "noldor_armour" -> noldor_armour_crafting). Fall back for old packs.
    upgrades = sorted(
        {
            cmd.produced_upgrade or cmd.name
            for cmd in unit.commands
            if cmd.type == "upgrade" and (cmd.produced_upgrade or cmd.name)
        }
    )
    if not upgrades:
        return
    researcher = etree.SubElement(root, "Researcher")
    techs = etree.SubElement(researcher, "Technologies", datatype="tokens")
    techs.text = "\n".join(f"{civ}/{sanitize_mod_name(name)}" for name in upgrades)


def _add_visual_actor(
    root: etree._Element, civ: str, unit: UnitDef, stats: MediaConversionStats
) -> None:
    sub = "structures" if unit.is_building else "units"
    visual = etree.SubElement(root, "VisualActor")
    etree.SubElement(visual, "Actor").text = f"{sub}/{civ}/{sanitize_mod_name(unit.name)}.xml"
    if unit.is_building and _unit_models(unit, stats)[1] is not None:
        # CCmpVisualActor renders this actor while the entity is a
        # foundation (Foundation.js drives construction via hitpoints,
        # which the fndn_ actor maps to build-stage meshes). Without a
        # be_built model no fndn_ actor is written, and the engine
        # falls back to the plain Actor for the foundation phase.
        etree.SubElement(
            visual, "FoundationActor"
        ).text = f"structures/{civ}/fndn_{sanitize_mod_name(unit.name)}.xml"
    # SilhouetteDisplay/SilhouetteOccluder/VisibleInAtlasOnly are required
    # by the 0.28+ VisualActor schema.
    etree.SubElement(visual, "SilhouetteDisplay").text = str(unit.is_building).lower()
    etree.SubElement(visual, "SilhouetteOccluder").text = "false"
    etree.SubElement(visual, "VisibleInAtlasOnly").text = "false"


def _fmt(value: float) -> str:
    return f"{value:.1f}"
