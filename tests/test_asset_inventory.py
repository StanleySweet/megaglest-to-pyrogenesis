"""Asset inventory and reference validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from megaglest_to_0ad.core.errors import AssetReferenceError
from megaglest_to_0ad.megaglest.asset_inventory import build_inventory
from megaglest_to_0ad.megaglest.civ_loader import load_faction
from megaglest_to_0ad.megaglest.parser import discover_pack


def _inventory_for(layout_b_pack: Path):
    pack = discover_pack(layout_b_pack)
    pack.factions["elves"] = load_faction(pack, pack.factions_dir / "elves")
    return build_inventory(pack)


def test_inventory_counts(layout_b_pack: Path) -> None:
    inventory = _inventory_for(layout_b_pack)
    assert len(inventory.meshes) == 3  # elf_walk, elf_stand, barracks
    assert len(inventory.textures) == 5  # elf.bmp, barracks.bmp, weaponry.bmp, gold.bmp, loading
    assert len(inventory.sounds) == 2  # ack1.wav, shared_attack.wav
    assert len(inventory.music) == 1  # theme.ogg
    assert len(inventory.maps) == 0
    assert inventory.referenced  # every XML-referenced asset resolved
    assert inventory.unresolved == []


def test_broken_reference_raises(broken_pack: Path) -> None:
    pack = discover_pack(broken_pack)
    pack.factions["ghosts"] = load_faction(pack, pack.factions_dir / "ghosts")
    with pytest.raises(AssetReferenceError, match="broken asset reference"):
        build_inventory(pack)


def test_broken_reference_details(broken_pack: Path) -> None:
    pack = discover_pack(broken_pack)
    pack.factions["ghosts"] = load_faction(pack, pack.factions_dir / "ghosts")
    try:
        build_inventory(pack)
    except AssetReferenceError as exc:
        message = str(exc)
        assert "missing.g3d" in message
        assert "woosh.wav" in message
        assert "factions/ghosts/units/ghost" in message
    else:
        pytest.fail("expected AssetReferenceError")
