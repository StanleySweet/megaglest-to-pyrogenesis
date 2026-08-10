"""Pack discovery and layout auto-detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from megaglest_to_0ad.core.errors import PackStructureError
from megaglest_to_0ad.megaglest.parser import LayoutKind, discover_pack


def test_discover_flat_layout(layout_b_pack: Path) -> None:
    pack = discover_pack(layout_b_pack)
    assert pack.layout is LayoutKind.FLAT
    assert pack.name == "layout_b"
    assert pack.factions_dir == layout_b_pack / "factions"
    assert pack.tech_xml == layout_b_pack / "megapack_test.xml"
    assert pack.commondata_dir == layout_b_pack / "commondata"
    assert pack.resources_dir == layout_b_pack / "resources"


def test_discover_classic_layout(layout_a_pack: Path) -> None:
    pack = discover_pack(layout_a_pack)
    assert pack.layout is LayoutKind.CLASSIC
    assert pack.name == "magitech"
    assert pack.tech_root == layout_a_pack / "techs" / "magitech"
    assert pack.factions_dir == layout_a_pack / "techs" / "magitech" / "factions"


def test_tech_tree_parsed(layout_b_pack: Path) -> None:
    pack = discover_pack(layout_b_pack)
    assert pack.tech_tree is not None
    assert "piercing" in pack.tech_tree.attack_types
    assert "leather" in pack.tech_tree.armor_types
    assert pack.tech_tree.damage_multipliers[("piercing", "leather")] == 1.25


def test_macro_map(layout_b_pack: Path) -> None:
    pack = discover_pack(layout_b_pack)
    macros = pack.macro_map()
    assert macros["COMMONDATAPATH"] == layout_b_pack / "commondata"
    assert macros["TECHSPATH"] == layout_b_pack


def test_unrecognized_layout_raises(tmp_path: Path) -> None:
    (tmp_path / "maps").mkdir()
    with pytest.raises(PackStructureError, match="Unrecognized pack layout"):
        discover_pack(tmp_path)


def test_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(PackStructureError, match="not a directory"):
        discover_pack(tmp_path / "nope")
