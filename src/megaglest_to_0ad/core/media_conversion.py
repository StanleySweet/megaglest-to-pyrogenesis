"""Faction media conversion: meshes, textures, audio into the mod tree."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from ..converters.audio_converter import AudioConverter
from ..converters.mesh_converter import (
    ConvertedMesh,
    MeshConverter,
    diffuse_texture_names,
    read_g3d,
    texture_groups,
)
from ..converters.rig import Rig, build_rig
from ..converters.texture_converter import (
    _TEXTURE_EXTS,
    TextureConverter,
    texture_stem,
)
from ..core.config import Settings
from ..core.errors import ConversionError
from ..megaglest.civ_loader import Faction, SkillDef, UnitDef
from ..oad.mod_builder import sanitize_mod_name
from ..oad.particle_converter import convert_particles
from ..oad.skeleton_writer import write_skeletons

if TYPE_CHECKING:
    import g3dlib  # vendored; importable after sys.path bootstrap (see mesh_converter)


_PROP_ALPHA_IS_TRANSPARENCY = 16  # g3dlib.PROP_ALPHA_IS_TRANSPARENCY
LOGGER = logging.getLogger(__name__)


def _has_alpha_channel(png: Path, cache: dict[Path, bool]) -> bool:
    """True if ``png`` (an RGBA PNG from TextureConverter) has a real alpha
    channel: any pixel below half opacity. Texture alpha drives the actor's
    transparent material; near-opaque bands (alpha >= 128 everywhere, e.g.
    baked matte noise on RGB textures) do not count as transparency.
    """
    if png in cache:
        return cache[png]
    with Image.open(png) as img:
        if img.mode not in ("RGBA", "LA", "PA"):
            cache[png] = False
            return False
        extrema = img.getextrema()
        alpha = extrema[-1]
        if isinstance(alpha, tuple):
            alpha = alpha[0]
        cache[png] = alpha < 128
    return cache[png]

LOGGER = logging.getLogger(__name__)


# MegaGlest skill types -> the 0 A.D. engine animation names the actor
# wires for them. One entry per engine name; aliases share one file.
_ENGINE_ANIM_NAMES: dict[str, tuple[str, ...]] = {
    "stop": ("idle",),
    "move": ("walk", "run"),
    "die": ("death",),
    "harvest": (
        "gather_food",
        "gather_wood",
        "gather_stone",
        "gather_metal",
        "gather_fruit",
    ),
    "build": ("build",),
    "repair": ("repair",),
}

# Skill types with animations but no engine-requested name: the DAE is
# still emitted (one file per skill animation) but the actor does not wire
# it (the engine never plays these states).
_UNWIRED_ANIM_TYPES = frozenset({"produce", "upgrade", "morph", "be_built"})

# Minimum vertices for a bone beyond the root (else the bone count drops).
_VERTICES_PER_BONE = 40

# Process-pool width for the rig/mesh and animation phases. The rig math
# (k-means clustering + per-frame Kabsch fits) is pure-Python and
# GIL-bound, so threads cannot help; one process per core wins ~Nx on
# the conversion's slowest phase.
_MAX_WORKERS = min(8, os.cpu_count() or 1)


@dataclass
class MediaConversionStats:
    """Counts and output paths from one faction's media conversion."""

    meshes: int = 0
    animation_count: int = 0
    textures: int = 0
    sounds: int = 0
    music: int = 0
    generated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    models: dict[Path, ConvertedMesh] = field(default_factory=dict)
    # g3d source -> baseTex PNG written for it (Phase 4 actor generation)
    model_texture: dict[Path, Path] = field(default_factory=dict)
    # DAE -> its baseTex PNG (per texture group; prop actors need the
    # group's own texture, not the model's first one)
    mesh_textures: dict[Path, Path] = field(default_factory=dict)
    # g3d sources whose meshes declare alpha-is-transparency or opacity < 1
    # (actor material choice; see g3dlib.PROP_ALPHA_IS_TRANSPARENCY)
    transparent_models: set[Path] = field(default_factory=set)
    # written audio/music filenames, for the civ.json Music list
    music_files: list[str] = field(default_factory=list)
    # base g3d -> engine animation name -> animation DAE (actor wiring)
    animations: dict[Path, dict[str, Path]] = field(default_factory=dict)
    # base g3d -> group index -> engine animation name -> animation DAE
    # (prop actors for texture-split groups need their own animation DAEs)
    prop_animations: dict[Path, dict[int, dict[str, Path]]] = field(default_factory=dict)
    # MG particle XML (abs) -> written art/particles/*.xml (abs, inside mod)
    particle_systems: dict[Path, Path] = field(default_factory=dict)
    # MG particle texture (abs) -> written art/textures/particles/*.png (abs)
    particle_textures: dict[Path, Path] = field(default_factory=dict)
    # projectile mesh g3d (abs) -> projectile actor path (rel to art/actors/)
    projectile_actor: dict[Path, str] = field(default_factory=dict)
    # projectile mesh g3d (abs) -> impact actor path (rel to art/actors/)
    projectile_impact_actor: dict[Path, str] = field(default_factory=dict)
    # MG projectile particle XML (abs) -> projectile actor path (rel to art/actors/)
    projectile_actor_by_particle: dict[Path, str] = field(default_factory=dict)
    # unit name -> list of particle actor paths (rel to art/actors/) to attach
    unit_particle_props: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# process-pool workers (rig math is pure-Python and GIL-bound; each worker
