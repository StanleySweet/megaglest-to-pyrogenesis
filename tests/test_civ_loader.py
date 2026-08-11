"""Faction, unit and upgrade loading."""

from __future__ import annotations

from pathlib import Path

from megaglest_to_0ad.megaglest.civ_loader import (
    CommandDef,
    SkillDef,
    UnitDef,
    _classify_building,
    load_faction,
)
from megaglest_to_0ad.megaglest.parser import discover_pack


def _minimal_unit(name: str, skills=None, commands=None, parameters=None) -> UnitDef:
    return UnitDef(
        name=name,
        directory=Path(f"/packs/{name}"),
        xml_path=Path(f"/packs/{name}/{name}.xml"),
        parameters=parameters or {},
        skills=skills or {},
        commands=commands or [],
    )


def _load_elves(layout_b_pack: Path):
    pack = discover_pack(layout_b_pack)
    faction = load_faction(pack, pack.factions_dir / "elves")
    return pack, faction


def test_faction_metadata(layout_b_pack: Path) -> None:
    _, faction = _load_elves(layout_b_pack)
    assert faction.name == "elves"
    assert faction.starting_resources == {"gold": 500, "wood": 500}
    assert ("elf", 3) in faction.starting_units
    assert faction.ai_behavior["worker-units"] == {"elf": 10}
    assert faction.music is not None
    assert faction.music.name == "theme.ogg"
    assert faction.loading_screen is not None
    assert faction.loading_screen.suffix == ".png"


def test_faction_entities(layout_b_pack: Path) -> None:
    _, faction = _load_elves(layout_b_pack)
    assert set(faction.units) == {"elf", "barracks"}
    assert set(faction.upgrades) == {"weaponry"}


def test_unit_parameters(layout_b_pack: Path) -> None:
    _, faction = _load_elves(layout_b_pack)
    elf = faction.units["elf"]
    assert elf.is_building is False
    assert elf.parameters["max_hp"] == 600
    assert elf.parameters["armor_type"] == "leather"
    assert elf.parameters["resource_requirements"] == {"gold": 75, "wood": 0}
    assert elf.parameters["fields"] == ["land"]
    assert elf.image is not None
    assert elf.image.name == "elf.bmp"


def test_building_classification(layout_b_pack: Path) -> None:
    _, faction = _load_elves(layout_b_pack)
    barracks = faction.units["barracks"]
    assert barracks.is_building is True
    assert barracks.parameters["ai_build_size"] == 6
    assert barracks.parameters["properties"] == ["burnable"]


def test_mobile_producer_classified_as_unit() -> None:
    """MG summons are produce skills on field units (minstrel summons
    dryads/ents); a moving producer must not become a building."""
    summoner = _minimal_unit(
        "minstrel",
        skills={
            "move": SkillDef(type="move", name="move"),
            "produce": SkillDef(type="produce", name="summon_skill"),
        },
        commands=[CommandDef(type="produce", name="summon", produced_unit="dryad")],
    )
    assert _classify_building(summoner) is False

    # static producer with no move skill stays a building
    barracks = _minimal_unit(
        "barracks",
        skills={"produce": SkillDef(type="produce", name="produce_skill")},
        commands=[CommandDef(type="produce", name="train", produced_unit="elf")],
    )
    assert _classify_building(barracks) is True

    # structure markers beat mobility (MG forest_guardian is built by the
    # AI and burns despite having move/attack skills)
    guardian = _minimal_unit(
        "forest_guardian",
        skills={"move": SkillDef(type="move", name="move")},
        parameters={"ai_build_size": 5, "properties": ["burnable"]},
    )
    assert _classify_building(guardian) is True


def test_skill_parsing_and_macros(layout_b_pack: Path) -> None:
    _, faction = _load_elves(layout_b_pack)
    elf = faction.units["elf"]
    attack = elf.skills["attack_skill"]
    assert attack.type == "attack"
    assert attack.speed == 55
    assert attack.animation is not None
    assert attack.animation.name == "elf_stand.g3d"
    assert attack.attack is not None
    assert attack.attack.attack_type == "piercing"
    assert attack.attack.strength == 215
    assert attack.attack.range == 11
    assert attack.attack.projectile is True
    # $COMMONDATAPATH macro resolves to pack commondata/
    assert attack.sounds and attack.sounds[0].name == "shared_attack.wav"
    assert attack.sounds[0].parent.parent.name == "commondata"


def test_commands(layout_b_pack: Path) -> None:
    _, faction = _load_elves(layout_b_pack)
    barracks = faction.units["barracks"]
    produce = barracks.commands[0]
    assert produce.type == "produce"
    assert produce.produced_unit == "elf"
    assert produce.skill_refs == {"produce": "produce_skill"}


def test_upgrade(layout_b_pack: Path) -> None:
    _, faction = _load_elves(layout_b_pack)
    weaponry = faction.upgrades["weaponry"]
    assert weaponry.time == 250
    assert weaponry.resource_requirements == {"gold": 200, "wood": 100}
    assert weaponry.effects == ["elf"]
    assert weaponry.stats["armor"] == 5
    assert weaponry.stats["max_hp"]["start_percentage"] == 100


def test_classic_layout_loading(layout_a_pack: Path) -> None:
    pack = discover_pack(layout_a_pack)
    faction = load_faction(pack, pack.factions_dir / "romans")
    assert faction.name == "romans"
    assert set(faction.units) == {"legion"}
    legion = faction.units["legion"]
    assert legion.is_building is False
    assert legion.skills["attack_skill"].attack is not None
    assert legion.skills["attack_skill"].attack.attack_type == "slashing"
