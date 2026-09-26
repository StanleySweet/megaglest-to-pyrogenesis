"""Does the written DAE actually reproduce MegaGlest's animation?

Every other check in this suite is self-consistency: the same code reads the
source and writes the output, so agreeing with itself proves only that nothing
crashed. This module closes the loop against the *source frames*.

Each test skins the written COLLADA the way the 0 A.D. importer does --
``sum_j w_j * (M_j * bind_j^-1) * bind_shape * p`` -- and compares the result
with the vertex positions MegaGlest authored. The reconstruction is read back
with pycollada rather than the project's own XML layer, so a DAE that only our
writer can read fails here.

Errors are quoted as a fraction of the model's bounding-box diagonal, so the
bounds mean the same thing for a 200-vertex prop and a 20k-vertex unit.
"""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pytest
from collada import Collada

from megaglest_to_0ad.converters.mesh_converter import MeshConverter, read_g3d
from megaglest_to_0ad.converters.rig import _frame_stack, build_rig

G3D = Path(__file__).resolve().parent / "fixtures/g3d"
TREANT = G3D / "treant_idle.g3d"
NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}

# A rest pose that is not bit-exact means the model visibly shifts the moment
# the animation starts, so this is held to machine precision rather than to a
# fraction of the model.
_REST_TOL = 1e-9
# The inline path clusters on the animation it is given, so its fits reproduce
# it exactly; the foreign path inherits the nearest-neighbour transfer floor.
_INLINE_MAX = 1e-6
_FOREIGN_MAX = 1e-3


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _shifted_copy(path: Path, seed: int = 5, scale: float = 0.01) -> object:
    """A second parse of the same model, nudged off the original rest pose.

    Every frame gets the same per-vertex offset, so the model is displaced but
    its animation is untouched. That is the realistic foreign case: a
    neighbouring mesh carrying the same motion, which is what the displacement
    transfer exists to handle.

    ``read_g3d`` hands back a shared cached model, so the copy is deep: writing
    to the parse directly would corrupt the model every other test reads.
    """
    model = copy.deepcopy(read_g3d(path))
    rng = np.random.default_rng(seed)
    for mesh in model.meshes:
        count = mesh.vertex_count
        frames = np.asarray(mesh.vertices, dtype=np.float64).reshape(-1, count, 3)
        frames += rng.normal(0.0, scale, (1, count, 3))
        mesh.vertices = frames.reshape(-1).tolist()
    return model


def _frame_count(model: object) -> int:
    return min(m.frame_count for m in model.meshes)


# ---------------------------------------------------------------------------
# reading the DAE back
# ---------------------------------------------------------------------------


def _write(base: object, anim: object, path: Path, bones: int = 7) -> None:
    rig = build_rig(base, [list(range(len(base.meshes)))], bones, "root")
    MeshConverter().write_animation_dae(
        base,
        anim,
        rig,
        path,
        "demo",
        anim_speed=100.0,
        loop=True,
        use_base_weights=anim is base,
    )


def _skinned(dae_path: Path, frame: int) -> tuple[np.ndarray, np.ndarray]:
    """Positions after skinning ``frame``, plus the bind-pose positions."""
    dae = Collada(str(dae_path))
    positions = np.asarray(dae.geometries[0].primitives[0].vertex, dtype=float)
    skin = dae.controllers[0]
    joints = list(skin.weight_joints)
    weights = np.asarray(skin.weights.data, dtype=float).ravel()
    bind = {n: np.asarray(m, dtype=float) for n, m in skin.joint_matrices.items()}
    bind_shape = np.asarray(skin.bind_shape_matrix, dtype=float)
    inv_bind = {n: np.linalg.inv(m) for n, m in bind.items()}

    # One 4x4 keyframe matrix per bone, in the order the channels were written.
    matrices: dict[str, np.ndarray] = {}
    for anim in dae.animations:
        channel = anim.xmlnode.find("c:channel", NS)
        sampler = anim.xmlnode.find("c:sampler", NS)
        if channel is None or sampler is None:
            continue
        refs = {i.get("semantic"): i.get("source") for i in sampler.findall("c:input", NS)}
        out = np.asarray(anim.sourceById[refs["OUTPUT"].lstrip("#")].data, dtype=float)
        matrices[channel.get("target").split("/")[0]] = out.reshape(-1, 4, 4)[frame]

    out = np.empty_like(positions)
    for i, (pos, (joint_ids, weight_ids)) in enumerate(
        zip(positions, zip(skin.joint_index, skin.weight_index), strict=True)
    ):
        acc = np.zeros(4)
        for j, w in zip(joint_ids, weight_ids, strict=True):
            name = joints[int(j)]
            if name not in matrices or name not in inv_bind:
                continue
            m = matrices[name] @ inv_bind[name]
            acc += weights[int(w)] * (m @ (bind_shape @ np.append(pos, 1.0)))
        out[i] = acc[:3] / acc[3] if abs(acc[3]) > 1e-12 else pos
    return out, positions


def _diagonal(points: np.ndarray) -> float:
    return float(np.linalg.norm(np.ptp(points, axis=0)))


# ---------------------------------------------------------------------------
# the specification the output must meet
# ---------------------------------------------------------------------------


