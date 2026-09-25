#!/usr/bin/env python3
"""Turn MegaGlest morph animations into skeletal 0 A.D. animations.

G3D files store morph frames (per-frame vertex positions) and never bones:
the format has no joints, weights or hierarchy. 0 A.D.'s COLLADA importer
only accepts skin controllers (PMDConvert/PSAConvert both require a skin),
so the rig is synthesized:

1. Vertices are clustered by their displacement trajectories (k-means over
   rest position + per-frame deltas) into quasi-rigid parts.
2. Each part becomes a joint; skin weights are a soft top-4 assignment so
   vertices near part boundaries blend between parts.
3. Every animation frame is re-encoded as rigid joint transforms: a
   scale-free Kabsch fit (weighted 3x3 SVD) of each part's rest pose onto
   its pose in that frame.

This script is the standalone face of the converter's rig pipeline
(``megaglest_to_0ad.converters.rig`` + ``mesh_converter``): it writes one
skinned mesh DAE plus one skeletal animation DAE per input animation, all
sharing one synthesized joint contract. The engine imports the DAEs as PMD
(skinned mesh) + PSA (bone animation) — no morph support involved.

Animation filenames follow the MegaGlest convention ``{unit}_{skill}.g3d``;
the skill part (text after the first underscore) names the output DAE
(``treant_idle.g3d`` -> ``treant_idle_idle.dae``).

Usage::

    python tools/morph_to_skeletal.py treant_idle.g3d treant_attack_ranged.g3d \\
        --output mymod --rig-bones 6

Output (drop ``mymod`` straight into 0 A.D. as a mod):

    mymod/art/meshes/treant_idle/treant_idle.dae        (skinned mesh)
    mymod/art/animation/treant_idle/treant_idle_idle.dae
    mymod/art/animation/treant_idle/treant_idle_attack_ranged.dae

Actor wiring is left to you: reference the mesh DAE with ``<mesh>`` and each
animation DAE with ``<animation file=... name=...>`` (paths relative to
``art/`` / ``art/animation/``).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from megaglest_to_0ad.converters.mesh_converter import (  # noqa: E402
    MeshConverter,
    read_g3d,
    texture_groups,
)
from megaglest_to_0ad.converters.rig import Rig, build_rig  # noqa: E402

# Same clamp as the pack converter: a bone per ~40 vertices (beyond the root).
_VERTICES_PER_BONE = 40
# MegaGlest's canonical default animation speed (frames per 100 s cycle).
_DEFAULT_ANIM_SPEED = 4.0
_DEFAULT_RIG_BONES = 6


def _anim_key(stem: str) -> str:
    """Skill part of a ``{unit}_{skill}.g3d`` stem; bare stems pass through."""
    return stem.split("_", 1)[1] if "_" in stem else stem


def _sanitize_id(value: str) -> str:
    """Identifiers inside DAEs (civ/root names) must be XML-safe."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def convert(
    base: Path,
    anims: list[Path],
    output: Path,
    civ: str,
    rig_bones: int,
    anim_speed: float,
    loop: bool,
) -> int:
    """Convert ``base``'s mesh + every animation source into skeletal DAEs.

    Returns 0 on success, 1 when a required conversion failed (skipped
    animations only print warnings). Deterministic: same inputs, same bytes
    (fixed k-means seed, closed-form SVD — see ``rig``).
    """
    base_model = read_g3d(base)
    if not any(m.frame_count > 1 for m in base_model.meshes):
        print(f"error: {base} carries no morph frames; nothing to rig")
        return 1

    # Partition meshes by resolved diffuse texture: one 0 A.D. PMD has a
    # single material, so each texture group is its own skinned DAE.
    groups = texture_groups(
        base_model, lambda name: (base.parent / name) if (base.parent / name).exists() else None
    )
    group_vertices = sum(base_model.meshes[i].vertex_count for i in groups[0])
    bone_count = max(2, min(rig_bones, group_vertices // _VERTICES_PER_BONE))
    root_name = f"{_sanitize_id(civ)}_{base.stem}_root"
    rig: Rig = build_rig(base_model, groups, bone_count, root_name)
    meshes_dir = output / "art" / "meshes" / civ
    anim_dir = output / "art" / "animation" / civ
    anim_dir.mkdir(parents=True, exist_ok=True)

    converter = MeshConverter()
    converted = converter.convert_g3d_to_dae(base, meshes_dir, civ, stem=base.stem, rig=rig)
    for warning in converted.warnings:
        print(f"warning: {base.name}: {warning}")
    print(f"mesh: {', '.join(str(p) for p in converted.mesh_daes)} ({bone_count} bones)")

    # One animation DAE per source; the base model's own frames count as one
    # (key from its stem). Same-file sources reuse the exact k-means weights;
    # foreign models get weights by rest-position proximity.
    written: dict[str, Path] = {}
    for anim in anims or [base]:
        anim_model = read_g3d(anim)
        group = rig.groups[0]
        if len(anim_model.meshes) <= max(group.mesh_indices):
            print(f"warning: {anim.name} has fewer meshes than the base model; skipped")
            continue
        frame_count = min(anim_model.meshes[i].frame_count for i in group.mesh_indices)
        if frame_count < 2:
            print(f"warning: {anim.name} has {frame_count} frame(s); skipped")
            continue
        key = _anim_key(anim.stem)
        if key in written:
            print(f"warning: duplicate animation key {key!r}; keeping {written[key].name}")
            continue
        path = anim_dir / f"{base.stem}_{key}.dae"
        converter.write_animation_dae(
            base_model,
            anim_model,
            rig,
            path,
            civ,
            anim_speed,
            loop=loop,
            use_base_weights=anim.resolve() == base.resolve(),
        )
        written[key] = path
        print(f"animation: {path} ({frame_count} frames, key {key!r})")

    print(f"done: {1 + len(written)} DAE(s) written to {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="morph_to_skeletal",
        description=(
            "Convert MegaGlest morph models/animations into skeletal 0 A.D. "
            "DAEs (synthesized rig; see module docstring)."
        ),
    )
    parser.add_argument(
        "base",
        type=Path,
        help="base morph model (its mesh becomes the skinned mesh DAE)",
    )
    parser.add_argument(
        "anims",
        type=Path,
        nargs="*",
        help="morph animation sources reusing the base rig (default: the base model itself)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="mod root to write under (art/meshes/<civ>/ and art/animation/<civ>/)",
    )
    parser.add_argument(
        "--civ",
        default="",
        help="civ/name used in DAE asset ids and output dirs (default: base stem)",
    )
    parser.add_argument(
        "--rig-bones",
        type=int,
        default=_DEFAULT_RIG_BONES,
        help=f"total joints including the root (default: {_DEFAULT_RIG_BONES})",
    )
    parser.add_argument(
        "--anim-speed",
        type=float,
        default=_DEFAULT_ANIM_SPEED,
        help=(
            f"MegaGlest anim-speed: 100/speed seconds per cycle (default: {_DEFAULT_ANIM_SPEED})"
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="close the animation loop (last frame wraps to the first)",
    )
    args = parser.parse_args(argv)

    civ = args.civ or args.base.stem
    return convert(
        args.base,
        list(args.anims),
        args.output,
        civ,
        args.rig_bones,
        args.anim_speed,
        args.loop,
    )


if __name__ == "__main__":
    sys.exit(main())