# parses, rigs and writes one model / one animation DAE)
# ---------------------------------------------------------------------------


@dataclass
class _MeshTask:
    """One model's DAE conversion, executed in a worker process."""

    g3d_path: Path
    mesh_stem: str
    civ: str
    meshes_dir: Path
    rig_bones: int
    groups: list[list[int]] | None
    group_textures: list[Path | None] | None
    bone_count: int
    root_name: str


@dataclass
class _MeshTaskResult:
    """Per-model worker outcome; ``ConversionError`` degrades to warnings."""

    mesh: ConvertedMesh | None = None
    rig: Rig | None = None
    mesh_stem: str = ""
    warnings: list[str] = field(default_factory=list)


def _group_texture_keys(
    model: g3dlib.G3DModel,
    groups: list[list[int]],
    model_dir: Path,
) -> list[Path | None]:
    """Resolved diffuse texture source per mesh group (None = untextured)."""
    names = diffuse_texture_names(model)
    keys: list[Path | None] = []
    for group in groups:
        key = next(
            (_resolve_texture(model_dir, names[i]) for i in group if names[i]),
            None,
        )
        keys.append(key)
    return keys


def _convert_mesh_task(task: _MeshTask) -> _MeshTaskResult:
    """Worker: parse, rig, and write one model's DAEs."""
    result = _MeshTaskResult(mesh_stem=task.mesh_stem)
    try:
        model = read_g3d(task.g3d_path)
        rig = None
        if task.bone_count > 0:
            rig = build_rig(model, task.groups, task.bone_count, task.root_name)
        result.mesh = MeshConverter().convert_g3d_to_dae(
            task.g3d_path,
            task.meshes_dir,
            task.civ,
            stem=task.mesh_stem,
            rig=rig,
            groups=None if rig is not None else task.groups,
        )
        result.rig = rig
    except ConversionError as exc:
        result.warnings.append(f"{task.g3d_path.name}: {exc}")
    return result


@dataclass
class _AnimTask:
    """One animation DAE write, executed in a worker process."""

    base_path: Path
    anim_path: Path
    base_stem: str
    key: str
    names: tuple[str, ...]
    rig: Rig
    civ: str
    output: Path
    anim_speed: float
    loop: bool
    use_base_weights: bool
    unit_name: str
    anim_name: str
    group_index: int = 0


