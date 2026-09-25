"""Spatial-index nearest-neighbour for displacement transfer.

The scaling tests run in a child process on purpose: the previous
implementation built a full n-by-n distance matrix, and at these sizes the
kernel kills the process. Asserting that in-process would take the whole test
run down instead of failing one test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from megaglest_to_0ad.converters.rig import _nearest_map, _nearest_map_grid

SRC = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")

# Documented ceiling from issue #2. The n-by-n matrix this replaces needed
# n*n*8 bytes per temporary -- 3.2 GB at 20k, and the kernel killed it.
PEAK_MB_LIMIT = 1024


def _brute_force(points: np.ndarray, queries: np.ndarray) -> np.ndarray:
    """Oracle: the exact n-by-n definition, in blocks so it stays affordable."""
    out = np.empty(len(queries), dtype=np.int64)
    for start in range(0, len(queries), 256):
        chunk = queries[start : start + 256]
        d2 = ((chunk[:, None, :] - points[None, :, :]) ** 2).sum(-1)
        out[start : start + len(chunk)] = d2.argmin(-1)
    return out


def _cloud(n: int, seed: int = 0) -> np.ndarray:
    """A rest-pose-shaped cloud: a displaced surface, not a gaussian blob."""
    rng = np.random.default_rng(seed)
    side = int(np.ceil(np.sqrt(n)))
    grid = np.stack(
        np.meshgrid(np.linspace(0, 4, side), np.linspace(0, 4, side)), -1
    ).reshape(-1, 2)
    pts = np.zeros((n, 3))
    pts[:, :2] = grid[:n]
    pts[:, 2] = 0.3 * np.sin(pts[:, 0] * 2.0) * np.cos(pts[:, 1] * 1.7)
    return pts + rng.normal(0, 0.01, (n, 3))


CHILD_PREAMBLE = f"""\
import json, resource, sys
sys.path.insert(0, {SRC!r})
import numpy as np
from megaglest_to_0ad.converters.rig import _nearest_map


def cloud(n, seed=0):
    rng = np.random.default_rng(seed)
    side = int(np.ceil(np.sqrt(n)))
    grid = np.stack(
        np.meshgrid(np.linspace(0, 4, side), np.linspace(0, 4, side)), -1
    ).reshape(-1, 2)
    pts = np.zeros((n, 3))
    pts[:, :2] = grid[:n]
    pts[:, 2] = 0.3 * np.sin(pts[:, 0] * 2.0) * np.cos(pts[:, 1] * 1.7)
    return pts + rng.normal(0, 0.01, (n, 3))

