"""Synthetic skeleton synthesis for MegaGlest morph models.

G3D files store morph frames (per-frame vertex positions) and never bones:
the format has no joints, weights or hierarchy. 0 A.D.'s importer only
accepts skin controllers (PMDConvert/PSAConvert both ``REQUIRE(skin != NULL)``),
so the rig must be synthesized:

1. Vertices are clustered by their displacement trajectories (k-means over
   rest position + per-frame deltas) into quasi-rigid parts.
2. Each part becomes a joint; skin weights are a soft assignment (top-4
   influences, normalized) so vertices near part boundaries blend.
3. Each animation frame is re-encoded as rigid joint transforms: a
   scale-free Kabsch fit (weighted SVD) of each part's rest pose onto its
   pose in that frame.

The joint contract (names, order, count) is shared by the model's mesh DAE
and every animation DAE so the PMD bone count equals the PSA key count
(``CModel::SetAnimation`` rejects mismatches). Animations for *other*
models (different skill files) reuse the same joints: their vertices are
assigned to the nearest cluster rest centroid, then fitted per frame.

All math is deterministic (fixed seeds, closed-form SVD) so reconverts are
byte-identical.
"""

from __future__ import annotations

import math
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Vendored importer bootstrap (idempotent; see mesh_converter).
_VENDOR_G3D_DIR = Path(__file__).resolve().parents[3] / "vendor" / "g3d"
if str(_VENDOR_G3D_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_G3D_DIR))

import g3dlib  # noqa: E402  (vendored third-party module; see vendor/g3d/)

_MAX_INFLUENCES = 4
_KMEANS_ITERATIONS = 60


@dataclass
class RigGroup:
    """One texture-merged mesh group of the base model: its joint weights.

    ``vertex_weights`` is per *merged* vertex (concatenation of the group's
    meshes in ``mesh_indices`` order): a list of up to 4 ``(bone, weight)``
    pairs, normalized. ``rest_centroids`` is the weighted rest-position
    centroid of each bone's membership, used to assign other models'
    vertices to this rig.
    """

    mesh_indices: list[int]
    vertex_weights: list[list[tuple[int, float]]] = field(default_factory=list)
    rest_centroids: list[list[float]] = field(default_factory=list)
    # per base-model vertex: the bone with the largest weight
    assignments: list[int] = field(default_factory=list)


@dataclass
class Rig:
    """Joint contract shared by a model's mesh + animation DAEs.

    ``bone_names[0]`` is the root joint; its name is the mapped-skeleton
    identifier root in ``art/skeletons/*.xml`` (the engine picks the mapped
    skeleton by walking up from joint 0 until a joint name matches an
    ``<identifier><root>``).
    """

    root_name: str
    bone_names: list[str]
    groups: list[RigGroup]

    @property
    def bone_count(self) -> int:
        return len(self.bone_names)

    @property
    def part_count(self) -> int:
        """Bones after the root (one k-means cluster each)."""
        return self.bone_count - 1


def build_rig(
    model: g3dlib.G3DModel,
    groups: Sequence[Sequence[int]],
    bone_count: int,
    root_name: str,
) -> Rig:
    """Synthesize a joint contract for ``model``.

    ``groups`` partitions ``model.meshes`` (typically by resolved texture;
    the actor references the first group). ``bone_count`` includes the root
    joint: cluster count = ``bone_count - 1``, shared across groups.
    Models with fewer frames than 2 must not be rigged (callers guard).
    """
    names = [root_name] + [f"bone_{i}" for i in range(1, bone_count)]
    rig_groups: list[RigGroup] = []
    for group in groups:
        meshes = [model.meshes[i] for i in group]
        frame_count = min(m.frame_count for m in meshes)
        if frame_count < 2:
            # A static group inside an animated model: no displacement data.
            # Fall back to spatial clustering so the DAE still skins.
            frame_count = 1
        features, rest = _features(meshes, frame_count)
        k = _cluster_count(len(rest), bone_count - 1)
        centroids, _ = _kmeans(features, k)
        weights = [_soft_weights(features[i], centroids) for i in range(len(rest))]
        rest_centroids = [_rest_centroid(rest, weights, b) for b in range(k)]
        rig_groups.append(
            RigGroup(
                mesh_indices=list(group),
                vertex_weights=weights,
                rest_centroids=rest_centroids,
                assignments=[max(w, key=lambda pair: pair[1])[0] for w in weights],
            )
        )
    return Rig(root_name=root_name, bone_names=names, groups=rig_groups)