def _transferred(base: object, anim: object, frames: int) -> np.ndarray:
    """Where each base vertex must end up, per frame.

    The nearest-neighbour rule restated independently of ``rig.py``: a base
    vertex keeps its own rest position and borrows the displacement of the
    closest vertex in the animated model. This is the *specification* the
    foreign path is measured against, not a reimplementation of it.
    """
    rest = _frame_stack(base.meshes, 1)[0]
    anim_stack = _frame_stack(anim.meshes, frames)
    d2 = ((rest[:, None, :] - anim_stack[0][None, :, :]) ** 2).sum(-1)
    nearest = d2.argmin(-1)
    return rest[None, :, :] + anim_stack[:, nearest, :] - anim_stack[0][None, nearest, :]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_rest_pose_is_the_base_mesh(tmp_path: Path) -> None:
    """Frame 0 must be the authored mesh, not a skinned approximation."""
    base = read_g3d(TREANT)
    want = _frame_stack(base.meshes, 1)[0]

    for label, anim in (("inline", base), ("foreign", _shifted_copy(TREANT))):
        dae = tmp_path / f"{label}.dae"
        _write(base, anim, dae)
        got, _ = _skinned(dae, 0)
        assert np.abs(got - want).max() < _REST_TOL, label


def test_inline_animation_reproduces_the_source_frames(tmp_path: Path) -> None:
    """The common case: animation baked into the model that owns the mesh."""
    base = read_g3d(TREANT)
    frames = _frame_count(base)
    dae = tmp_path / "inline.dae"
    _write(base, base, dae)

    want = _frame_stack(base.meshes, frames)
    _, positions = _skinned(dae, 0)
    diag = _diagonal(positions)

    for f in range(1, frames):
        got, _ = _skinned(dae, f)
        err = np.linalg.norm(got - want[f], axis=-1).max()
        assert err < _INLINE_MAX * diag, f"frame {f}: {err / diag:.2e} of diagonal"


def test_foreign_animation_reproduces_the_transferred_motion(tmp_path: Path) -> None:
    """A neighbouring mesh's animation must still be reproduced.

    Weighting these vertices by rest proximity instead of by how they move
    leaves the worst vertex roughly a full frame displacement away from where
    the source put it -- and makes neighbouring vertices disagree about which
    bone they follow, which reads as flicker along the seam.
    """
    base = read_g3d(TREANT)
    anim = _shifted_copy(TREANT)
    frames = _frame_count(anim)
    dae = tmp_path / "foreign.dae"
    _write(base, anim, dae)

    want = _transferred(base, anim, frames)
    _, positions = _skinned(dae, 0)
    diag = _diagonal(positions)

    for f in range(1, frames):
        got, _ = _skinned(dae, f)
        err = np.linalg.norm(got - want[f], axis=-1).max()
        assert err < _FOREIGN_MAX * diag, f"frame {f}: {err / diag:.2e} of diagonal"


@pytest.mark.parametrize("bones", [4, 7, 16])
def test_foreign_accuracy_holds_across_usable_bone_counts(tmp_path: Path, bones: int) -> None:
    """A few bones are enough, because the accuracy comes from the weighting.

    Not "any bone count": see ``test_too_few_bones_cannot_carry_the_motion``
    and the degenerate-cluster issue for the ends of the range.
    """
    base = read_g3d(TREANT)
    anim = _shifted_copy(TREANT)
    frames = _frame_count(anim)
    dae = tmp_path / f"b{bones}.dae"
    _write(base, anim, dae, bones=bones)

    want = _transferred(base, anim, frames)
    _, positions = _skinned(dae, 0)
    diag = _diagonal(positions)

    worst = max(
        float(np.linalg.norm(_skinned(dae, f)[0] - want[f], axis=-1).max())
        for f in range(1, frames)
    )
    assert worst < _FOREIGN_MAX * diag, f"{bones} bones: {worst / diag:.2e} of diagonal"


def test_default_bone_count_for_this_model_is_accurate(tmp_path: Path) -> None:
    """Guard the regime the converter actually ships.

    ``media_conversion`` asks for ``max(4, min(rig_bones, verts // 40 + 2))``
    bones, which is 18 for the treant. Accuracy has to hold at the number the
    pipeline picks, not just at round numbers.
    """
    base = read_g3d(TREANT)
    vertices = sum(m.vertex_count for m in base.meshes)
    bones = max(4, min(32, vertices // 40 + 2))
    assert bones == 18, "fixture changed; keep this guard in step with the formula"

    anim = _shifted_copy(TREANT)
    frames = _frame_count(anim)
    dae = tmp_path / "default.dae"
    _write(base, anim, dae, bones=bones)

    want = _transferred(base, anim, frames)
    _, positions = _skinned(dae, 0)
    diag = _diagonal(positions)
    worst = max(
        float(np.linalg.norm(_skinned(dae, f)[0] - want[f], axis=-1).max())
        for f in range(1, frames)
    )
    assert worst < _FOREIGN_MAX * diag, f"{worst / diag:.2e} of diagonal"


def test_too_few_bones_cannot_carry_the_motion(tmp_path: Path) -> None:
    """A single bone is a whole-model rigid fit, and this model is not rigid.

    This is the floor of what any weighting can achieve: with one cluster every
    vertex shares one transform, so a model that bends cannot be reproduced.
    It is a capacity limit, not a bug -- but it must not be mistaken for one
    when the foreign-path bound is loosened.
    """
    base = read_g3d(TREANT)
    anim = _shifted_copy(TREANT)
    frames = _frame_count(anim)
    dae = tmp_path / "one.dae"
    _write(base, anim, dae, bones=2)

    want = _transferred(base, anim, frames)
    _, positions = _skinned(dae, 0)
    diag = _diagonal(positions)
    worst = max(
        float(np.linalg.norm(_skinned(dae, f)[0] - want[f], axis=-1).max())
        for f in range(1, frames)
    )
    # A real bone's worth of slack, yet still far better than the old
    # rest-proximity weighting managed with plenty of bones.
    assert _FOREIGN_MAX * diag < worst < 0.05 * diag