"""


def _run_in_child(body: str) -> dict:
    """Execute ``body`` in a fresh interpreter; return its reported numbers."""
    code = CHILD_PREAMBLE + textwrap.dedent(body).strip() + "\n"
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, f"child failed:\n{proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_maps_20k_points_within_the_memory_ceiling() -> None:
    """A 20k-vertex model maps without an n-by-n distance matrix."""
    report = _run_in_child(
        """
        n = 20_000
        points = cloud(n)
        queries = cloud(n, seed=1)
        got = _nearest_map(points, queries)
        probe = np.linspace(0, n - 1, 200).astype(int)
        expect = np.concatenate(
            [
                ((queries[probe][:, None, :] - points[None, :, :]) ** 2).sum(-1).argmin(-1)
            ]
        )
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
        print(json.dumps({
            "mismatches": int((got[probe] != expect).sum()),
            "peak_mb": peak,
        }))
        """
    )
    assert report["mismatches"] == 0
    assert report["peak_mb"] < PEAK_MB_LIMIT, (
        f"peak {report['peak_mb']:.0f} MB exceeds the {PEAK_MB_LIMIT} MB ceiling"
    )


def test_identical_point_sets_map_each_vertex_to_itself() -> None:
    """A model's own vertices are each other's nearest neighbour.

    This is the same-model case, so ``points`` and ``queries`` are the same
    array: the only zero distance is the one to itself, and every query must
    come back as its own index. A spatial index that returned some other
    zero-ish match would silently reshape every frame of every animation.
    """
    report = _run_in_child(
        """
        n = 20_000
        points = cloud(n)
        got = _nearest_map(points, points.copy())
        print(json.dumps({
            "wrong": int((got != np.arange(n)).sum()),
            "peak_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024),
        }))
        """
    )
    assert report["wrong"] == 0
    assert report["peak_mb"] < PEAK_MB_LIMIT


def test_matches_brute_force_on_a_foreign_model() -> None:
    """Different point sets give the same answer the quadratic version did."""
    points = _cloud(2_000, seed=3)
    queries = _cloud(1_500, seed=7)
    assert np.array_equal(_nearest_map(points, queries), _brute_force(points, queries))


def _shaped(name: str, n: int, seed: int) -> np.ndarray:
    """Point sets that break naive bucketing in different ways."""
    rng = np.random.default_rng(seed)
    if name == "surface":
        return np.c_[rng.random((n, 2)) * 4, rng.random(n)]
    if name == "gaussian":
        return rng.normal(0, 1, (n, 3))
    if name == "clustered":
        centres = rng.normal(0, 6, (6, 3))
        return centres[rng.integers(0, 6, n)] + rng.normal(0, 0.05, (n, 3))
    if name == "collinear":
        t = rng.random(n) * 10
        return np.c_[t, np.zeros(n), np.zeros(n)]
    if name == "planar":
        return np.c_[rng.random((n, 2)) * 4, np.full(n, 1.5)]
    if name == "duplicate_heavy":
        base = rng.normal(0, 1, (max(n // 20, 1), 3))
        return base[rng.integers(0, len(base), n)]
    if name == "tight":
        return rng.normal(0, 1e-4, (n, 3))
    raise AssertionError(name)  # pragma: no cover


@pytest.mark.parametrize("shape", ["surface", "gaussian", "clustered", "collinear",
                                   "planar", "duplicate_heavy", "tight"])
@pytest.mark.parametrize("n", [600, 1_500, 4_000])
def test_grid_is_exact_for_every_shape_and_size(shape: str, n: int) -> None:
    """Exercise the grid directly, so coverage does not depend on dispatch.

    The public entry point sends small inputs to the quadratic path, which
    would leave the grid untested at the sizes where it is easiest to get the
    cell arithmetic wrong.
    """
    points = _shaped(shape, n, seed=n)
    queries = points + np.random.default_rng(n).normal(0, 0.02, points.shape)
    assert np.array_equal(_nearest_map_grid(points, queries), _brute_force(points, queries))


@pytest.mark.parametrize("n", [600, 2_000])
def test_dispatch_matches_brute_force_on_both_sides_of_the_threshold(n: int) -> None:
    points = _cloud(n, seed=11)
    queries = points + np.random.default_rng(n).normal(0, 0.02, points.shape)
    assert np.array_equal(_nearest_map(points, queries), _brute_force(points, queries))


@pytest.mark.parametrize(
    "points, queries",
    [
        ([[0.0, 0.0, 0.0]], [[5.0, 5.0, 5.0], [-1.0, 0.0, 0.0]]),
        ([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]], [[3.0, 3.0, 3.0]]),
        ([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]], [[0.9, 0.0, 0.0]]),
        ([[0.0, 0.0, 0.0], [10.0, 10.0, 10.0]], [[-99.0, 0.0, 0.0], [99.0, 0.0, 0.0]]),
        (np.zeros((500, 3)), np.ones((17, 3))),
    ],
)
def test_edge_cases(points, queries) -> None:
    """Duplicates, collinear points, single points and out-of-hull queries."""
    p = np.asarray(points, dtype=np.float64)
    q = np.asarray(queries, dtype=np.float64)
    assert np.array_equal(_nearest_map(p, q), _brute_force(p, q))


def test_empty_queries_return_no_indices() -> None:
    assert len(_nearest_map(_cloud(10), np.zeros((0, 3)))) == 0
