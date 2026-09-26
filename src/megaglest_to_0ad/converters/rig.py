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

All math is deterministic (fixed numpy seed, closed-form SVD) so reconverts
are byte-identical. The clustering/fitting runs on numpy arrays: the k-means
feature space is (rest pos + per-frame displacement) so models with many
animation frames get high-dimensional feature vectors — the pure-Python
all-pairs loops were the conversion's dominant cost (a 76-frame 2465-vertex
model took ~36 s; the vectorized kernels run it in well under a second).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Vendored importer bootstrap (idempotent; see mesh_converter).
_VENDOR_G3D_DIR = Path(__file__).resolve().parents[3] / "vendor" / "g3d"
if str(_VENDOR_G3D_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_G3D_DIR))

import g3dlib  # noqa: E402  (vendored third-party module; see vendor/g3d/)

_MAX_INFLUENCES = 4
_KMEANS_ITERATIONS = 60

_IDENTITY9 = (
    [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
)
_ZERO3 = [0.0, 0.0, 0.0]


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
    # centroid of all rest-position vertices in this group (base-model frame)
    rest_center: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])


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
        stack = _frame_stack(meshes, frame_count)
        rest = stack[0]
        features = _features(rest, stack)
        k = _cluster_count(len(rest), bone_count - 1)
        centroids, _ = _kmeans(features, k)
        soft_matrix = _soft_weight_matrix(features, centroids)
        n = len(rest)
        pair_weights = [
            _top_influences(soft_matrix[i]) for i in range(soft_matrix.shape[0])
        ]
        rest_centroids = _rest_centroids(rest, soft_matrix, pair_weights)
        rest_center = rest.mean(axis=0).tolist() if n > 0 else [0.0, 0.0, 0.0]
        rig_groups.append(
            RigGroup(
                mesh_indices=list(group),
                vertex_weights=pair_weights,
                rest_centroids=rest_centroids,
                assignments=[int(soft_matrix[i].argmax()) for i in range(n)],
                rest_center=rest_center,
            )
        )
    return Rig(root_name=root_name, bone_names=names, groups=rig_groups)


