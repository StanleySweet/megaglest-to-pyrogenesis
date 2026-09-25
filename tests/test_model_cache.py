"""Parsed-model reuse.

A pack's base model is read once per animation that uses it, and the mesh pass
re-reads models the parent already parsed. Parsing is pure, so the result can be
kept -- as long as a rewritten file is not served from the cache.

The observable contract is object identity: parsing allocates, so the same
object back means no second parse. That keeps these tests free of spies.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from megaglest_to_0ad.converters import mesh_converter
from megaglest_to_0ad.converters.mesh_converter import read_g3d

G3D_FIXTURES = Path(__file__).resolve().parent / "fixtures/g3d"


@pytest.fixture(autouse=True)
def _clear_cache():
    """The cache is process-wide, so one test's entries must not leak."""
    mesh_converter._MODEL_CACHE_SIZE  # fail loudly if the cache is gone
    mesh_converter.clear_model_cache()
    yield
    mesh_converter.clear_model_cache()


def test_second_read_of_the_same_file_does_not_reparse(tmp_path: Path) -> None:
    target = tmp_path / "unit.g3d"
    shutil.copy(G3D_FIXTURES / "treant_idle.g3d", target)

    first = read_g3d(target)
    second = read_g3d(target)

    assert second is first


def test_a_rewritten_file_is_reparsed(tmp_path: Path) -> None:
    target = tmp_path / "unit.g3d"
    shutil.copy(G3D_FIXTURES / "treant_idle.g3d", target)
    first = read_g3d(target)

    shutil.copy(G3D_FIXTURES / "tower_destruction.g3d", target)
    second = read_g3d(target)

    assert second is not first
    assert second.meshes[0].vertex_count == 836


def test_a_touched_file_is_reparsed_even_when_size_matches(tmp_path: Path) -> None:
    """Rewriting the same bytes still changes mtime, and must invalidate."""
    target = tmp_path / "unit.g3d"
    shutil.copy(G3D_FIXTURES / "gold.g3d", target)
    first = read_g3d(target)
    before = target.stat().st_mtime_ns

    # Same length, different content, so only mtime can tell them apart.
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 0xFF
    target.write_bytes(bytes(raw))
    if target.stat().st_mtime_ns == before:  # coarse filesystem clock
        pytest.skip("filesystem mtime resolution too coarse to distinguish")
    _ = first

    assert read_g3d(target) is not first


def test_distinct_files_do_not_share_an_entry(tmp_path: Path) -> None:
    a, b = tmp_path / "a.g3d", tmp_path / "b.g3d"
    shutil.copy(G3D_FIXTURES / "gold.g3d", a)
    shutil.copy(G3D_FIXTURES / "treant_idle.g3d", b)

    assert read_g3d(a) is not read_g3d(b)


def test_the_cache_stays_within_its_size(tmp_path: Path) -> None:
    """Entries are evicted, so a long pack cannot grow memory without bound."""
    limit = mesh_converter._MODEL_CACHE_SIZE
    paths = []
    for i in range(limit + 2):
        target = tmp_path / f"m{i}.g3d"
        shutil.copy(G3D_FIXTURES / "gold.g3d", target)
        paths.append(target)

    kept = [read_g3d(p) for p in paths]
    assert read_g3d(paths[-1]) is kept[-1], "the newest entry should still be cached"

    # The oldest entries must have been dropped, so reading them again re-parses.
    evicted = read_g3d(paths[0])
    assert evicted is not kept[0], f"cache exceeded its {limit}-entry bound"
