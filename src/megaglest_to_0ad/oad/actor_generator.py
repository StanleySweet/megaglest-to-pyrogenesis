"""Actor XML generation (``art/actors/{units|structures}/{civ}/{name}.xml``).

Actors reference the skinned (or static base-pose) DAE written for the
unit's model, its converted base texture, and — for rigged models — the
animation files that drive it. The material is chosen per target version.
Structure actors omit the foundation actor (the public mod ships no
simulation-side ``fndn_*`` templates to reference).

Skinned-prop sync: every ``<animation>`` carries an ``id`` equal to its
``name``. The engine's ``CUnitAnimation`` picks a random animation id on the
root model (``PickAnimationID``) and plays the matching ``id`` on every
attached prop; without shared ids props fall back to idle and desync (see
``tools/validate_animations.py`` for the lint that guards this).
"""

from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

from ..core.config import Settings
from ..core.media_conversion import MediaConversionStats, engine_animation_names
from ..megaglest.civ_loader import Faction, UnitDef
from .common import add_actor_textures, material_for, write_xml
from .mod_builder import sanitize_mod_name

LOGGER = logging.getLogger(__name__)

# Actor animation speed is integer percent (ObjectBase.cpp: ToInt()/100);
# the MegaGlest anim-speed is baked into the DAE keyframe times instead.
_ANIM_SPEED_PERCENT = "100"


def generate_actors(
    faction: Faction,
    mod_dir: Path,
    stats: MediaConversionStats,
    settings: Settings,
) -> list[Path]:
    """Write one actor per unit/building; returns the written XML paths."""
    civ = sanitize_mod_name(faction.name)
    written: list[Path] = []
    for name, unit in sorted(faction.units.items()):
        base, cons, des = _unit_models(unit, stats)
        if base is None or not stats.models[base].mesh_daes:
            stats.warnings.append(f"{name}: no converted model; actor skipped")
            continue
        sub = "structures" if unit.is_building else "units"
        root = _build_actor(civ, unit, base, cons, des, stats, settings, mod_dir, written)
        path = mod_dir / "art/actors" / sub / civ / f"{sanitize_mod_name(name)}.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_xml(path, root)
        written.append(path)
        if unit.is_building and cons is not None:
            fndn = _build_foundation_actor(civ, unit, cons, des, stats, settings, mod_dir)
            if fndn is not None:
                written.append(fndn)
    return written


def _unit_models(
    unit: UnitDef, stats: MediaConversionStats
) -> tuple[Path | None, Path | None, Path | None]:
    """(base, construction, destroyed) g3d sources from the unit's skills.

    MegaGlest units carry no single model parameter: meshes come from skill
    animations (``stop_skill`` idle is the base pose). Buildings add a
    ``be_built`` skill (construction stages) and a ``die`` skill (destroyed
    state); both are optional for units.
    """
    base = cons = des = None
    for skill in unit.skills.values():
        if skill.animation is None:
            continue
        key = skill.animation.resolve()
        g3d = next(
            (g for g, r in stats.models.items() if g.resolve() == key and r.mesh_daes),
            None,
        )
        if g3d is None:
            continue
        if skill.type == "be_built":
            cons = g3d
        elif skill.type == "die":
            des = g3d
        elif base is None:
            base = g3d
    return base, cons, des


def _build_actor(
    civ: str,
    unit: UnitDef,
    g3d: Path,
    cons: Path | None,
    des: Path | None,
    stats: MediaConversionStats,
    settings: Settings,
    mod_dir: Path,
    written: list[Path],
) -> etree._Element:
    root = etree.Element("actor", version="1")
    etree.SubElement(root, "castshadow")
    mesh_daes = stats.models[g3d].mesh_daes
    props = _add_props(civ, g3d, mesh_daes, stats, settings, mod_dir, written)
    group = etree.SubElement(root, "group")
    variant = etree.SubElement(group, "variant", frequency="1", name="Base")
    _add_animations(variant, civ, unit, g3d, stats)
    mesh = etree.SubElement(variant, "mesh")
    mesh.text = f"{civ}/{mesh_daes[0].name}"
    texture = _dae_texture(mesh_daes[0], g3d, stats)
    if texture is not None:
        add_actor_textures(variant, civ, texture)
    # engine actor grammar (0.28 actor.rng): <props> lives inside a
    # <variant>, never at actor level (CXeromyces rejects the latter)
    if props is not None:
        variant.append(props)
    _add_particle_props(variant, unit, stats)
    if unit.is_building:
        _add_health_group(root, civ, des, stats)
    material = etree.SubElement(root, "material")
    material.text = _material_for(g3d, stats, settings)
    return root