@dataclass
class _AnimTaskResult:
    """Per-animation worker outcome."""

    ok: bool = False
    warnings: list[str] = field(default_factory=list)


def _convert_animation_task(task: _AnimTask) -> _AnimTaskResult:
    """Worker: re-read both models and write one animation DAE.

    ``use_base_weights`` is decided in the parent (model identity), since
    worker-parsed copies of the same file are never ``is``-identical.
    """
    result = _AnimTaskResult()
    try:
        base_model = read_g3d(task.base_path)
        anim_model = read_g3d(task.anim_path)
        MeshConverter().write_animation_dae(
            base_model,
            anim_model,
            task.rig,
            task.output,
            task.civ,
            task.anim_speed,
            loop=task.loop,
            use_base_weights=task.use_base_weights,
            group_index=task.group_index,
        )
        result.ok = True
    except ConversionError as exc:
        result.warnings.append(f"{task.unit_name}: {task.anim_name}: {exc}")
    return result


def _run_tasks(fn: Callable[[object], object], tasks: list[object]) -> list[tuple[object, object]]:
    """Run ``fn`` over ``tasks`` in a process pool, preserving order.

    Falls back to sequential execution for tiny batches: spawning a pool
    costs ~0.5 s per worker on macOS and is wasted on a handful of tasks.
    """
    if not tasks:
        return []
    if len(tasks) <= 2 or _MAX_WORKERS <= 1:
        return [(task, fn(task)) for task in tasks]
    with ProcessPoolExecutor(max_workers=min(_MAX_WORKERS, len(tasks))) as pool:
        return list(zip(tasks, pool.map(fn, tasks)))


