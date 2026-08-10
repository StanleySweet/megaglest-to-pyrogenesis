#!/usr/bin/env python3
"""Verify converted animation DAEs reproduce the source morph frames.

The converter re-encodes MegaGlest morph-frame animations as synthetic
skeletal animations: k-means displacement clusters become joints, and each
frame becomes per-bone rigid (Kabsch) transforms on the base model's rest
geometry (see ``src/megaglest_to_0ad/converters/rig.py``). The rigid
approximation is lossy — this tool quantifies that loss frame by frame and
flags distortions that would be visible in-game.

Per animation DAE ``art/animation/{civ}/{base_stem}_{key}.dae``:

1. **Plan**: the source pairing is re-derived the same way the converter
   decides it — the first unit (sorted directory order) whose base model
   stem matches ``base_stem``, and the first skill of that unit that maps
   to the engine animation name ``key`` — so ``(base g3d, anim g3d)`` is
   exactly what the converter consumed.
2. **Parse**: pycollada loads the DAE (an independent parser — a DAE the
   writer accepts but pycollada rejects is a finding); geometry, skin
   weights and per-joint keyframe transforms are extracted.
3. **Skin**: every source frame is reconstructed as the engine would:
   ``p' = sum_j w_j * M_j * p`` (bind pose and bind shape are identity).
4. **Compare**: reconstructed positions vs the anim model's own morph
   frames for the base's group-0 meshes, vertex by vertex. Frame 0
   (identity keyframes) must land on the base rest pose; a mismatch there
   means the anim model's rest pose differs from the base's — the animation
   would visibly shift the unit. Errors on later frames are the rigid-fit
   approximation loss.

Verdicts per DAE:

- ``OK``: all frames within tolerance.
- ``WARN``: distortion beyond tolerance (rigid-fit loss), rest offset, or
  keyframe timing/size mismatch.
- ``FAIL``: structural problems — parse errors, joint/vertex count
  mismatches, missing source files, unusable frames.

Tolerances are relative to the base geometry's bounding-box diagonal
(``--mean-tol`` and ``--max-tol`` are fractions of it).

Usage::

    python tools/verify_animations.py --megaglest-data input/elves_A10 \
        --mod-dir output/elves_a10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo-local bootstrap so the tool runs from any CWD (see rig.py).
_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "vendor" / "g3d"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402  (third-party; pycollada dependency)
from collada import Collada  # noqa: E402

from megaglest_to_0ad.converters.mesh_converter import (  # noqa: E402
    read_g3d,
    texture_groups,
)
from megaglest_to_0ad.core.media_conversion import (  # noqa: E402
    _UNWIRED_ANIM_TYPES,
    _collect_models,
    _first_skill_model,
    _is_animated,
    _resolve_texture,
    _unit_base_models,
    engine_animation_names,
)
from megaglest_to_0ad.megaglest.parser import discover_pack  # noqa: E402

_NS = "{http://www.collada.org/2005/11/COLLADASchema}"


# ---------------------------------------------------------------------------
# plan: replicate the converter's (base, key) -> (base g3d, anim g3d) choice
# ---------------------------------------------------------------------------


def _group0_indices(base_model, base_path: Path) -> list[int]:
    """Mesh indices of the base model's texture group 0 (the DAE's geometry)."""
    resolve = lambda name: _resolve_texture(base_path.parent, name)  # noqa: E731
    return texture_groups(base_model, resolve)[0]


def _plan_animations(
    faction, model_cache: dict[Path, object]
) -> dict[tuple[str, str], dict[str, object]]:
    """Output animation name -> conversion inputs, exactly as the converter
    decides them (deterministic first-wins over sorted units and skills).

    Returns ``{(base_stem, key): {...}}`` for every DAE the converter
    should have emitted.
    """
    base_g3ds = _unit_base_models(faction, model_cache)
    plan: dict[tuple[str, str], dict[str, object]] = {}
    for unit in faction.units.values():
        base = _first_skill_model(unit, model_cache)
        if base is None or base not in base_g3ds:
            continue
        model = model_cache.get(base)
        if model is None or not _is_animated(model):
            continue
        base_stem = base.stem
        group0 = _group0_indices(model, base)
        for skill in unit.skills.values():
            if skill.animation is None:
                continue
            anim_path = skill.animation
            anim_model = model_cache.get(anim_path)
            if anim_model is None:
                continue
            if len(anim_model.meshes) <= max(group0):
                continue
            frame_count = min(anim_model.meshes[i].frame_count for i in group0)
            if frame_count < 2:
                continue
            names = engine_animation_names(skill)
            if names:
                keys = list(names)
            elif skill.type in _UNWIRED_ANIM_TYPES:
                keys = [skill.type]
            else:
                continue
            for key in keys:
                plan.setdefault(
                    (base_stem, key),
                    {
                        "base": base,
                        "anim": anim_path,
                        "frame_count": frame_count,
                        "anim_speed": skill.anim_speed,
                        "loop": skill.type in {"stop", "move"},
                    },
                )
    return plan


# ---------------------------------------------------------------------------
# DAE extraction (pycollada + raw sampler nodes)
# ---------------------------------------------------------------------------


def _dae_data(dae_path: Path) -> dict[str, object]:
    """Positions, skin weights and per-bone keyframes from one DAE."""
    dae = Collada(str(dae_path))
    prim = dae.geometries[0].primitives[0]
    positions = np.asarray(prim.vertex, dtype=float)  # N x 3 (bind pose)

    skin = dae.controllers[0]
    joint_names = list(skin.weight_joints)  # joint order of the <v> indices
    weights = np.asarray(skin.weights.data, dtype=float).ravel()
    per_vertex: list[list[tuple[int, float]]] = []
    for joints, weight_idx in zip(skin.joint_index, skin.weight_index):
        per_vertex.append(
            [(int(j), float(weights[int(w)])) for j, w in zip(joints, weight_idx)]
        )

    keyframes: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for anim in dae.animations:
        channel = anim.xmlnode.find(f"{_NS}channel")
        sampler = anim.xmlnode.find(f"{_NS}sampler")
        if channel is None or sampler is None:
            continue
        target = channel.get("target", "")
        bone = target.split("/")[0]
        refs = {
            inp.get("semantic"): inp.get("source")
            for inp in sampler.findall(f"{_NS}input")
        }
        times = np.asarray(anim.sourceById[refs["INPUT"].lstrip("#")].data, dtype=float)
        matrices = np.asarray(
            anim.sourceById[refs["OUTPUT"].lstrip("#")].data, dtype=float
        )
        matrices = matrices.reshape(-1, 4, 4)
        keyframes[bone] = (times, matrices)
    return {
        "positions": positions,
        "joint_names": joint_names,
        "per_vertex": per_vertex,
        "keyframes": keyframes,
        "bind": {name: np.asarray(m, dtype=float) for name, m in skin.joint_matrices.items()},
        "bind_shape": np.asarray(skin.bind_shape_matrix, dtype=float),
    }


def _skin_positions(
    positions: np.ndarray,
    joint_names: list[str],
    per_vertex: list[list[tuple[int, float]]],
    matrices: dict[str, np.ndarray],
    bind: dict[str, np.ndarray],
    bind_shape: np.ndarray,
) -> np.ndarray:
    """Engine skinning: sum_j w_j * (M_j * bind_j^-1) * bind_shape * p."""
    out = np.zeros_like(positions)
    for i, influences in enumerate(per_vertex):
        point = bind_shape @ np.append(positions[i], 1.0)
        acc = np.zeros(4)
        for j, w in influences:
            name = joint_names[j]
            if name not in matrices:
                continue
            m = matrices[name] @ np.linalg.inv(bind[name])
            acc += w * (m @ point)
        out[i] = acc[:3] / acc[3] if abs(acc[3]) > 1e-12 else positions[i]
    return out


# ---------------------------------------------------------------------------
# frame comparison
# ---------------------------------------------------------------------------


def _expected_frames(
    anim_model, group0: list[int], frame_count: int
) -> list[np.ndarray]:
    meshes = [anim_model.meshes[i] for i in group0]
    out = []
    for f in range(frame_count):
        parts = []
        for mesh in meshes:
            stride = mesh.vertex_count * 3
            parts.append(
                np.asarray(
                    mesh.vertices[f * stride : (f + 1) * stride], dtype=float
                ).reshape(-1, 3)
            )
        out.append(np.concatenate(parts))
    return out


def _distances(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.linalg.norm(a - b, axis=1)


def verify_dae(
    dae_path: Path,
    base_path: Path,
    anim_path: Path,
    frame_count: int,
    anim_speed: float,
    loop: bool,
    mean_tol: float,
    max_tol: float,
) -> dict[str, object]:
    """Verify one animation DAE against its source models."""
    report: dict[str, object] = {
        "dae": str(dae_path.relative_to(dae_path.parents[2])),
        "base": base_path.name,
        "anim": anim_path.name,
    }
    base_model = read_g3d(base_path)
    anim_model = read_g3d(anim_path)
    group0 = _group0_indices(base_model, base_path)

    data = _dae_data(dae_path)
    positions = data["positions"]
    expected = _expected_frames(anim_model, group0, frame_count)

    if len(expected[0]) != len(positions):
        report["verdict"] = "FAIL"
        report["error"] = (
            f"vertex count mismatch: DAE {len(positions)} vs source "
            f"{len(expected[0])}"
        )
        return report

    keyframes = data["keyframes"]
    bone_names = [n for n in data["joint_names"] if n in keyframes]
    if len(bone_names) != len(data["joint_names"]):
        missing = set(data["joint_names"]) - set(keyframes)
        report["verdict"] = "FAIL"
        report["error"] = f"joints without animation channels: {sorted(missing)}"
        return report

    # keyframe times must encode anim-speed (100/s seconds per cycle)
    duration = 100.0 / anim_speed if anim_speed > 0 else 1.0
    expected_keys = frame_count + (1 if loop else 0)
    times = keyframes[bone_names[0]][0].ravel()
    if len(times) != expected_keys:
        report["verdict"] = "FAIL"
        report["error"] = (
            f"keyframe count {len(times)} != source frames {frame_count} "
            f"(+{1 if loop else 0} loop close)"
        )
        return report
    wanted = [f * duration / max(frame_count - 1, 1) for f in range(frame_count)]
    if loop:
        wanted.append(duration)
    if not np.allclose(times, wanted, atol=1e-4):
        report["verdict"] = "FAIL"
        report["error"] = (
            f"keyframe times {times.tolist()} != anim-speed times {wanted}"
        )
        return report

    bind = data["bind"]
    bind_shape = data["bind_shape"]
    per_vertex = data["per_vertex"]

    diag = float(np.linalg.norm(np.ptp(positions, axis=0)))
    frame_errors: list[float] = []
    max_errors: list[float] = []
    for f in range(frame_count):
        matrices = {name: keyframes[name][1][f] for name in bone_names}
        predicted = _skin_positions(
            positions, data["joint_names"], per_vertex, matrices, bind, bind_shape
        )
        d = _distances(predicted, expected[f])
        frame_errors.append(float(np.mean(d)))
        max_errors.append(float(np.max(d)))

    issues: list[str] = []
    rest_offset = frame_errors[0]
    if rest_offset > mean_tol * diag:
        issues.append(
            f"rest offset {rest_offset:.4f} m (anim rest != base rest); "
            "the unit will visibly shift at frame 0"
        )
    for f, (mean_e, max_e) in enumerate(zip(frame_errors, max_errors)):
        if mean_e > mean_tol * diag or max_e > max_tol * diag:
            issues.append(
                f"frame {f}: mean {mean_e:.4f} m, max {max_e:.4f} m "
                f"(tolerances {mean_tol * diag:.4f} / {max_tol * diag:.4f} m)"
            )
    if loop:
        last = keyframes[bone_names[0]][1][-1]
        if not np.allclose(last, np.eye(4), atol=1e-6):
            issues.append("loop-closing keyframe is not identity (rest)")

    report.update(
        {
            "frames": frame_count,
            "vertices": len(positions),
            "diag": round(diag, 4),
            "rest_offset": round(rest_offset, 6),
            "mean_error": round(float(max(frame_errors)), 6),
            "max_error": round(float(max(max_errors)), 6),
            "worst_frame": int(np.argmax(frame_errors)),
        }
    )
    report["verdict"] = "WARN" if issues else "OK"
    report["issues"] = issues
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--megaglest-data", required=True, type=Path)
    parser.add_argument("--mod-dir", required=True, type=Path)
    parser.add_argument(
        "--mean-tol", type=float, default=0.01, help="mean error, fraction of bbox diagonal"
    )
    parser.add_argument(
        "--max-tol", type=float, default=0.04, help="max error, fraction of bbox diagonal"
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    args = parser.parse_args(argv)

    pack = discover_pack(args.megaglest_data)
    available = sorted(
        p.name for p in pack.factions_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )
    if len(available) != 1:
        parser.error(f"expected exactly one faction, found {available}")
    from megaglest_to_0ad.megaglest.civ_loader import load_faction

    faction = load_faction(pack, pack.factions_dir / available[0])
    model_cache: dict[Path, object] = {}
    missing_sources: list[str] = []
    for g3d_path in sorted(_collect_models(faction, None)):
        try:
            model_cache[g3d_path] = read_g3d(g3d_path)
        except Exception as exc:
            missing_sources.append(f"{g3d_path.name}: {exc}")

    plan = _plan_animations(faction, model_cache)
    anim_root = args.mod_dir / "art/animation"
    subdirs = [p for p in anim_root.iterdir() if p.is_dir()]
    civ = subdirs[0].name if subdirs else ""
    reports: list[dict[str, object]] = []
    for (base_stem, key), info in sorted(plan.items()):
        dae_path = args.mod_dir / "art/animation" / civ / f"{base_stem}_{key}.dae"
        if not dae_path.is_file():
            reports.append(
                {
                    "dae": f"{base_stem}_{key}.dae",
                    "verdict": "FAIL",
                    "error": "plan says emit, file missing",
                }
            )
            continue
        try:
            reports.append(
                verify_dae(
                    dae_path,
                    info["base"],
                    info["anim"],
                    info["frame_count"],
                    info["anim_speed"],
                    info["loop"],
                    args.mean_tol,
                    args.max_tol,
                )
            )
        except Exception as exc:
            reports.append(
                {
                    "dae": dae_path.name,
                    "verdict": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    counts = {"OK": 0, "WARN": 0, "FAIL": 0}
    for report in reports:
        counts[report["verdict"]] += 1

    if args.json:
        print(json.dumps({"counts": counts, "reports": reports}, indent=2))
        return 0

    for report in reports:
        verdict = report["verdict"]
        if verdict == "OK":
            continue
        print(
            f"[{verdict}] {report['dae']} "
            f"({report.get('base', '?')} <- {report.get('anim', '?')})"
        )
        for issue in report.get("issues", []):
            print(f"    {issue}")
        if report.get("error"):
            print(f"    {report['error']}")
    print(
        f"{len(reports)} animation(s): {counts['OK']} OK, {counts['WARN']} WARN, "
        f"{counts['FAIL']} FAIL"
    )
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