def fit_group_frames(
    model: g3dlib.G3DModel,
    rig: Rig,
    group_index: int = 0,
    use_base_weights: bool = False,
) -> list[list[tuple[list[float], list[float]]]]:
    """Per-frame rigid transforms for ``rig``'s group ``group_index``.

    Returns ``frames`` with ``frames[f][b]`` = ``(R, t)`` (rotation as 9
    row-major floats, translation as 3 floats) for bone ``b`` in frame ``f``.
    Frame 0 is the rest pose (identity). Vertices of ``model`` are assigned
    to the rig's clusters by rest-position proximity (for the base model,
    ``use_base_weights`` reuses the exact k-means weights). Bone 0 (root)
    carries the whole-model rigid fit; no vertex is weighted to it.
    """
    group = rig.groups[group_index]
    meshes = [model.meshes[i] for i in group.mesh_indices]
    rest = _concat_rest(meshes)
    frame_count = min(m.frame_count for m in meshes)

    # Root: global rigid fit over every vertex (frame 0 = identity).
    roots = [
        _kabsch(rest, _frame_positions(meshes, f), [1.0] * len(rest)) for f in range(frame_count)
    ]
    if use_base_weights:
        # Expand the top-4 (bone, weight) pairs into full per-bone vectors so
        # bone b+1 below reads cluster b's weight.
        weights = []
        for vertex_weights in group.vertex_weights:
            vec = [0.0] * rig.part_count
            for bone, weight in vertex_weights:
                vec[bone] = weight
            weights.append(vec)
    else:
        weights = [_assign_weights(rest[i], group.rest_centroids) for i in range(len(rest))]
    identity = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    frames: list[list[tuple[list[float], list[float]]]] = []
    for f in range(frame_count):
        if f == 0:
            # Frame 0 IS the rest pose: exact identity (no SVD drift on a
            # symmetric fit), so the base pose renders exactly as authored.
            frames.append([(identity, [0.0, 0.0, 0.0])] * rig.bone_count)
            continue
        poses = _frame_positions(meshes, f)
        bone_transforms: list[tuple[list[float], list[float]]] = [
            roots[f],
            *[_kabsch(rest, poses, [w[b] for w in weights]) for b in range(rig.part_count)],
        ]
        frames.append(bone_transforms)
    return frames


def animation_duration(anim_speed: float) -> float:
    """Seconds for one animation cycle (MegaGlest ``anim-speed`` semantics).

    MegaGlest advances ``animProgress`` by ``animSpeed * 100000 / (100 *
    updateFps)`` per tick and completes at ``100000`` (unit.cpp
    ``ANIMATION_SPEED_MULTIPLIER`` / ``speedDivider``), so one cycle takes
    ``100 / animSpeed`` seconds. A missing/zero speed freezes the animation
    in MegaGlest; we fall back to 100 (1 second) so converted units still
    move.
    """
    if anim_speed <= 0:
        return 1.0
    return 100.0 / anim_speed


# ---------------------------------------------------------------------------
# clustering
# ---------------------------------------------------------------------------


def _features(
    meshes: Sequence[g3dlib.Mesh], frame_count: int
) -> tuple[list[list[float]], list[list[float]]]:
    """Per-vertex feature vectors: rest position + per-frame displacements.

    Dimensions are standardized (mean 0, std 1 per dimension) so rest
    position and motion contribute comparably; ``rest`` is returned
    separately for the DAE and for proximity assignment.
    """
    rest = _concat_rest(meshes)
    n = len(rest)
    raw: list[list[float]] = []
    for i in range(n):
        point = list(rest[i])
        for f in range(1, frame_count):
            pos = _vertex_at(meshes, i, f)
            for axis in range(3):
                point.append(pos[axis] - rest[i][axis])
        raw.append(point)
    # standardize each dimension
    dims = len(raw[0])
    features = [list(row) for row in raw]
    for d in range(dims):
        values = [row[d] for row in raw]
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        std = math.sqrt(variance)
        if std < 1e-9:
            continue
        for row in features:
            row[d] = (row[d] - mean) / std
    return features, rest