def convert_faction_media(
    faction: Faction,
    mod_dir: Path,
    settings: Settings,
    resources_dir: Path | None = None,
) -> MediaConversionStats:
    """Convert one faction's models, textures, portraits and audio.

    Outputs (all under ``mod_dir``):

    - ``art/meshes/{civ}/*.dae`` (skinned per texture group, or static)
    - ``art/animation/{civ}/*.dae`` (one per skill animation)
    - ``art/skeletons/{civ}.xml`` (joint contracts of the rigged models)
    - ``art/textures/skins/units/{civ}/*.png`` (mesh textures)
    - ``art/textures/ui/session/portraits/units/{civ}/*.png`` (icons)
    - ``audio/sfx/{civ}/*.ogg`` + ``audio/groups/*.xml`` (sounds)
    - ``audio/music/*.ogg`` (faction music)
    """
    if settings.skip_media:
        return MediaConversionStats()

    civ = sanitize_mod_name(faction.name)
    texture_converter = TextureConverter()
    audio_converter = AudioConverter()
    stats = MediaConversionStats()
    meshes_dir = mod_dir / "art/meshes" / civ
    anim_dir = mod_dir / "art/animation" / civ
    skeletons_dir = mod_dir / "art/skeletons"
    textures_dir = mod_dir / "art/textures/skins/units" / civ
    portraits_dir = mod_dir / "art/textures/ui/session/portraits/units" / civ
    tech_portraits_dir = mod_dir / "art/textures/ui/session/portraits/technologies"
    sfx_dir = mod_dir / "audio/sfx" / civ
    music_dir = mod_dir / "audio/music"
    groups_dir = mod_dir / "audio/groups"

    converted_textures: dict[Path, Path] = {}
    texture_by_hash: dict[str, Path] = {}
    written: set[Path] = set()
    written_mesh_names: set[str] = set()
    png_alpha: dict[Path, bool] = {}

    models = sorted(_collect_models(faction, resources_dir))
    model_cache: dict[Path, g3dlib.G3DModel] = {}
    for g3d_path in models:
        try:
            model_cache[g3d_path] = read_g3d(g3d_path)
        except ConversionError as exc:
            stats.warnings.append(f"{g3d_path.name}: {exc}")
    base_g3ds = _unit_base_models(faction, model_cache)
    rigs: dict[Path, Rig] = {}
    rig_stems: dict[Path, str] = {}
    mesh_tasks: list[_MeshTask] = []
    for g3d_path in models:
        model = model_cache.get(g3d_path)
        if model is None:
            continue
        # Mesh-referenced textures convert for the Phase 4 actors (the DAE
        # itself stays geometry-only per the 0 A.D. importer contract).
        # Dedup is by content, not by resolved path: packs copy one art file
        # into many unit dirs, so the same bytes must produce exactly one
        # output (``texture_by_hash``) instead of N near-identical copies.
        for name in diffuse_texture_names(model):
            if not name:
                continue
            source = _resolve_texture(g3d_path.parent, name)
            if source is None:
                stats.warnings.append(f"{g3d_path.name}: texture {name!r} not found")
                continue
            if source in converted_textures:
                continue
            digest = _file_sha256(source)
            canonical = texture_by_hash.get(digest)
            if canonical is not None:
                converted_textures[source] = canonical
                continue
            output = _unique_path(textures_dir, texture_stem(Path(name).name), ".png", written)
            try:
                texture_converter.convert_to_png(source, output)
            except ConversionError as exc:
                stats.warnings.append(str(exc))
                continue
            converted_textures[source] = output
            texture_by_hash[digest] = output
            written.add(output)
        # Transparent material at actor time: either the G3D mesh declares
        # alpha-is-transparency (or sub-1 opacity), or any of its diffuse
        # textures actually carries an alpha channel (TGA/PNG with
        # alpha, so those models need basic_trans_norm_spec.xml. Per-model so
        # the actor generator does not re-parse the G3D or textures.
        transparent = any(
            (mesh.properties & _PROP_ALPHA_IS_TRANSPARENCY) or mesh.opacity < 0.999
            for mesh in model.meshes
        )
        if not transparent:
            # Any diffuse texture with a real alpha channel makes the model
            # transparent: the engine's player material ignores texture
            # alpha, so the actor must use basic_trans_norm_spec.xml instead.
            for name in diffuse_texture_names(model):
                if not name:
                    continue
                source = _resolve_texture(g3d_path.parent, name)
                if source is None or source not in converted_textures:
                    continue
                if _has_alpha_channel(converted_textures[source], png_alpha):
                    transparent = True
                    break
        if transparent:
            stats.transparent_models.add(g3d_path)
        if g3d_path not in stats.model_texture:
            first = next((n for n in diffuse_texture_names(model) if n), None)
            if first is not None:
                source = _resolve_texture(g3d_path.parent, first)
                if source is not None and source in converted_textures:
                    stats.model_texture[g3d_path] = converted_textures[source]
        # Same-named models from different directories (e.g. three distinct
        # stone.g3d) must not clobber each other: dedupe the output stem and
        # record the mapping so the Phase 4 actor generator references the
        # exact DAEs written for each source model. The dedup avoids the
        # whole ``{stem}_NN`` family so it can't collide with multi-mesh
        # split names either.
        is_base = g3d_path in base_g3ds
        is_cons = g3d_path.stem.endswith("_cons")
        resolve = lambda name: _resolve_texture(g3d_path.parent, name)  # noqa: E731
        if is_base and _is_animated(model):
            groups = texture_groups(model, resolve)
        elif not is_cons:
            # Static models: same-texture subobjects merge into one DAE
            # (one instanced object per file). Construction-stage models
            # keep one DAE per mesh so the foundation actor can switch
            # stages by health.
            groups = texture_groups(model, resolve)
        else:
            groups = [[i] for i in range(len(model.meshes))]
        n_outputs = len(groups)
        group_textures = _group_texture_keys(model, groups, g3d_path.parent)
        mesh_stem = g3d_path.stem
        counter = 0
        while _mesh_names(mesh_stem, n_outputs) & written_mesh_names:
            counter += 1
            mesh_stem = f"{g3d_path.stem}_{counter:02d}"
        # Base models of animated units get a synthesized rig; the DAE then
        # carries merged per-texture geometry + skin + armature. Other
        # models stay static base-pose DAEs. Rig construction (k-means
        # clustering) is the conversion's slowest phase by far — defer it
        # to the process pool below, one model per worker.
        if is_base and _is_animated(model):
            group_vertices = sum(model.meshes[i].vertex_count for i in groups[0])
            # Scale bones with model complexity: more vertices need more
            # rigid parts to avoid Kabsch distortion.  The old formula
            # capped at rig_bones (6) which was far too few for large
            # models (ent: 2465 vertices → 6 bones → ~493 vertices per
            # Kabsch fit → severe distortion).
            bone_count = max(
                4,
                min(settings.rig_bones, group_vertices // _VERTICES_PER_BONE + 2),
            )
            root_name = f"{civ}_{mesh_stem}_root"
        else:
            bone_count = 0
            root_name = ""
        mesh_tasks.append(
            _MeshTask(
                g3d_path=g3d_path,
                mesh_stem=mesh_stem,
                civ=civ,
                meshes_dir=meshes_dir,
                rig_bones=settings.rig_bones,
                groups=groups,
                group_textures=group_textures,
                bone_count=bone_count,
                root_name=root_name,
            )
        )
        written_mesh_names |= _mesh_names(mesh_stem, n_outputs)

    for task, result in _run_tasks(_convert_mesh_task, mesh_tasks):
        stats.warnings.extend(result.warnings)
        if result.mesh is None:
            continue
        g3d_path = task.g3d_path
        stats.models[g3d_path] = result.mesh
        stats.meshes += 1
        stats.warnings.extend(result.mesh.warnings)
        stats.generated.extend(_rel(mod_dir, path) for path in result.mesh.mesh_daes)
        if task.group_textures:
            for group_index, dae in enumerate(result.mesh.mesh_daes):
                source = (
                    task.group_textures[group_index]
                    if group_index < len(task.group_textures)
                    else None
                )
                if source is not None and source in converted_textures:
                    stats.mesh_textures[dae] = converted_textures[source]
        if result.rig is not None:
            rigs[g3d_path] = result.rig
            rig_stems[g3d_path] = result.mesh_stem

    _convert_animations(faction, mod_dir, anim_dir, civ, model_cache, stats, rigs, rig_stems)
    write_skeletons(
        [(rig_stems[g3d], rigs[g3d]) for g3d in sorted(rigs)],
        skeletons_dir,
        civ,
    )
    _convert_portraits(faction, portraits_dir, texture_converter, mod_dir, stats, written)
    _convert_tech_portraits(faction, tech_portraits_dir, texture_converter, mod_dir, stats)
    _convert_sounds(faction, sfx_dir, groups_dir, audio_converter, mod_dir, stats, written)
    _convert_music(faction, music_dir, audio_converter, mod_dir, stats, written)
    convert_particles(faction, mod_dir, settings, stats)

    for warning in stats.warnings:
        LOGGER.warning("media: %s", warning)
    return stats


# ---------------------------------------------------------------------------
# animation emission
# ---------------------------------------------------------------------------


def engine_animation_names(skill: SkillDef) -> tuple[str, ...]:
    """Engine animation names the actor wires for ``skill`` (empty = unwired).

    Mirrors the engine's requests: ``attack_<type>`` where the type is
    chosen by the attack stats (melee unless ranged), and the fixed names
    for stop/move/die/harvest/build/repair. Unwired types (produce,
    upgrade, morph, be_built) still get an animation file, never a wire.
    """
    if skill.type == "attack":
        attack = skill.attack
        ranged = attack is not None and (attack.range > 4 or attack.projectile)
        return ("attack_ranged",) if ranged else ("attack_melee",)
    return _ENGINE_ANIM_NAMES.get(skill.type, ())


def _convert_animations(
    faction: Faction,
    mod_dir: Path,
    anim_dir: Path,
    civ: str,
    model_cache: dict[Path, g3dlib.G3DModel],
    stats: MediaConversionStats,
    rigs: dict[Path, Rig],
    rig_stems: dict[Path, str],
) -> None:
    """One animation DAE per unit skill, per rigged base model, per group.

    Each DAE reuses the base model's group geometry + skin and adds the
    skill model's per-frame joint transforms (``write_animation_dae``).
    Files are deduped by (base stem, group_index, key) — units sharing a
    base model share its animation files; a conflicting second source warns
    and loses.  Group-0 animations go to ``stats.animations`` (the main
    actor); non-zero groups go to ``stats.prop_animations`` (prop actors
    for texture-split meshes like the hedir sword).
    """
    anim_dir.mkdir(parents=True, exist_ok=True)
    tasks: list[_AnimTask] = []
    paths: dict[tuple[str, int, str], Path] = {}
    for unit in faction.units.values():
        base = _first_skill_model(unit, model_cache)
        rig = rigs.get(base) if base is not None else None
        if rig is None:
            continue
        base_stem = rig_stems[base]
        base_model = model_cache[base]
        for skill in unit.skills.values():
            if skill.animation is None:
                continue
            anim_model = model_cache.get(skill.animation.resolve())
            if anim_model is None:
                continue
            if len(anim_model.meshes) <= max(rig.groups[0].mesh_indices):
                stats.warnings.append(
                    f"{unit.name}: {skill.animation.name} has fewer meshes than the base "
                    "model; animation skipped"
                )
                continue
            names = engine_animation_names(skill)
            if names:
                keys = list(names)
            elif skill.type in _UNWIRED_ANIM_TYPES:
                keys = [skill.type]
            else:
                continue
            # Generate animation DAEs for every rigged group (group 0 is
            # the main actor; non-zero groups are texture-split props like
            # the hedir sword that need their own animation DAEs).
            for gi in range(len(rig.groups)):
                group = rig.groups[gi]
                if len(anim_model.meshes) <= max(group.mesh_indices):
                    continue
                frame_count = min(
                    anim_model.meshes[i].frame_count for i in group.mesh_indices
                )
                if frame_count < 2:
                    continue
                for key in keys:
                    dedupe_key = (base_stem, gi, key)
                    if dedupe_key in paths:
                        continue
                    suffix = f"_g{gi + 1:02d}_{key}" if gi > 0 else f"_{key}"
                    paths[dedupe_key] = anim_dir / f"{base_stem}{suffix}.dae"
                    tasks.append(
                        _AnimTask(
                            base_path=base,
                            anim_path=skill.animation.resolve(),
                            base_stem=base_stem,
                            key=key,
                            names=names,
                            rig=rig,
                            civ=civ,
                            output=paths[dedupe_key],
                            anim_speed=skill.anim_speed,
                            loop=skill.type in {"stop", "move"},
                            use_base_weights=anim_model is base_model,
                            unit_name=unit.name,
                            anim_name=skill.animation.name,
                            group_index=gi,
                        )
                    )
    for task, result in _run_tasks(_convert_animation_task, tasks):
        stats.warnings.extend(result.warnings)
        if not result.ok:
            continue
        path = paths[(task.base_stem, task.group_index, task.key)]
        stats.animation_count += 1
        stats.generated.append(_rel(mod_dir, path))
        if task.names:
            if task.group_index == 0:
                stats.animations.setdefault(task.base_path, {})[task.key] = path
            else:
                stats.prop_animations.setdefault(task.base_path, {}).setdefault(
                    task.group_index, {}
                )[task.key] = path


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _collect_models(faction: Faction, resources_dir: Path | None) -> set[Path]:
    """Every G3D a faction can show: unit-dir files, skill animations,
    and resource models (gold/stone nodes render in-game)."""
    models: set[Path] = set()
    for unit in faction.units.values():
        for path in unit.directory.rglob("*.g3d"):
            models.add(path)
        for skill in unit.skills.values():
            if skill.animation is not None and skill.animation.suffix == ".g3d":
                models.add(skill.animation)
    if resources_dir is not None:
        for path in resources_dir.rglob("*.g3d"):
            models.add(path)
    return models


def _is_animated(model: g3dlib.G3DModel) -> bool:
    """True when any mesh carries morph frames (animatable)."""
    return any(m.frame_count > 1 for m in model.meshes)


def _unit_base_models(faction: Faction, model_cache: dict[Path, g3dlib.G3DModel]) -> set[Path]:
    """Base (actor mesh) models: first skill animation per unit that parses."""
    bases: set[Path] = set()
    for unit in faction.units.values():
        base = _first_skill_model(unit, model_cache)
        if base is not None:
            bases.add(base)
    return bases


def _first_skill_model(unit: UnitDef, model_cache: dict[Path, g3dlib.G3DModel]) -> Path | None:
    """First skill whose animation resolves to a converted model.

    Mirrors the actor generator's unit-model selection so the rig is built
    for exactly the model the actor references.
    """
    for skill in unit.skills.values():
        if skill.animation is None:
            continue
        key = skill.animation.resolve()
        for g3d in model_cache:
            if g3d.resolve() == key:
                return g3d
    return None


def _resolve_texture(model_dir: Path, name: str) -> Path | None:
    """Locate a mesh texture: as-is, doubled-ext stripped, then parent dir.

    Packs sometimes reference a stale extension (``.tga``) while the shipped
    file is another supported format; retry by stem as a last resort.
    """
    raw = model_dir / name
    stripped = model_dir / (texture_stem(name) + Path(name).suffix)
    candidates = [raw, stripped, model_dir.parent / name, model_dir.parent / stripped]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for directory in (model_dir, model_dir.parent):
        for suffix in _TEXTURE_EXTS:
            candidate = directory / f"{texture_stem(name)}{suffix}"
            if candidate.is_file():
                return candidate
    return None


def _file_sha256(path: Path) -> str:
    """Content digest of ``path`` (dedup key for texture conversion)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_path(directory: Path, stem: str, suffix: str, written: set[Path]) -> Path:
    """``directory/{stem}{suffix}``, suffixing ``_1``, ``_2`` ... on collision.

    The ``_N`` form (not ``-N``): 0 A.D.'s VFS/archive builder fails on
    hyphenated filenames, and the plan's naming rules are underscore-only.
    """
    candidate = directory / f"{stem}{suffix}"
    counter = 0
    while candidate in written or candidate.exists():
        counter += 1
        candidate = directory / f"{stem}_{counter}{suffix}"
    return candidate


def _mesh_names(stem: str, n_meshes: int) -> set[str]:
    """Output filenames (``.dae``) a model with ``stem`` will produce."""
    if n_meshes <= 1:
        return {f"{stem}.dae"}
    return {f"{stem}_{i + 1:02d}.dae" for i in range(n_meshes)}


def _convert_portraits(
    faction: Faction,
    portraits_dir: Path,
    converter: TextureConverter,
    mod_dir: Path,
    stats: MediaConversionStats,
    written: set[Path],
) -> None:
    # Only the main portrait converts: MegaGlest's cancel image has no 0 A.D.
    # analog (the engine renders its own cancel UI), and the shared cancel
    # icon is byte-identical for every unit, so emitting *_cancel.png files
    # would duplicate the same texture N times for zero consumers.
    for unit in faction.units.values():
        source = unit.image
        if source is None:
            continue
        if not source.is_file():
            stats.warnings.append(f"{unit.name}: portrait {source} missing")
            continue
        output = _unique_path(portraits_dir, sanitize_mod_name(unit.name), ".png", written)
        try:
            converter.convert_to_png(source, output)
        except ConversionError as exc:
            stats.warnings.append(str(exc))
            continue
        written.add(output)
        stats.textures += 1
        stats.generated.append(_rel(mod_dir, output))


def _convert_tech_portraits(
    faction: Faction,
    portraits_dir: Path,
    converter: TextureConverter,
    mod_dir: Path,
    stats: MediaConversionStats,
) -> None:
    """Convert upgrade images to technology portrait icons.

    Names are deterministic (``{upgrade}.png`` in the flat technologies
    portraits dir) so the tech generator can reference icons without a
    registry; upgrade names are unique per faction.
    """
    for upgrade in faction.upgrades.values():
        source = upgrade.image
        if source is None:
            continue
        if not source.is_file():
            stats.warnings.append(f"{upgrade.name}: portrait {source} missing")
            continue
        output = portraits_dir / f"{sanitize_mod_name(upgrade.name)}.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            converter.convert_to_png(source, output)
        except ConversionError as exc:
            stats.warnings.append(str(exc))
            continue
        stats.textures += 1
        stats.generated.append(_rel(mod_dir, output))


def _convert_sounds(
    faction: Faction,
    sfx_dir: Path,
    groups_dir: Path,
    converter: AudioConverter,
    mod_dir: Path,
    stats: MediaConversionStats,
    written: set[Path],
) -> None:
    civ = sanitize_mod_name(faction.name)
    converted: dict[Path, Path] = {}
    for group_name, sources in _sound_sets(faction).items():
        outputs: list[Path] = []
        for source in sources:
            if not source.is_file():
                stats.warnings.append(f"{group_name}: sound {source.name} missing")
                continue
            output = converted.get(source)
            if output is None:
                output = _unique_path(sfx_dir, source.stem, ".ogg", written)
                try:
                    converter.convert_to_ogg(source, output)
                except ConversionError as exc:
                    stats.warnings.append(str(exc))
                    continue
                converted[source] = output
                written.add(output)
                stats.sounds += 1
                stats.generated.append(_rel(mod_dir, output))
            outputs.append(output)
        if not outputs:
            continue
        group_path = groups_dir / f"{sanitize_mod_name(group_name)}.xml"
        converter.write_sound_group(
            group_path,
            f"audio/sfx/{civ}/",
            [output.name for output in outputs],
        )
        stats.generated.append(_rel(mod_dir, group_path))


def _sound_sets(faction: Faction) -> dict[str, list[Path]]:
    """Group names -> sound sources: selection, command, and per-skill sets."""
    sets: dict[str, list[Path]] = {}
    for unit in faction.units.values():
        if unit.selection_sounds:
            sets[f"{unit.name}_select"] = list(unit.selection_sounds)
        if unit.command_sounds:
            sets[f"{unit.name}_command"] = list(unit.command_sounds)
        for skill in unit.skills.values():
            sounds = list(skill.sounds)
            if skill.attack is not None:
                sounds.extend(skill.attack.sounds)
            if sounds:
                sets[f"{unit.name}_{skill.type}"] = sounds
    return sets


def _convert_music(
    faction: Faction,
    music_dir: Path,
    converter: AudioConverter,
    mod_dir: Path,
    stats: MediaConversionStats,
    written: set[Path],
) -> None:
    if faction.music is None:
        return
    source = faction.music
    if not source.is_file():
        stats.warnings.append(f"music {source} missing")
        return
    output = _unique_path(music_dir, source.stem, ".ogg", written)
    try:
        converter.convert_to_ogg(source, output)
    except ConversionError as exc:
        stats.warnings.append(str(exc))
        return
    written.add(output)
    stats.music += 1
    stats.music_files.append(output.name)
    stats.generated.append(_rel(mod_dir, output))


def _rel(mod_dir: Path, path: Path) -> str:
    return str(path.relative_to(mod_dir))
