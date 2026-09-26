"""Convert MegaGlest particle systems to 0 A.D. particles/actors.

MegaGlest particles are standalone XML files of three kinds, referenced from
faction/unit/skill data:

- ``<projectile-particle-system>`` — a flying projectile; carries a ``<model>``
  (the arrow/stone mesh) and an optional glow ``<texture>``.
- ``<splash-particle-system>`` — emitted on impact (archer_hit_splash, etc.).
- ``<unit-particle-system>`` — persistent particles attached to a unit
  (healing, glow, boost, mining, range, poison).

0 A.D. has no single equivalent: projectile *models* become ``VisualActor``
projectile actors on the attack (``art/actors/props/...``), splash/unit
particles become ``art/particles/*.xml`` systems, and their textures are
converted to ``art/textures/particles/*.png``.

Only the fields 0 A.D. can express are mapped; everything else degrades
gracefully (recorded as a warning), since the two particle models are
fundamentally different.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree

from ..converters.texture_converter import texture_stem
from ..core.errors import ConversionError
from ..megaglest.xmlutil import XmlNode, parse_xml
from .common import add_actor_textures, write_xml
from .mod_builder import sanitize_mod_name

if TYPE_CHECKING:
    from ..core.config import Settings
    from ..core.media_conversion import MediaConversionStats
    from ..megaglest.civ_loader import Faction

LOGGER = logging.getLogger(__name__)


@dataclass
class MGParticle:
    """A parsed MegaGlest particle-system XML (any of the three kinds)."""

    path: Path
    kind: str  # "projectile" | "splash" | "unit"
    texture: Path | None = None
    luminance: bool = False
    model: Path | None = None  # projectile mesh G3D, resolved to an absolute path
    primitive: str = "quad"
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    direction: tuple[float, float, float] = (0.0, 0.0, 0.0)
    color: tuple[float, float, float, float] | None = None
    size: float = 1.0
    speed: float = 0.0
    gravity: float = 0.0
    emission_rate: float = 1.0
    energy_max: float = 1.0
    energy_var: float = 0.0
    radius: float = 0.0
    fixed: bool = False
    unmapped: list[str] = field(default_factory=list)


def parse_particle(path: Path) -> MGParticle:
    """Parse one MegaGlest particle XML into a :class:`MGParticle`.

    Raises :class:`ConversionError` if the file is malformed or its root is
    not a recognised particle-system element.
    """
    root = parse_xml(path)
    if not root.tag.endswith("-particle-system"):
        raise ConversionError(f"{path.name}: unknown particle root <{root.tag}>")
    kind_name = root.tag[: -(len("-particle-system"))]
    kind = {"projectile": "projectile", "splash": "splash", "unit": "unit"}.get(
        kind_name, "unit"
    )

    particle = MGParticle(path=path, kind=kind)

    texture_node = root.get("texture")
    if texture_node is not None and texture_node.bool_value(True) is not False:
        ref = texture_node.path_attr()
        if ref:
            candidate = _absolute(path.parent, ref)
            if candidate is not None:
                particle.texture = candidate
        lum = texture_node.attr("luminance")
        particle.luminance = lum is not None and lum.strip().lower() in {"true", "1"}

    model_node = root.get("model")
    if model_node is not None and model_node.bool_value(False):
        ref = model_node.path_attr()
        if ref:
            candidate = _absolute(path.parent, ref)
            if candidate is not None:
                particle.model = candidate

    prim = root.get("primitive")
    if prim is not None:
        particle.primitive = str(prim.value() or "quad")

    offset = _vec3(root.get("offset"))
    if offset is not None:
        particle.offset = offset
    direction = _vec3(root.get("direction"))
    if direction is not None:
        particle.direction = direction

    color_node = root.get("color")
    if color_node is not None:
        particle.color = (
            _node_f(color_node, "red", 1.0),
            _node_f(color_node, "green", 1.0),
            _node_f(color_node, "blue", 1.0),
            _node_f(color_node, "alpha", 1.0),
        )

    particle.size = _tag_f(root, "size", 1.0)
    particle.speed = _tag_f(root, "speed", 0.0)
    particle.gravity = _tag_f(root, "gravity", 0.0)
    particle.emission_rate = _tag_f(root, "emission-rate", 1.0)
    particle.energy_max = _tag_f(root, "energy-max", 1.0)
    particle.energy_var = _tag_f(root, "energy-var", 0.0)
    particle.radius = _tag_f(root, "radius", 0.0)

    fixed = root.get("fixed")
    if fixed is not None:
        value = fixed.bool_value(None)
        if value is not None:
            particle.fixed = value

    known = {
        "texture", "model", "primitive", "offset", "direction", "color",
        "color-no-energy", "size", "size-no-energy", "speed", "gravity",
        "emission-rate", "energy-max", "energy-var", "radius", "fixed",
        "relative", "relativeDirection", "teamcolorEnergy", "teamcolorNoEnergy",
        "static-particle-count", "trajectory", "mode", "isVisibleAtNight",
        "isVisibleAtDay", "isDaylightAffected", "particleSystemStartDelay",
        "radiusBasedStartenergy", "shape", "alternations", "spawn-initial",
        "fade-in", "fade-out", "proj-min-size", "proj-max-size",
    }
    for child in root.children:
        if child.tag not in known:
            particle.unmapped.append(child.tag)
    return particle


def _absolute(base: Path, ref: str) -> Path | None:
    if ref.startswith("/"):
        ref = ref.lstrip("/")
    candidate = Path(ref)
    if candidate.is_absolute():
        return candidate
    return (base / candidate).resolve()


def _node_f(node: XmlNode, attr: str, default: float) -> float:
    raw = node.attr(attr)
    if raw is None:
        return default
    try:
        return float(str(raw))
    except ValueError:
        return default


def _vec3(node: XmlNode | None) -> tuple[float, float, float] | None:
    if node is None:
        return None
    try:
        x = float(node.attr("x") or 0.0)
        y = float(node.attr("y") or 0.0)
        z = float(node.attr("z") or 0.0)
    except ValueError:
        return None
    return (x, y, z)


def _tag_f(root: XmlNode, tag: str, default: float) -> float:
    node = root.get(tag)
    if node is None:
        return default
    value = node.float_value()
    return default if value is None else float(value)


def _fmt(value: float) -> str:
    return f"{value:.6g}"



# ---------------------------------------------------------------------------
# 0 A.D. particle-system writer
# ---------------------------------------------------------------------------


def write_particle_system(
    particle: MGParticle,
    texture_png: Path | None,
    mod_dir: Path,
    out_path: Path,
) -> None:
    """Write ``particle`` as a 0 A.D. ``art/particles/*.xml`` system.

    ``texture_png`` is the converted particle texture written to
    ``art/textures/particles/<name>.png``; the ``<texture>`` tag resolves
    relative to the mod root (public uses ``art/textures/particles/...``).
    """
    root = etree.Element("particles")

    if texture_png is not None:
        rel = texture_png.relative_to(mod_dir)
        etree.SubElement(root, "texture").text = rel.as_posix()
    blend = etree.SubElement(root, "blend")
    blend.set("mode", "add" if particle.luminance else "over")

    if particle.fixed or particle.speed <= 0.0:
        etree.SubElement(root, "start_full")

    etree.SubElement(
        root, "constant", name="emissionrate", value=_fmt(particle.emission_rate)
    )

    maybe_uniform_lifetime(root, particle.energy_max, particle.energy_var)

    ox, oy, oz = particle.offset
    for name, value in (("position.x", ox), ("position.y", oy), ("position.z", oz)):
        etree.SubElement(root, "constant", name=name, value=_fmt(value))

    etree.SubElement(
        root, "uniform", name="angle", min=_fmt(-3.14), max=_fmt(3.14)
    )

    vx, vy, vz = particle.direction
    speed = particle.speed
    _velocity_uniform(root, "velocity.x", vx * speed)
    _velocity_uniform(root, "velocity.y", vy * speed)
    _velocity_uniform(root, "velocity.z", vz * speed)

    if particle.size > 0:
        lo = particle.size * 0.75
        hi = particle.size * 1.25
        etree.SubElement(
            root, "uniform", name="size", min=_fmt(lo), max=_fmt(hi)
        )

    if particle.color is not None:
        r, g, b, _a = particle.color
        etree.SubElement(root, "uniform", name="color.r", min=_fmt(r), max=_fmt(r))
        etree.SubElement(root, "uniform", name="color.g", min=_fmt(g), max=_fmt(g))
        etree.SubElement(root, "uniform", name="color.b", min=_fmt(b), max=_fmt(b))

    if particle.gravity:
        etree.SubElement(root, "force", y=_fmt(-particle.gravity))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_xml(out_path, root)


def maybe_uniform_lifetime(root: etree._Element, center: float, spread: float) -> None:
    lo = max(0.05, center - spread)
    hi = max(lo + 0.05, center + spread)
    etree.SubElement(root, "uniform", name="lifetime", min=_fmt(lo), max=_fmt(hi))


def _velocity_uniform(root: etree._Element, name: str, base: float) -> None:
    if base == 0.0:
        return
    jitter = abs(base) * 0.25
    etree.SubElement(
        root, "uniform", name=name, min=_fmt(base - jitter), max=_fmt(base + jitter)
    )


# ---------------------------------------------------------------------------
# Orchestration: convert every referenced particle for a faction
# ---------------------------------------------------------------------------

def convert_particles(
    faction: Faction,
    mod_dir: Path,
    settings: Settings,
    stats: MediaConversionStats,
) -> None:
    """Convert the faction's referenced particles into the 0 A.D. mod.

    Handles three outputs:

    - ``art/particles/*.xml`` systems for splash/unit particles.
    - ``art/textures/particles/*.png`` from the particle texture sprites,
      content-deduped.
    - ``art/actors/props/{civ}/*_projectile.xml`` (+ impact actors) for the
      projectile meshes referenced by ``<projectile-particle-system>``.

    Wiring (we return tables on ``stats`` for the template/actor generators):

    - ``stats.projectile_actor[g3d]`` / ``stats.projectile_impact_actor[g3d]``
      -> paths for the ``<Projectile>`` ``ActorName``/``ImpactActorName``.
    - ``stats.unit_particle_props[unit]`` -> particle actor paths to attach.
    """
    from ..converters.texture_converter import TextureConverter

    if settings.skip_media:
        return
    civ = sanitize_mod_name(faction.name)

    particles_dir = mod_dir / "art/particles"
    textures_dir = mod_dir / "art/textures/particles"
    props_dir = mod_dir / "art/actors/props" / civ

    converter = TextureConverter()
    particle_xml: dict[Path, MGParticle] = {}
    for ref in _referenced_particles(faction):
        if ref in particle_xml:
            continue
        try:
            particle_xml[ref] = parse_particle(ref)
        except ConversionError as exc:
            stats.warnings.append(f"particle {ref.name}: {exc}")

    texture_source_to_png: dict[Path, Path] = {}
    written_systems: set[str] = set()
    written_textures: set[Path] = set()
    for path, particle in particle_xml.items():
        # Convert / reference the particle texture.
        png = None
        if particle.texture is not None:
            png = _convert_particle_texture(
                particle.texture, textures_dir, converter, stats,
                texture_source_to_png, written_textures,
            )
            if png is not None:
                stats.particle_textures[particle.texture] = png

        if particle.kind in ("splash", "unit") and particle.texture is not None:
            stem = _unique_name(particle.path, written_systems)
            out = particles_dir / f"{stem}.xml"
            write_particle_system(particle, png, mod_dir, out)
            stats.particle_systems[path] = out
            stats.generated.append(str(out.relative_to(mod_dir)))
            _write_particle_actor(mod_dir, stem)
            actor_rel = str(
                (mod_dir / "art/actors/particle" / f"{stem}.xml").relative_to(mod_dir)
            )
            stats.generated.append(actor_rel)

        if particle.kind == "projectile" and particle.model is not None:
            _convert_projectile(particle, civ, props_dir, stats)

    _wire_unit_particles(faction, stats)


def _referenced_particles(faction: Faction) -> set[Path]:
    refs: set[Path] = set()
    refs.update(faction.particles)
    for unit in faction.units.values():
        for skill in unit.skills.values():
            refs.update(skill.particles)
            if skill.attack is not None:
                if skill.attack.projectile_particle is not None:
                    refs.add(skill.attack.projectile_particle)
                if skill.attack.splash_particle is not None:
                    refs.add(skill.attack.splash_particle)
    return refs


def _convert_particle_texture(
    source: Path,
    textures_dir: Path,
    converter,
    stats: MediaConversionStats,
    cache: dict[Path, Path],
    written: set[Path],
) -> Path | None:
    """Convert a particle texture to PNG (content-deduped)."""
    if source in cache:
        return cache[source]
    if not source.is_file():
        stats.warnings.append(f"particle texture {source.name} not found")
        return None
    from ..core.media_conversion import _unique_path

    stem = texture_stem(source.stem)
    output = _unique_path(textures_dir, stem, ".png", written)
    try:
        converter.convert_to_png(source, output)
    except ConversionError as exc:
        stats.warnings.append(str(exc))
        return None
    cache[source] = output
    written.add(output)
    stats.generated.append(str(output.relative_to(textures_dir.parent.parent.parent)))
    return output


def _unique_name(path: Path, used: set[str]) -> str:
    stem = texture_stem(path.stem)
    candidate = stem
    counter = 1
    while candidate in used:
        candidate = f"{stem}_{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _convert_projectile(
    particle: MGParticle,
    civ: str,
    props_dir: Path,
    stats: MediaConversionStats,
) -> None:
    """Generate a projectile + impact actor for one projectile particle.

    The projectile mesh must already have been converted to a DAE (it lives
    in ``stats.models``); the actor references that DAE.
    """
    model = _match_model(particle.model, stats)
    mesh = stats.models.get(model)
    if mesh is None or not mesh.mesh_daes:
        stats.warnings.append(f"projectile {particle.path.name}: model not converted")
        return
    dae = mesh.mesh_daes[0]
    texture = stats.mesh_textures.get(dae) or stats.model_texture.get(model)

    stem = texture_stem(dae.stem) + "_projectile"
    actor_rel = f"props/{civ}/{stem}.xml"

    # Dedup: several units share one projectile mesh (e.g. archer_arrow.g3d).
    for g3d, rel in stats.projectile_actor.items():
        if rel == actor_rel:
            stats.projectile_actor[model] = rel
            if model not in stats.projectile_impact_actor:
                stats.projectile_impact_actor[model] = stats.projectile_impact_actor[g3d]
            stats.projectile_actor_by_particle[particle.path] = rel
            return

    actor_path = props_dir / f"{stem}.xml"
    _write_mesh_actor(civ, dae, texture, actor_path)
    stats.projectile_actor[model] = actor_rel
    stats.projectile_actor_by_particle[particle.path] = actor_rel
    stats.generated.append(str(actor_path.relative_to(mod_dir_of(props_dir))))

    impact = actor_path.with_name(f"{stem}_impact.xml")
    impact_rel = f"props/{civ}/{stem}_impact.xml"
    _write_mesh_actor(civ, dae, texture, impact)
    stats.projectile_impact_actor[model] = impact_rel
    stats.generated.append(str(impact.relative_to(mod_dir_of(props_dir))))


def mod_dir_of(props_dir: Path) -> Path:
    """The mod root: ``art/actors/props/<civ>`` -> ``art/../../..``."""
    return props_dir.parent.parent.parent.parent


def _match_model(model: Path, stats) -> Path | None:
    if model is None:
        return None
    key = model.resolve()
    if key in stats.models:
        return key
    for g3d in stats.models:
        if g3d.resolve() == key:
            return g3d
    return None


def _write_mesh_actor(civ: str, dae: Path, texture: Path | None, out_path: Path) -> None:
    """Write a shadowless mesh actor.

    Serves both the flying projectile (a VideoMotion actor) and the impact
    burst it spawns; the two differed only in their docstring.
    """
    root = etree.Element("actor", version="1")
    etree.SubElement(root, "castshadow")
    group = etree.SubElement(root, "group")
    variant = etree.SubElement(group, "variant", frequency="1", name="Base")
    mesh = etree.SubElement(variant, "mesh")
    mesh.text = f"{civ}/{dae.name}"
    if texture is not None:
        add_actor_textures(variant, civ, texture)
    material = etree.SubElement(root, "material")
    material.text = "no_trans_norm_spec.xml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_xml(out_path, root)


def _write_particle_actor(mod_dir: Path, stem: str) -> None:
    """Actor wrapper so the particle system can attach as an actor prop.

    0 A.D. references particles from other actors via ``<prop
    actor="particle/<stem>.xml" .../>``, where the referenced file is an actor
    whose variant carries ``<particles file="<stem>.xml"/>`` (that ``file`` is
    relative to ``art/particles/``).
    """
    out = mod_dir / "art/actors/particle" / f"{stem}.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    root = etree.Element("actor", version="1")
    group = etree.SubElement(root, "group")
    variant = etree.SubElement(group, "variant", name="Base")
    etree.SubElement(variant, "particles", file=f"{stem}.xml")
    write_xml(out, root)




def _wire_unit_particles(
    faction: Faction,
    stats: MediaConversionStats,
) -> None:
    """Attach unit particle systems (healing/glow/boost/mining/range) to units.

    0 A.D. attaches persistent particles to an actor as a ``particle/...``
    prop. We emit a small "actor" that merely references the particle system
    for each unit particle, and record unit -> prop-actor paths on
    ``stats.unit_particle_props`` for the unit actor generator.
    """
    for _, unit in faction.units.items():
        props: list[str] = []
        seen: set[str] = set()
        for skill in unit.skills.values():
            for pxml in skill.particles:
                system = stats.particle_systems.get(pxml)
                if system is None:
                    continue
                # Props reference the particle *actor* (art/actors/particle/...),
                # which the engine resolves relative to art/actors/.
                rel = f"particle/{system.stem}.xml"
                if rel in seen:
                    continue
                seen.add(rel)
                props.append(rel)
        if props:
            stats.unit_particle_props[sanitize_mod_name(unit.name)] = props
