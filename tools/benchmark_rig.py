"""Measure the rig-synthesis hot path.

Two things are reported, because issue #2 needs both:

- ``nearest``: the displacement-transfer map between two models' rest poses.
  This is the one that was quadratic, so it is swept over point counts and run
  in a child process per size -- ``ru_maxrss`` is a high-water mark that never
  falls, so measuring several sizes in one process would just report the first.
- ``fixtures``: parse and rig-build time for the committed ``.g3d`` fixtures,
  which are real models rather than the synthetic cloud used above.

Run it directly::

    python tools/benchmark_rig.py            # table
    python tools/benchmark_rig.py --child N  # one size, for the parent to call
"""

from __future__ import annotations

import argparse
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from megaglest_to_0ad.converters.mesh_converter import clear_model_cache, read_g3d
from megaglest_to_0ad.converters.rig import _nearest_map

G3D_FIXTURES = Path(__file__).resolve().parent.parent / "tests/fixtures/g3d"
SIZES = (2_000, 4_000, 8_000, 16_000, 32_000)
REPEATS = 3


def _peak_rss_mb() -> float:
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def _cloud(n: int, seed: int = 0) -> np.ndarray:
    """Rest-pose-shaped point cloud on a rough surface, not a gaussian blob."""
    rng = np.random.default_rng(seed)
    side = int(np.ceil(np.sqrt(n)))
    grid = np.stack(
        np.meshgrid(np.linspace(0, 4, side), np.linspace(0, 4, side)), -1
    ).reshape(-1, 2)
    pts = np.zeros((n, 3))
    pts[:, :2] = grid[:n]
    pts[:, 2] = 0.3 * np.sin(pts[:, 0] * 2.0) * np.cos(pts[:, 1] * 1.7)
    pts += rng.normal(0, 0.01, pts.shape)
    return pts


def measure_nearest(n: int) -> tuple[float, float]:
    points = _cloud(n)
    queries = _cloud(n, seed=1)
    _nearest_map(points, queries[:16])  # warm up any lazy import cost
    best = min(_time(lambda: _nearest_map(points, queries)) for _ in range(REPEATS))
    return best, _peak_rss_mb()


def _time(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def child(n: int) -> None:
    seconds, peak = measure_nearest(n)
    print(f"{n},{seconds:.4f},{peak:.1f}")


def parent() -> None:
    print(f"nearest-neighbour displacement map, {REPEATS} repeats, best of\n")
    print(f"{'points':>8}  {'seconds':>9}  {'peak RSS':>10}")
    for n in SIZES:
        proc = subprocess.run(
            [sys.executable, __file__, "--child", str(n)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            # SIGKILL here is the OS refusing the n-by-n distance matrix, which
            # is the point being measured rather than a harness failure.
            how = "OOM-killed" if proc.returncode == -9 else f"exit {proc.returncode}"
            print(f"{n:>8}  {how:>9}  {'-':>10}")
            continue
        _, seconds, peak = proc.stdout.strip().split(",")
        print(f"{n:>8}  {float(seconds):>9.4f}  {float(peak):>7.1f} MB")

    print("\ncommitted fixtures (parse, then rig build)\n")
    print(f"{'fixture':<26} {'verts':>6} {'frames':>7} {'parse':>9} {'rig':>9}")
    for path in sorted(G3D_FIXTURES.glob("*.g3d")):
        from megaglest_to_0ad.converters.rig import build_rig

        # The parse cache would otherwise turn this into a cache-hit
        # measurement, which is not what the column claims to report.
        def _cold_parse(p=path):
            clear_model_cache()
            return read_g3d(p)

        parse_s = min(_time(_cold_parse) for _ in range(REPEATS))
        model = read_g3d(path)
        verts = sum(m.vertex_count for m in model.meshes)
        frames = max(m.frame_count for m in model.meshes)
        groups = [[i] for i in range(len(model.meshes))]
        rig_s = min(
            _time(lambda: build_rig(model, groups, 32, "root")) for _ in range(REPEATS)
        )
        print(f"{path.name:<26} {verts:>6} {frames:>7} {parse_s:>8.4f}s {rig_s:>8.4f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.environ.setdefault("PYTHONHASHSEED", "0")
    if args.child:
        child(args.child)
    else:
        parent()