def _concat_rest(meshes: Sequence[g3dlib.Mesh]) -> list[list[float]]:
    rest: list[list[float]] = []
    for mesh in meshes:
        stride = mesh.vertex_count * 3
        positions = mesh.vertices[:stride]
        rest.extend([positions[i], positions[i + 1], positions[i + 2]] for i in range(0, stride, 3))
    return rest


def _vertex_at(meshes: Sequence[g3dlib.Mesh], merged_index: int, frame: int) -> list[float]:
    """Position of the ``merged_index``-th merged vertex in ``frame``."""
    offset = 0
    for mesh in meshes:
        count = mesh.vertex_count
        if merged_index < offset + count:
            stride = count * 3
            start = frame * stride + (merged_index - offset) * 3
            return list(mesh.vertices[start : start + 3])
        offset += count
    raise IndexError(merged_index)


def _frame_positions(meshes: Sequence[g3dlib.Mesh], frame: int) -> list[list[float]]:
    return [_vertex_at(meshes, i, frame) for i in range(sum(m.vertex_count for m in meshes))]


def _cluster_count(vertex_count: int, requested: int) -> int:
    """Clamp the requested cluster count to something meaningful for the mesh."""
    if vertex_count <= 0:
        return 1
    return max(1, min(requested, vertex_count // 5 + 1))


def _kmeans(features: Sequence[Sequence[float]], k: int) -> tuple[list[list[float]], list[int]]:
    """Deterministic k-means++ over ``features``; returns (centroids, labels)."""
    n = len(features)
    rng = random.Random(0)
    dims = len(features[0])
    centroids: list[list[float]] = [list(features[rng.randrange(n)])]
    while len(centroids) < k:
        distances = [min(_dist2(features[i], c) for c in centroids) for i in range(n)]
        total = sum(distances)
        if total <= 1e-18:
            centroids.append(list(features[rng.randrange(n)]))
            continue
        target = rng.random() * total
        running = 0.0
        for i, d in enumerate(distances):
            running += d
            if running >= target:
                centroids.append(list(features[i]))
                break
        else:
            centroids.append(list(features[-1]))

    labels = [0] * n
    for _ in range(_KMEANS_ITERATIONS):
        changed = False
        sums = [[0.0] * dims for _ in range(k)]
        counts = [0] * k
        for i, point in enumerate(features):
            best = min(range(k), key=lambda b: _dist2(point, centroids[b]))
            if best != labels[i]:
                labels[i] = best
                changed = True
            for d in range(dims):
                sums[best][d] += point[d]
            counts[best] += 1
        for b in range(k):
            if counts[b]:
                centroids[b] = [s / counts[b] for s in sums[b]]
        if not changed:
            break
    return centroids, labels


def _dist2(a: Sequence[float], b: Sequence[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def _soft_weights(
    point: Sequence[float], centroids: Sequence[Sequence[float]]
) -> list[tuple[int, float]]:
    """Top-4 normalized cluster weights for one vertex (near-hard blend)."""
    distances = [_dist2(point, c) for c in centroids]
    nearest = min(distances)
    denom = max(nearest, 1e-18)
    weights = [math.exp(-3.0 * (d / denom) ** 2) for d in distances]
    ranked = sorted(range(len(weights)), key=lambda b: weights[b], reverse=True)
    selected = ranked[:_MAX_INFLUENCES]
    total = sum(weights[b] for b in selected)
    if total <= 1e-18:
        return [(ranked[0], 1.0)]
    return [(b, weights[b] / total) for b in selected]


def _rest_centroid(
    rest: Sequence[Sequence[float]],
    weights: Sequence[Sequence[tuple[int, float]]],
    bone: int,
) -> list[float]:
    total = 0.0
    centroid = [0.0, 0.0, 0.0]
    for point, vertex_weights in zip(rest, weights):
        weight = next((w for b, w in vertex_weights if b == bone), 0.0)
        if weight <= 0:
            continue
        total += weight
        for axis in range(3):
            centroid[axis] += weight * point[axis]
    if total <= 1e-18:
        return [0.0, 0.0, 0.0]
    return [c / total for c in centroid]


def _assign_weights(point: Sequence[float], centroids: Sequence[Sequence[float]]) -> list[float]:
    """Soft per-bone weights for a foreign model's vertex (rest proximity)."""
    distances = [_dist2(point, c) for c in centroids]
    nearest = min(distances)
    denom = max(nearest, 1e-18)
    weights = [math.exp(-3.0 * (d / denom) ** 2) for d in distances]
    total = sum(weights)
    if total <= 1e-18:
        return [1.0 if b == distances.index(nearest) else 0.0 for b in range(len(centroids))]
    return [w / total for w in weights]


# ---------------------------------------------------------------------------
# rigid fitting (scale-free Kabsch via 3x3 SVD)
# ---------------------------------------------------------------------------


def _kabsch(
    src: Sequence[Sequence[float]],
    dst: Sequence[Sequence[float]],
    weights: Sequence[float],
) -> tuple[list[float], list[float]]:
    """Weighted rigid fit (rotation + translation, no scale).

    Minimizes ``sum(w * |R*src + t - dst|^2)``; returns (R as 9 row-major
    floats, t as 3 floats). Degenerate inputs (zero weight or a single
    point) yield identity rotation.
    """
    total = sum(weights)
    if total <= 1e-18:
        return ([1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0])
    c0 = [sum(w * p[axis] for p, w in zip(src, weights)) / total for axis in range(3)]
    c1 = [sum(w * p[axis] for p, w in zip(dst, weights)) / total for axis in range(3)]

    h = [[0.0] * 3 for _ in range(3)]
    for p, q, w in zip(src, dst, weights):
        for i in range(3):
            for j in range(3):
                h[i][j] += w * (p[i] - c0[i]) * (q[j] - c1[j])

    u, _, vt = _svd3(h)
    # R = V * U^T, with reflection removed
    v = _transpose(vt)
    r = _matmul(v, _transpose(u))
    if _det3(r) < 0:
        for row in r:
            row[2] = -row[2]
    flat = [r[i][j] for i in range(3) for j in range(3)]
    t = [c1[i] - sum(r[i][j] * c0[j] for j in range(3)) for i in range(3)]
    return flat, t


def _svd3(a: Sequence[Sequence[float]]) -> tuple[list[list[float]], list[float], list[list[float]]]:
    """SVD of a 3x3 matrix via Jacobi eigen-decomposition of A^T A."""
    at_a = [[sum(a[k][i] * a[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    v = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    for _ in range(64):
        off_diag = sum(at_a[i][j] ** 2 for i in range(3) for j in range(i + 1, 3))
        if off_diag < 1e-22:
            break
        for p in range(3):
            for q in range(p + 1, 3):
                apq = at_a[p][q]
                if abs(apq) < 1e-16:
                    continue
                theta = (at_a[q][q] - at_a[p][p]) / (2.0 * apq)
                sign = 1.0 if theta >= 0 else -1.0
                t = sign / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(3):
                    if k == p or k == q:
                        continue
                    akp = at_a[k][p]
                    akq = at_a[k][q]
                    at_a[k][p] = c * akp - s * akq
                    at_a[k][q] = s * akp + c * akq
                    at_a[p][k] = at_a[k][p]
                    at_a[q][k] = at_a[k][q]
                app = at_a[p][p]
                aqq = at_a[q][q]
                apq = at_a[p][q]
                at_a[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq
                at_a[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq
                at_a[p][q] = 0.0
                at_a[q][p] = 0.0
                for k in range(3):
                    vkp = v[k][p]
                    vkq = v[k][q]
                    v[k][p] = c * vkp - s * vkq
                    v[k][q] = s * vkp + c * vkq

    eigenvalues = [at_a[i][i] for i in range(3)]
    order = sorted(range(3), key=lambda i: eigenvalues[i], reverse=True)
    v = [[v[i][order[j]] for j in range(3)] for i in range(3)]
    sigma = [math.sqrt(max(at_a[i][i], 0.0)) for i in order]
    u = [[0.0] * 3 for _ in range(3)]
    for j in range(3):
        if sigma[j] < 1e-12:
            u[j][j] = 1.0
            continue
        for i in range(3):
            u[i][j] = sum(a[i][k] * v[k][j] for k in range(3)) / sigma[j]
    # NOTE: do NOT flip u to force det(u)=+1 here - that breaks the
    # reconstruction u*Sigma*v^T = A. Reflection removal is _kabsch's job
    # (applied to the final R, per Umeyama).
    return u, sigma, _transpose(v)


def _transpose(m: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[m[j][i] for j in range(len(m))] for i in range(len(m[0]))]


def _matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    return [
        [sum(a[i][k] * b[k][j] for k in range(len(b))) for j in range(len(b[0]))]
        for i in range(len(a))
    ]


def _det3(m: Sequence[Sequence[float]]) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )
