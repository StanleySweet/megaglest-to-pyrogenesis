"""Simulation template generation (``simulation/templates/…/{civ}/{name}.xml``).

Parents are plain ``template_*`` chains that exist in BOTH the 0.28 and 0.29
public mods (0.29's ``civ/{civ}|{class}|`` modifiers need civ-specific
override dirs a converted civ does not ship). Components mirror Millennium
A.D. structures and 0.29 unit templates; armor uses the ``Resistance``
component (0.28 and 0.29 both), not the obsolete ``Armour``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

from ..core.config import Settings
from ..core.media_conversion import MediaConversionStats, engine_animation_names
from ..megaglest.civ_loader import Faction, UnitDef
from .actor_generator import _unit_models
from .common import (
    HP_SCALE,
    SPEED_SCALE,
    TILE_METERS,
    TIME_SCALE,
    grace_amount,
    humanize_name,
    resource_cost,
    town_centre_candidate,
    unmapped_resources,
    write_xml,
)
from .mod_builder import sanitize_mod_name

LOGGER = logging.getLogger(__name__)


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
        requirements = unit.parameters.get("resource_requirements", {})
        dropped = unmapped_resources(requirements, grace_is_mapped=True)
        if dropped:
            summary = ", ".join(f"{k} x{v}" for k, v in sorted(dropped.items()))
            message = f"{name}: custom resources '{summary}' have no 0 A.D. analog; dropped"
            stats.warnings.append(message)
            LOGGER.warning("template: %s", message)
        root = _build_template(faction, civ, unit, tc, stats)
        sub = "structures" if unit.is_building else "units"
        path = mod_dir / "simulation/templates" / sub / civ / f"{sanitize_mod_name(name)}.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_xml(path, root)
        written.append(path)
    return written


def _insert_alphabetic(root: etree._Element, element: etree._Element) -> None:
    """Insert ``element`` keeping ``root``'s children alphabetically ordered.

    0 A.D. registers template components in document order, so the generated
    file has to stay sorted for the engine to see them all.
    """
    index = next((i for i, el in enumerate(root) if el.tag > element.tag), len(root))
    root.insert(index, element)


def _parent_for(unit: UnitDef, tc: UnitDef | None) -> str:
    if unit.is_building:
        if tc is not None and unit.name == tc.name:
            return "template_structure_civic_civil_centre"
        if any(cmd.type == "produce" for cmd in unit.commands):
            return "template_structure_military_barracks"
        if any(cmd.type == "upgrade" for cmd in unit.commands):
            return "template_structure_economic"
        return "template_structure"
    if unit.is_flying:
        return "template_unit"
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
        _add_population_bonus(root, unit)
        _add_researcher(root, civ, unit)
        _add_trainer(root, civ, unit)
        _add_sound(root, civ, unit)
    else:
        # The engine's generated entity grammar sequences its optional
        # component refs in registration (alphabetical) order, so components
        # must be emitted in that order: Attack, Builder, Cost, Health,
        # Identity, Promotion, Resistance, UnitMotion, Vision.
        _add_attack(root, unit, stats)
        buildable = sorted(
            sanitize_mod_name(n) for n, u in faction.units.items() if u.is_building
        )
        _add_builder(root, civ, unit, buildable)
        _add_cost(root, unit, build_time=False)
        _add_health(root, unit)
        _add_identity(root, civ, unit)
        _add_promotion(root, civ, unit)
        _add_resistance(root, unit)
        _add_gatherer(root, unit)
        # Mobile summoners (MG bard) produce units too; Trainer is
        # entity-generic in the engine, so a unit can keep its summons.
        _add_trainer(root, civ, unit)
        _add_motion(root, unit)
        _add_vision(root, unit)
        _add_sound(root, civ, unit)
    _add_visual_actor(root, civ, unit, stats)
    return root


def _add_cost(root: etree._Element, unit: UnitDef, build_time: bool) -> None:
    requirements = unit.parameters.get("resource_requirements", {})
    resources = resource_cost(requirements)
    grace = grace_amount(requirements)
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
    # 0 A.D. has no ``grace`` resource; it is population instead. A unit's
    # positive grace cost is the population slots it consumes. Population is
    # not consumed by buildings (see _add_population_bonus).
    population = etree.SubElement(cost, "Population")
    if build_time:
        population.text = "0"
    else:
        population.text = str(grace if grace > 0 else "1")


def _add_population_bonus(root: etree._Element, unit: UnitDef) -> None:
    """A building's negative ``grace`` cost grants a population-cap bonus.

    MegaGlest buildings with a negative grace requirement *provide* grace
    while they stand (the sanctuary/PeC mechanic); 0 A.D. expresses the same
    idea with the ``Population`` component's ``Bonus`` (house analog).
    Producible structures (sanctuary, great_tree in the demo pack) emit
    it; a nonnegative grace emits nothing.
    """
    grace = grace_amount(unit.parameters.get("resource_requirements", {}))
    if grace >= 0:
        return
    pop = etree.Element("Population")
    etree.SubElement(pop, "Bonus").text = str(-grace)
    # Population sits between Obstruction and Researcher.
    _insert_alphabetic(root, pop)


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


def _add_attack(
    root: etree._Element, unit: UnitDef, stats: MediaConversionStats
) -> None:
    attacks = [(skill, skill.attack) for skill in unit.skills.values() if skill.attack]
    if not attacks:
        return
    attack = etree.SubElement(root, "Attack")
    for skill, astats in attacks:
        ranged = astats.range > 4 or astats.projectile
        kind = "Ranged" if ranged else "Melee"
        node = etree.SubElement(attack, kind)
        # AttackName/MaxRange/RepeatTime are required by the 0.28+ Attack
        # schema. RepeatTime derives from the skill's animation duration
        # (100 / anim_speed seconds per cycle).
        etree.SubElement(node, "AttackName").text = kind
        damage = etree.SubElement(node, "Damage")
        damage_kind = "Pierce" if astats.attack_type == "pierce" else "Hack"
        dmg_node = etree.SubElement(damage, damage_kind)
        dmg_node.text = _fmt(astats.strength / HP_SCALE)
        rng = etree.SubElement(node, "MaxRange")
        rng.text = _fmt(max(1.0, astats.range * TILE_METERS))
        if skill.anim_speed > 0:
            repeat = max(500, round(1000 * 100.0 / skill.anim_speed))
        else:
            repeat = 1000
        etree.SubElement(node, "RepeatTime").text = str(repeat)
        if astats.projectile:
            # Without a Projectile block the engine applies damage
            # instantly at the attack event; MG archers mark
            # attack-projectile so their arrows fly (dodgeable, blocked
            # by walls). Public defaults: arrow Speed 100 / Gravity 50.
            projectile = etree.SubElement(node, "Projectile")
            for name, value in (
                ("Speed", "100"),
                ("Spread", "0"),
                ("Gravity", "50"),
                ("FriendlyFire", "false"),
            ):
                etree.SubElement(projectile, name).text = value
            _add_projectile_visuals(projectile, astats, stats)


def _add_projectile_visuals(
    projectile: etree._Element,
    astats,
    stats: MediaConversionStats,
) -> None:
    """Attach the converted projectile/impact actors to a Projectile block.

    The MegaGlest ``projectile_particle`` that references the attack's
    projectile model is resolved (via ``stats.projectile_actor_by_particle``)
    into a 0 A.D. ``ActorName`` so the flying arrow/stone is visible, plus an
    ``ImpactActorName`` for the impact burst. Falls back to the unit's own
    actor projectile (engine default) when no conversion exists.
    """
    particle_xml = astats.projectile_particle
    actor = stats.projectile_actor_by_particle.get(particle_xml) if particle_xml else None
    if actor is None:
        return
    etree.SubElement(projectile, "ActorName").text = actor
    g3d = next((g for g, rel in stats.projectile_actor.items() if rel == actor), None)
    impact = stats.projectile_impact_actor.get(g3d) if g3d is not None else None
    if impact:
        etree.SubElement(projectile, "ImpactActorName").text = impact
        etree.SubElement(projectile, "ImpactAnimationLifetime").text = "0.3"

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
    tag = "UnitMotionFlying" if unit.is_flying else "UnitMotion"
    motion = etree.SubElement(root, tag)
    walk = etree.SubElement(motion, "WalkSpeed")
    walk.text = _fmt(speed / SPEED_SCALE)
    if unit.is_flying:
        # The 0.28 UnitMotionFlying schema requires MaxSpeed (nonNegativeDecimal).
        max_speed = etree.SubElement(motion, "MaxSpeed")
        max_speed.text = _fmt(speed / SPEED_SCALE)
    etree.SubElement(motion, "FormationController").text = "false"
    etree.SubElement(motion, "InstantTurnAngle").text = "1.0"
    etree.SubElement(motion, "Acceleration").text = "3.0"
    passability = "air" if unit.is_flying else "default"
    etree.SubElement(motion, "PassabilityClass").text = passability
    etree.SubElement(motion, "Weight").text = "10"
    if unit.is_flying:
        etree.SubElement(motion, "FlyingHeight").text = "10"


def _add_vision(root: etree._Element, unit: UnitDef) -> None:
    sight = float(unit.parameters.get("sight", 0))
    if sight <= 0:
        return
    vision = etree.SubElement(root, "Vision")
    rng = etree.SubElement(vision, "Range")
    # The 0.28+ Vision schema types Range as nonNegativeInteger.
    rng.text = str(max(1, round(sight * TILE_METERS)))


def _add_builder(
    root: etree._Element,
    civ: str,
    unit: UnitDef,
    buildable: list[str],
) -> None:
    """Builder component for units with a build skill.

    Without <Builder><Entities> the engine never offers the construct
    command, so a worker that cannot list its buildings cannot build at
    all. The pack's build-skill speed (ms per work hit) has no direct 0
    A.D. rate equivalent; keep the public default Rate 1.0 and preserve
    the pack's build times in Cost/BuildTime instead.
    """
    if not any(skill.type == "build" for skill in unit.skills.values()):
        return
    builder = etree.SubElement(root, "Builder")
    etree.SubElement(builder, "Rate").text = "1.0"
    entities = etree.SubElement(builder, "Entities", datatype="tokens")
    entities.text = "\n".join(f"structures/{civ}/{name}" for name in buildable)

def _add_gatherer(root: etree._Element, unit: UnitDef) -> None:
    """ResourceGatherer for units with a harvest skill.

    The fallback parent for workers (template_unit_support) carries no
    ResourceGatherer, so a harvest-skill unit would gather nothing. MG
    harvest is one generic skill covering all resources; give it the
    public per-subtype rates (a missing rate is ungatherable) and the
    standard 10-unit carries. Inserted alphabetically like Sound: the
    engine sequences optional component refs in registration order.
    """
    if not any(skill.type == "harvest" for skill in unit.skills.values()):
        return
    gatherer = etree.Element("ResourceGatherer")
    etree.SubElement(gatherer, "MaxDistance").text = "2.0"
    etree.SubElement(gatherer, "BaseSpeed").text = "1.0"
    rates = etree.SubElement(gatherer, "Rates")
    for resource, value in (
        ("food.fruit", "0.5"),
        ("food.grain", "0.25"),
        ("food.meat", "1"),
        ("wood.tree", "0.75"),
        ("wood.ruins", "5"),
        ("stone.rock", "0.5"),
        ("stone.ruins", "2"),
        ("metal.ore", "0.5"),
        ("metal.ruins", "2"),
    ):
        etree.SubElement(rates, resource).text = value
    capacities = etree.SubElement(gatherer, "Capacities")
    for resource, value in (("food", "10"), ("wood", "10"), ("stone", "10"), ("metal", "10")):
        etree.SubElement(capacities, resource).text = value
    _insert_alphabetic(root, gatherer)

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
    trainer = etree.Element("Trainer")
    entities = etree.SubElement(trainer, "Entities", datatype="tokens")
    entities.text = "\n".join(f"units/{civ}/{name}" for name in produced)
    _insert_alphabetic(root, trainer)


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


def _add_sound(root: etree._Element, civ: str, unit: UnitDef) -> None:
    """Wire the converted SoundGroup files (``audio/groups/{unit}_{set}.xml``)
    to the engine's query keys. ``selection-sounds`` feed ``select``; each
    skill feeds the engine animation name(s) the actor wires for it (die ->
    death, harvest -> gather_*, attack -> attack_melee/attack_ranged, ...);
    command sounds have no engine query and stay unwired.
    """
    groups: dict[str, str] = {}
    if unit.selection_sounds:
        groups["select"] = f"groups/{sanitize_mod_name(unit.name)}_select.xml"
    for skill in unit.skills.values():
        sounds = list(skill.sounds)
        if skill.attack is not None:
            sounds.extend(skill.attack.sounds)
        if not sounds:
            continue
        group_file = f"groups/{sanitize_mod_name(unit.name)}_{skill.type}.xml"
        for anim_name in engine_animation_names(skill):
            groups.setdefault(anim_name, group_file)
    if not groups:
        return
    sound = etree.Element("Sound")
    sound_groups = etree.SubElement(sound, "SoundGroups")
    for key in sorted(groups):
        etree.SubElement(sound_groups, key).text = groups[key]
    _insert_alphabetic(root, sound)


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