def _add_particle_props(
    variant: etree._Element, unit: UnitDef, stats: MediaConversionStats
) -> None:
    """Attach the unit's converted particle systems as actor props.

    ``stats.unit_particle_props[unit]`` lists ``particle/<name>.xml`` props
    (the actor wrappers written by :mod:`particle_converter`). They attach at
    the default ``root`` prop point so they follow the unit's base pose.
    """
    props = stats.unit_particle_props.get(sanitize_mod_name(unit.name))
    if not props:
        return
    container = etree.Element("props")
    for rel in props:
        etree.SubElement(container, "prop", actor=rel, attachpoint="root")
    variant.append(container)


def _add_health_group(
    root: etree._Element, civ: str, des: Path | None, stats: MediaConversionStats
) -> None:
    """Damage-state variants for buildings.

    ``template_structure`` ships Health/DamageVariants (light/medium/heavy),
    so every building's health selection cycles alive -> lightdamage ->
    mediumdamage -> heavydamage -> death. Named empty variants keep the
    Base mesh; the death variant swaps in the destroyed model.
    """
    group = etree.SubElement(root, "group")
    etree.SubElement(group, "variant", frequency="1", name="alive")
    for name in ("lightdamage", "mediumdamage", "heavydamage"):
        etree.SubElement(group, "variant", name=name)
    death = etree.SubElement(group, "variant", name="death")
    if des is not None:
        daes = stats.models[des].mesh_daes
        if daes:
            mesh = etree.SubElement(death, "mesh")
            mesh.text = f"{civ}/{daes[0].name}"
            texture = _dae_texture(daes[0], des, stats)
            if texture is not None:
                add_actor_textures(death, civ, texture)


def _dae_texture(dae: Path, g3d: Path, stats: MediaConversionStats) -> Path | None:
    """baseTex for one DAE: its own texture group, else the model's first."""
    return stats.mesh_textures.get(dae) or stats.model_texture.get(g3d)


def _add_props(
    civ: str,
    g3d: Path,
    mesh_daes: list[Path],
    stats: MediaConversionStats,
    settings: Settings,
    mod_dir: Path,
    written: list[Path],
) -> etree._Element | None:
    """Prop actors for every sub-mesh beyond the first, attached at ``root``.

    The engine ships a default ``root`` prop point at the origin on every
    converted model (``PMDConvert::AddDefaultPropPoints``), so extra meshes
    attached there align with the main mesh's base pose. Each prop carries
    its own texture group's baseTex (``mesh_textures``) and, when the
    model is rigged, animation DAEs so texture-split props (e.g. hedir
    sword) follow the same skeletal motion as the body.
    """
    extra = mesh_daes[1:]
    if not extra:
        return None
    material = _material_for(g3d, stats, settings)
    prop_anims = stats.prop_animations.get(g3d, {})
    props = etree.Element("props")
    for idx, dae in enumerate(extra):
        group_index = idx + 1
        prop_path = mod_dir / "art/actors/props" / civ / f"{dae.stem}.xml"
        if prop_path not in written:
            prop_path.parent.mkdir(parents=True, exist_ok=True)
            anims = prop_anims.get(group_index, {})
            prop_actor = _prop_actor(
                civ, dae, _dae_texture(dae, g3d, stats), material, anims
            )
            write_xml(prop_path, prop_actor)
            written.append(prop_path)
        etree.SubElement(props, "prop", actor=f"props/{civ}/{dae.stem}.xml", attachpoint="root")
    return props


