"""Asset inventory and reference validation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from megaglest_to_0ad.core.errors import AssetReferenceError
from megaglest_to_0ad.megaglest.asset_inventory import build_inventory
from megaglest_to_0ad.megaglest.civ_loader import load_faction
from megaglest_to_0ad.megaglest.parser import discover_pack


def _inventory_for(layout_b_pack: Path):
    pack = discover_pack(layout_b_pack)
    pack.factions["demo"] = load_faction(pack, pack.factions_dir / "demo")
    return build_inventory(pack)


def test_inventory_counts(layout_b_pack: Path) -> None:
    inventory = _inventory_for(layout_b_pack)
    assert len(inventory.meshes) == 3  # grunt_walk, grunt_stand, barracks
    assert len(inventory.textures) == 6  # grunt, barracks, weaponry, cancel, gold, loading
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


_DISABLED_UNIT = """<?xml version="1.0" standalone="no"?>
<unit>
	<parameters>
		<selection-sounds enabled="{selection}">
			<sound path="../nowhere/select.wav"/>
		</selection-sounds>
		<command-sounds enabled="false">
			<sound path="../nowhere/ack.wav"/>
		</command-sounds>
	</parameters>
	<skills>
		<skill>
			<type value="attack"/>
			<name value="attack_skill"/>
			<projectile value="{projectile}">
				<particle value="true" path="missing_proj.xml"/>
				<sound enabled="true">
					<sound-file path="sounds/missing_hit.wav"/>
				</sound>
			</projectile>
			<splash value="{splash}">
				<radius value="0"/>
				<damage-all value="true"/>
				<particle value="true" path="missing_splash.xml"/>
			</splash>
		</skill>
	</skills>
	<commands/>
</unit>
"""


def _pack_with_unit(root: Path, **flags: str) -> Path:
    faction = root / "factions" / "f"
    (faction / "units" / "u").mkdir(parents=True)
    (faction / "f.xml").write_text(
        '<faction><starting-units><unit name="u" amount="1"/></starting-units></faction>'
    )
    values = {"selection": "false", "projectile": "false", "splash": "false", **flags}
    (faction / "units" / "u" / "u.xml").write_text(_DISABLED_UNIT.format(**values))
    return root


@pytest.mark.parametrize(
    "flags",
    [{}, {"selection": "true"}, {"projectile": "true"}, {"splash": "true"}],
    ids=["all-off", "selection-on", "projectile-on", "splash-on"],
)
def test_disabled_nodes_are_not_references(tmp_path: Path, flags: dict[str, str]) -> None:
    """MegaGlest never opens anything under a node switched off, so neither do we.

    megapack ships such dead references (egypt's air_pyramid sounds, persian and
    roman splash particles); treating them as broken refused the whole pack.
    """
    pack = discover_pack(_pack_with_unit(tmp_path, **flags))
    pack.factions["f"] = load_faction(pack, pack.factions_dir / "f")
    if not flags:
        assert build_inventory(pack).unresolved == []
    else:
        with pytest.raises(AssetReferenceError):
            build_inventory(pack)


def test_upgrade_cancel_image_is_not_a_reference(layout_b_pack: Path, tmp_path: Path) -> None:
    # MegaGlest never loads an upgrade's image-cancel (upgrade_type.cpp), and
    # megapack's romans point theirs at a file the pack does not ship.
    pack_root = tmp_path / "pack"
    shutil.copytree(layout_b_pack, pack_root)
    (pack_root / "factions/demo/cancel.bmp").unlink()
    inventory = _inventory_for(pack_root)
    assert inventory.unresolved == []