def fit_group_frames(
    base_model: g3dlib.G3DModel,
    anim_model: g3dlib.G3DModel,
    rig: Rig,
    group_index: int = 0,
    use_base_weights: bool = False,
) -> tuple[
    list[list[tuple[list[float], list[float]]]], list[list[tuple[int, float]]]
]:
    """Per-frame rigid transforms for ``rig``'s group ``group_index``.

    Returns ``(frames, vertex_weights)`` where ``frames[f][b]`` = ``(R, t)``
    (rotation as 9 row-major floats, translation as 3 floats) for bone ``b``
    in frame ``f``, and ``vertex_weights[i]`` is the top-4 ``(bone, weight)``
    blend for vertex ``i``. Frame 0 is the rest pose (identity).

    ``base_model`` supplies the rest vertices (matching the DAE geometry).
    ``anim_model`` supplies the per-frame morph targets. When the two models
    differ, nearest-neighbour displacement transfer maps the animation morphs
    onto the base model's vertices so that the bone transforms correctly
    deform the DAE geometry. ``use_base_weights`` reuses the exact k-means
    weights from rig build (same-model fast path). Bone 0 (root) carries the
    whole-model rigid fit; no vertex is weighted to it.

    The returned ``vertex_weights`` are the ones the bone fits were solved
    against, so the caller must write these into the DAE skin rather than the
    rig's: linear blend skinning only reproduces the fit if the file's weights
    and the fitted weights are the same numbers.
    """
    group = rig.groups[group_index]
    base_meshes = [base_model.meshes[i] for i in group.mesh_indices]
    base_rest = _frame_stack(base_meshes, 1)[0]
    n_base = len(base_rest)

    same_model = use_base_weights
    if same_model:
        anim_meshes = base_meshes
        anim_rest = base_rest
        frame_count = min(m.frame_count for m in anim_meshes)
    else:
        anim_meshes = anim_model.meshes
        anim_rest = _frame_stack(anim_meshes, 1)[0]
        frame_count = min(m.frame_count for m in anim_meshes)
        if frame_count == 0:
            frame_count = min(m.frame_count for m in base_meshes)

    anim_stack = _frame_stack(anim_meshes, frame_count)

    # Pre-compute nearest-neighbour mapping for displacement transfer. When
    # base and anim models differ, each base vertex borrows the displacement
    # of its closest rest-pose neighbour in the anim model. The mapping is
    # based on rest positions (static) and reused per frame.
    nn_map = _nearest_map(anim_rest, base_rest) if not same_model else None

    if nn_map is None:
        target_stack = anim_stack
    else:
        target_stack = (
            base_rest[None, :, :]
            + anim_stack[:, nn_map, :]
            - anim_rest[None, nn_map, :]
        )

    if use_base_weights or frame_count <= 1:
        # Nothing to re-cluster: the rig's own partition is what the DAE stores.
        vertex_weights = group.vertex_weights
    else:
        # Weight by how each vertex *moves* in the trajectory being
        # transferred, not by where it happens to sit at rest. The bone fits
        # below are solved against target_stack, so the weights have to
        # describe that same motion: the rig's clusters were built on the base
        # model's own animation and say nothing about this one. Assigning by
        # rest proximity instead leaves the worst vertex most of a frame's
        # displacement from the source, and puts seam vertices on bones that
        # disagree about which way to move, which reads as flicker.
        features = _features(base_rest, target_stack)
        centroids, _ = _kmeans(features, _cluster_count(n_base, rig.part_count))
        soft_matrix = _soft_weight_matrix(features, centroids)
        vertex_weights = [
            _top_influences(soft_matrix[i]) for i in range(soft_matrix.shape[0])
        ]

    # Expand the top-4 (bone, weight) pairs into full per-bone vectors so bone
    # b+1 below reads cluster b's weight (one row per bone, matching
    # _kabsch_batch's (M, verts) weight layout). The DAE stores these same
    # pairs, so what the file reconstructs is exactly what was fitted here.
    weights = np.zeros((rig.part_count, n_base), dtype=np.float64)
    for i, pairs in enumerate(vertex_weights):
        for bone, weight in pairs:
            weights[bone, i] = weight

    root_rot, root_t = _kabsch_batch(base_rest, target_stack, np.ones(n_base))
    bone_rot, bone_t = _kabsch_batch(base_rest, target_stack, weights)

    frames: list[list[tuple[list[float], list[float]]]] = []
    for f in range(frame_count):
        if f == 0:
            # Frame 0 IS the rest pose: exact identity (no SVD drift on a
            # symmetric fit), so the base pose renders exactly as authored.
            frames.append([(_IDENTITY9, _ZERO3)] * rig.bone_count)
            continue
        transforms: list[tuple[list[float], list[float]]] = [
            (root_rot[0, f].reshape(-1).tolist(), root_t[0, f].tolist())
        ]
        transforms.extend(
            (bone_rot[b, f].reshape(-1).tolist(), bone_t[b, f].tolist())
            for b in range(rig.part_count)
        )
        frames.append(transforms)
    return frames, vertex_weights


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
# clustering (vectorized)
# ---------------------------------------------------------------------------


def _frame_stack(meshes: Sequence[g3dlib.Mesh], frame_count: int) -> np.ndarray:
    """Merged per-frame positions as ``(frame_count, total_verts, 3)``.

    Mesh vertices are stored frame-major: ``frame * count * 3`` consecutive
    floats per mesh (see ``_concat_rest``/``_vertex_at`` in the old layout,
    preserved by the G3D reader). Frame 0 is the rest pose.
    """
    blocks = []
    for mesh in meshes:
        count = mesh.vertex_count
        stride = count * 3
        grid = (
            np.asarray(mesh.vertices, dtype=np.float64)[: frame_count * stride]
            .reshape(frame_count, count, 3)
        )
        blocks.append(grid)
    return np.concatenate(blocks, axis=1)


def _features(rest: np.ndarray, stack: np.ndarray) -> np.ndarray:
    """Per-vertex feature vectors: rest position + per-frame displacements.

    ``stack`` is the (frames, verts, 3) trajectory the rig has to reproduce --
    a baked model's own animation, or the transferred one when a neighbouring
    model supplies the motion -- so the clusters describe the motion actually
    being fitted. Dimensions are standardized (mean 0, std 1 per dimension) so
    rest position and motion contribute comparably.
    """
    if len(stack) <= 1:
        # Static fallback: cluster on the raw rest positions only.
        return rest.copy()
    deltas = stack[1:] - rest  # (frames-1, verts, 3)
    raw = np.concatenate(
        [rest, deltas.transpose(1, 0, 2).reshape(len(rest), -1)], axis=1
    )
    features = raw.copy()
    mean = raw.mean(axis=0)
    variance = raw.var(axis=0)
    std = np.sqrt(variance)
    std_ok = std > 1e-9
    features[:, std_ok] = (raw[:, std_ok] - mean[std_ok]) / std[std_ok]
    return features