def _build_foundation_actor(
    civ: str,
    unit: UnitDef,
    cons: Path,
    des: Path | None,
    stats: MediaConversionStats,
    settings: Settings,
    mod_dir: Path,
) -> Path | None:
    """Foundation actor: construction stages as health-driven variants.

    Foundation hitpoints track construction progress (Foundation.js
    ``Build`` grows them), so the health ratio is the build ratio: the
    ``heavydamage`` selection shows the earliest stage and ``alive`` the
    most complete one. The ``scaffold`` variant exists for
    ``SelectAnimation("scaffold")`` once construction commits.
    """
    cons_daes = stats.models[cons].mesh_daes
    if not cons_daes:
        return None
    n = len(cons_daes)
    stages = {
        "alive": n - 1,
        "lightdamage": max(0, n - 2),
        "mediumdamage": max(0, n - 3),
        # The health selection at placement is heavydamage (Foundation.js
        # starts hitpoints at 1), so it must show the EARLIEST stage: the
        # previous n - 4 skipped the first stage for n >= 5 and a fresh
        # foundation appeared partially built.
        "heavydamage": 0,
    }
    root = etree.Element("actor", version="1")
    etree.SubElement(root, "castshadow")
    group = etree.SubElement(root, "group")
    for name, index in stages.items():
        attrs = {"name": name}
        if name == "alive":
            attrs["frequency"] = "1"
        variant = etree.SubElement(group, "variant", **attrs)
        mesh = etree.SubElement(variant, "mesh")
        mesh.text = f"{civ}/{cons_daes[index].name}"
        texture = _dae_texture(cons_daes[index], cons, stats)
        if texture is not None:
            add_actor_textures(variant, civ, texture)
    death = etree.SubElement(group, "variant", name="death")
    if des is not None:
        des_daes = stats.models[des].mesh_daes
        if des_daes:
            mesh = etree.SubElement(death, "mesh")
            mesh.text = f"{civ}/{des_daes[0].name}"
            texture = _dae_texture(des_daes[0], des, stats)
            if texture is not None:
                add_actor_textures(death, civ, texture)
    anim_group = etree.SubElement(root, "group")
    etree.SubElement(anim_group, "variant", frequency="1", name="Idle")
    etree.SubElement(anim_group, "variant", name="scaffold")
    material = etree.SubElement(root, "material")
    material.text = _material_for(cons, stats, settings)
    path = mod_dir / "art/actors/structures" / civ / f"fndn_{sanitize_mod_name(unit.name)}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xml(path, root)
    return path


def _prop_actor(
    civ: str,
    dae: Path,
    texture: Path | None,
    material: str,
    animations: dict[str, Path] | None = None,
) -> etree._Element:
    root = etree.Element("actor", version="1")
    etree.SubElement(root, "castshadow")
    group = etree.SubElement(root, "group")
    variant = etree.SubElement(group, "variant", frequency="1", name="Base")
    mesh = etree.SubElement(variant, "mesh")
    mesh.text = f"{civ}/{dae.name}"
    if texture is not None:
        add_actor_textures(variant, civ, texture)
    if animations:
        container = etree.SubElement(variant, "animations")
        for name in sorted(animations):
            path = animations[name]
            attrs = {
                "file": f"{civ}/{path.name}",
                "name": name,
                "speed": _ANIM_SPEED_PERCENT,
                # The animation id drives skinned-prop sync: a unit's parts
                # share the same id per name so PickAnimationID keeps them in
                # frame (see tools/validate_animations.py).
                "id": name,
            }
            etree.SubElement(container, "animation", **attrs)
    material_node = etree.SubElement(root, "material")
    material_node.text = material
    return root


def _material_for(g3d: Path, stats: MediaConversionStats, settings: Settings) -> str:
    """``basic_trans_norm_spec`` for models whose meshes declare alpha
    transparency; opaque models use the version-appropriate player
    material."""
    if g3d in stats.transparent_models:
        return "basic_trans_norm_spec.xml"
    return material_for(settings.target_version)


def _add_animations(
    variant: etree._Element,
    civ: str,
    unit: UnitDef,
    g3d: Path,
    stats: MediaConversionStats,
) -> None:
    """Wire the unit's engine animation names to their animation DAEs.

    ``file`` is relative to ``art/animation/``; names match what the
    engine requests (idle/walk/run/attack_*/death/build/repair/gather_*).
    Attack animations get the MegaGlest ``attack-start-time`` as ``event``
    (a progress fraction, like the engine's action point).
    """
    anims = stats.animations.get(g3d)
    if not anims:
        return
    container = etree.SubElement(variant, "animations")
    for name in sorted(anims):
        path = anims[name]
        attrs = {
            "file": f"{civ}/{path.name}",
            "name": name,
            "speed": _ANIM_SPEED_PERCENT,
            # id == name keeps every skinned part of this unit synced; the
            # engine selects the shared id via PickAnimationID.
            "id": name,
        }
        if name in ("attack_melee", "attack_ranged"):
            start = _attack_start_time(unit, name)
            if 0.0 < start < 1.0:
                attrs["event"] = f"{start:.7g}"
        etree.SubElement(container, "animation", **attrs)


def _attack_start_time(unit: UnitDef, name: str) -> float:
    """Fraction of the attack animation at which the hit lands."""
    for skill in unit.skills.values():
        if skill.type != "attack" or skill.attack is None:
            continue
        if engine_animation_names(skill) == (name,):
            return skill.attack.start_time
    return 0.0