def _kmeans(features: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic k-means++ over ``features``; returns (centroids, labels)."""
    # Fresh RNG per call so every build_rig reproduces (the former
    # ``random.Random(0)`` contract).
    rng = np.random.default_rng(0)
    n = len(features)
    dims = features.shape[1]
    centroids: list[np.ndarray] = [features[rng.integers(n)].copy()]
    while len(centroids) < k:
        pts = np.asarray(centroids)
        d2 = ((features[:, None, :] - pts[None, :, :]) ** 2).sum(-1)
        distances = d2.min(-1)
        total = distances.sum()
        if total <= 1e-18:
            centroids.append(features[rng.integers(n)].copy())
            continue
        target = rng.random() * total
        index = int(np.searchsorted(np.cumsum(distances), target, side="left"))
        centroids.append(features[index].copy())

    centroids_arr: np.ndarray = np.asarray(centroids)
    # -1 cannot collide with a real cluster index, so the first pass always
    # counts as a change. Zero would break out before any centroid update
    # whenever every point's nearest seed happens to be cluster 0.
    labels = np.full(n, -1, dtype=np.int64)
    for _ in range(_KMEANS_ITERATIONS):
        d2 = ((features[:, None, :] - centroids_arr[None, :, :]) ** 2).sum(-1)
        best = d2.argmin(-1)
        if not (best != labels).any():
            break
        labels = best
        sums = np.zeros((k, dims), dtype=np.float64)
        np.add.at(sums, labels, features)
        counts = np.bincount(labels, minlength=k)
        new_centroids = np.where(
            (counts > 0)[:, None],
            sums / np.maximum(counts, 1)[:, None],
            centroids_arr,
        )
        centroids_arr = new_centroids
    return centroids_arr, labels


def _soft_weight_matrix(
    features: np.ndarray, centroids: np.ndarray
) -> np.ndarray:
    """Soft per-cluster weights for every vertex, ``(verts, k)``.

    Near-hard blending: the exponent -3.0 makes the closest cluster dominate
    (displacement mostly rigid per part), with a small blend band across
    part boundaries so the DAE skins smoothly.
    """
    d2 = ((features[:, None, :] - centroids[None, :, :]) ** 2).sum(-1)
    nearest = d2.min(-1, keepdims=True)
    denom = np.maximum(nearest, 1e-18)
    return np.exp(-3.0 * (d2 / denom) ** 2)


def _top_influences(weights: np.ndarray) -> list[tuple[int, float]]:
    """Top-4 normalized cluster weights for one vertex (soft blend)."""
    order = np.argsort(-weights)[:_MAX_INFLUENCES]
    total = weights[order].sum()
    if total <= 1e-18:
        return [(int(weights.argmax()), 1.0)]
    return [(int(b), float(weights[b] / total)) for b in order]


def _rest_centroids(
    rest: np.ndarray,
    weights: np.ndarray,
    pair_weights: Sequence[Sequence[tuple[int, float]]],
) -> list[list[float]]:
    """Weighted rest-position centroid of each cluster, ``(k, 3)``.

    Matches the original per-bone ``_rest_centroid``: only the top-4
    (bone, weight) pairs per vertex contribute, with their *renormalized*
    weights, so clusters that never appear in any selected set get a zero
    centroid.
    """
    k = weights.shape[1]
    n = weights.shape[0]
    selected = np.zeros((n, k), dtype=np.float64)
    for i, pairs in enumerate(pair_weights):
        for bone, weight in pairs:
            selected[i, bone] = weight
    totals = selected.sum(axis=0)  # (k,)
    centroids = (selected.T @ rest) / np.maximum(totals, 1e-18)[:, None]
    centroids = np.where((totals > 1e-18)[:, None], centroids, 0.0)
    return centroids.tolist()


# Below this, the full distance matrix is a couple of megabytes and building a
# grid costs more than it saves, so the quadratic expression is kept verbatim and
# small models take the identical code path they always did. Measured crossover
# on a 3D model cloud is ~1250 points (at 1000 the grid is still 8% slower; at
# 1400 it is 14% faster), and 1280 squared floats peaks at 13 MB.
_BRUTE_LIMIT = 1280
# How many times to widen the cells before falling back to exact arithmetic.
_GRID_WIDENINGS = 8


def _nearest_map(points: np.ndarray, queries: np.ndarray) -> np.ndarray:
    """Index into ``points`` of the closest point for each ``queries`` row.

    Equivalent to ``((queries[:, None] - points[None]) ** 2).sum(-1).argmin(-1)``
    for every input, ties included: the lowest index wins an exact tie, so
    converted output is unchanged. That expression needs ``queries * points``
    doubles, which was a hard ceiling rather than a slow path -- a 32k-vertex
    model wanted 8 GB per temporary and the kernel killed the process.

    Larger inputs go through a uniform grid: points are bucketed into cells
    about as wide as the mean point spacing, and each query inspects only the
    27 cells around it. A query whose match may sit outside those cells (its
    best distance reaches past the edge of the examined block) is retried with
    wider cells, so the result stays exact instead of merely close.
    """
    points = np.ascontiguousarray(points, dtype=np.float64)
    queries = np.ascontiguousarray(queries, dtype=np.float64)
    if len(points) <= _BRUTE_LIMIT or len(queries) <= _BRUTE_LIMIT:
        d2 = ((queries[:, None, :] - points[None, :, :]) ** 2).sum(-1)
        return d2.argmin(-1)
    if len(queries) == 0:
        return np.empty(0, dtype=np.int64)
    return _nearest_map_grid(points, queries)


def _nearest_map_grid(points: np.ndarray, queries: np.ndarray) -> np.ndarray:
    """Exact nearest neighbour via a uniform grid, widening until provable."""
    best_i = np.zeros(len(queries), dtype=np.int64)
    best_d = np.full(len(queries), np.inf)
    pending = np.arange(len(queries))
    origin = points.min(axis=0)
    extent = np.maximum(points.max(axis=0) - origin, 0.0)
    span = float(extent.max())
    if span <= 0:
        # Every point is the same point, so the first index wins every query.
        return best_i

    # Aim for a handful of points per cell along the longest axis.
    cells = max(1.0, (len(points) / 8.0) ** (1 / 3))
    h = span / cells
    for _ in range(_GRID_WIDENINGS):
        counts, keys, order = _bucket(points, origin, extent, h)
        d, i = _search_blocks(queries[pending], points, counts, keys, order, origin, h)
        better = d < best_d[pending]
        tied = (d == best_d[pending]) & (i < best_i[pending])
        take = better | tied
        idx = pending[take]
        best_d[idx] = d[take]
        best_i[idx] = i[take]

        # A match that reaches past the nearest face of the 3x3x3 block may sit
        # in a cell that was not searched, so it has to be settled with wider
        # cells. Cell coordinates are left unclamped: cells outside the point
        # cloud are empty, so the wider block is a superset of what was skipped
        # and the test stays conservative.
        searched = queries[pending]
        raw = (searched - origin) / h
        low = origin + (raw - 1.0) * h
        high = origin + (raw + 2.0) * h
        shell = np.minimum(searched - low, high - searched).min(-1)
        pending = pending[~(best_d[pending] < shell**2)]
        if not len(pending):
            return best_i
        h *= 2.0
    # Pathological point distribution: settle the rest with exact arithmetic.
    for start in range(0, len(pending), 512):
        idx = pending[start : start + 512]
        chunk = queries[idx]
        best_i[idx] = ((chunk[:, None, :] - points[None, :, :]) ** 2).sum(-1).argmin(-1)
    return best_i


def _bucket(
    points: np.ndarray, origin: np.ndarray, extent: np.ndarray, h: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cell counts per axis, cells packed into one integer, points by cell.

    Points are ordered by ``(cell, original index)`` so that the lowest index
    among equals is always the first candidate seen in a cell.
    """
    counts = np.maximum((extent / h).astype(np.int64) + 1, 1)
    cell = np.minimum(((points - origin) / h).astype(np.int64), counts - 1)
    keys = (cell[:, 0] * counts[1] + cell[:, 1]) * counts[2] + cell[:, 2]
    order = np.lexsort((np.arange(len(points)), keys))
    return counts, keys[order], order


def _search_blocks(
    queries: np.ndarray,
    points: np.ndarray,
    counts: np.ndarray,
    keys: np.ndarray,
    order: np.ndarray,
    origin: np.ndarray,
    h: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Best (distance, index) per query over the 27 cells around each one."""
    n = len(queries)
    best_d = np.full(n, np.inf)
    best_i = np.zeros(n, dtype=np.int64)
    cell = np.minimum(((queries - origin) / h).astype(np.int64), counts - 1)
    packed = (cell[:, 0] * counts[1] + cell[:, 1]) * counts[2] + cell[:, 2]
    empty = np.iinfo(np.int64).max

    for start in range(0, n, 1024):
        block = slice(start, min(start + 1024, n))
        qb = queries[block]
        kb = packed[block]
        cx, cy, cz = kb // (counts[1] * counts[2]), (kb // counts[2]) % counts[1], kb % counts[2]
        bd = np.full(len(qb), np.inf)
        bi = np.zeros(len(qb), dtype=np.int64)
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for oz in (-1, 0, 1):
                    want = (
                        np.clip(cx + ox, 0, counts[0] - 1) * counts[1]
                        + np.clip(cy + oy, 0, counts[1] - 1)
                    ) * counts[2] + np.clip(cz + oz, 0, counts[2] - 1)
                    lo = np.searchsorted(keys, want, "left")
                    hi = np.searchsorted(keys, want, "right")
                    per = hi - lo
                    live = np.nonzero(per)[0]
                    if not len(live):
                        continue
                    size = per[live]
                    starts = np.concatenate(([0], np.cumsum(size)[:-1]))
                    whose = np.repeat(live, size)
                    cand = order[np.arange(size.sum()) + np.repeat(lo[live] - starts, size)]
                    d2 = ((qb[whose] - points[cand]) ** 2).sum(-1)
                    near = np.full(len(qb), np.inf)
                    np.minimum.at(near, whose, d2)
                    on = d2 == near[whose]
                    low = np.full(len(qb), empty, dtype=np.int64)
                    np.minimum.at(low, whose[on], cand[on])
                    take = (near < bd) | ((near == bd) & (low < bi))
                    bd = np.where(take, near, bd)
                    bi = np.where(take, low, bi)
        best_d[block] = bd
        best_i[block] = bi
    return best_d, best_i


def _cluster_count(vertex_count: int, requested: int) -> int:
    """Clamp the requested cluster count to something meaningful for the mesh."""
    if vertex_count <= 0:
        return 1
    return max(1, min(requested, vertex_count // 5 + 1))


# ---------------------------------------------------------------------------
# rigid fitting (scale-free Kabsch via batched 3x3 SVD)
# ---------------------------------------------------------------------------


def _kabsch_batch(
    src: np.ndarray,
    dsts: np.ndarray,
    weights: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Weighted rigid fits ``src`` -> each ``dsts`` frame, batched.

    ``src`` is ``(verts, 3)``, ``dsts`` is ``(frames, verts, 3)`` and
    ``weights`` is ``(M, verts)`` (a row per bone/weight vector, all fitting
    the same ``dsts``). Returns ``(rot, trans)`` with shapes ``(M, frames,
    3, 3)`` and ``(M, frames, 3)``, minimizing ``sum(w * |R p + t - q|^2)``.
    Rows with zero total weight (or degenerate input) yield identity.
    """
    src = np.asarray(src, dtype=np.float64)
    dsts = np.asarray(dsts, dtype=np.float64)
    weights_arr = np.asarray(weights, dtype=np.float64)
    if weights_arr.ndim == 1:
        weights_arr = weights_arr[None, :]
    m, _ = weights_arr.shape
    frames = dsts.shape[0]

    totals = weights_arr.sum(-1)  # (m,)
    ok = totals > 1e-18
    denom = np.maximum(totals, 1e-18)

    c0 = (weights_arr @ src) / denom[:, None]  # (m, 3)
    c1 = np.einsum("mv,fvj->mfj", weights_arr, dsts) / denom[:, None, None]
    srcc = src[None, :, :] - c0[:, None, :]  # (m, verts, 3)  = P - c0
    # Original H[i][j] = sum_w (P_i - c0)(Q_j - c1); SVD of P*Q^T so that
    # R = V*U^T maps src onto dst.
    cov = np.empty((m, frames, 3, 3), dtype=np.float64)
    for f in range(frames):
        dstc = dsts[f][None, :, :] - c1[:, f, None, :]  # (m, verts, 3)  = Q - c1
        cov[:, f] = np.einsum("mv,mvi,mvj->mij", weights_arr, srcc, dstc)

    u, _sigma, vh = np.linalg.svd(cov)
    # R = V * U^T (V = vh^T), with reflection removed.
    rot = vh.transpose(0, 1, 3, 2) @ u.transpose(0, 1, 3, 2)
    mirrored = np.linalg.det(rot) < 0
    rot[mirrored, :, 2] *= -1.0
    trans = c1 - np.einsum("mfij,mj->mfi", rot, c0)

    identity = np.broadcast_to(
        np.eye(3, dtype=np.float64), (m, *rot.shape[1:])
    ).copy()
    rot = np.where(ok[:, None, None, None], rot, identity)
    trans = np.where(ok[:, None, None], trans, 0.0)
    return rot, trans


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
    rot, trans = _kabsch_batch(
        np.asarray(src, dtype=np.float64),
        np.asarray(dst, dtype=np.float64)[None, :, :],
        np.asarray(weights, dtype=np.float64),
    )
    return rot[0, 0].reshape(-1).tolist(), trans[0, 0].tolist()



