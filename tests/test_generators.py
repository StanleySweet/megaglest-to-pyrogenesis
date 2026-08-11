"""Phase 4 generator tests: civ JSON, actors, templates, techs.

Synthetic factions/stats exercise the generators without depending on
fixture G3Ds (the layout_b fixture models are stubs that fail conversion).
"""

from __future__ import annotations

import json
from pathlib import Path

from lxml import etree

from megaglest_to_0ad.converters.mesh_converter import ConvertedMesh
from megaglest_to_0ad.core.config import Settings
from megaglest_to_0ad.core.media_conversion import MediaConversionStats
from megaglest_to_0ad.megaglest.civ_loader import (
    AttackStats,
    CommandDef,
    Faction,
    SkillDef,
    UnitDef,
    UpgradeDef,
)
from megaglest_to_0ad.oad.actor_generator import generate_actors
from megaglest_to_0ad.oad.civ_generator import generate_civ
from megaglest_to_0ad.oad.tech_generator import generate_techs
from megaglest_to_0ad.oad.template_generator import generate_templates

G3D_FIXTURES = Path(__file__).parent / "fixtures" / "g3d"


def _unit(
    name: str,
    *,
    is_building: bool = False,
    skills: dict[str, SkillDef] | None = None,
    commands: list[CommandDef] | None = None,
    parameters: dict[str, object] | None = None,
) -> UnitDef:
    return UnitDef(
        name=name,
        directory=Path(f"/packs/{name}"),
        xml_path=Path(f"/packs/{name}/{name}.xml"),
        is_building=is_building,
        parameters=parameters or {},
        skills=skills or {},
        commands=commands or [],
    )


def _stats_with_model(g3d: Path, tmp_path: Path) -> tuple[MediaConversionStats, Path, Path]:
    """MediaConversionStats with one converted model + baseTex entry."""
    dae = tmp_path / "art" / "meshes" / "elves" / f"{g3d.stem}.dae"
    png = tmp_path / "art" / "textures" / "units" / "elves" / "skin.png"
    stats = MediaConversionStats(
        models={g3d: ConvertedMesh(g3d_path=g3d, mesh_daes=[dae])},
        model_texture={g3d: png},
        music_files=["theme.ogg"],
    )
    return stats, dae, png


def _faction_with(tmp_path: Path, units: dict[str, UnitDef], name: str = "elves") -> Faction:
    return Faction(name=name, directory=tmp_path, xml_path=tmp_path / f"{name}.xml", units=units)


# ---------------------------------------------------------------------------
# Civ JSON
# ---------------------------------------------------------------------------


def test_civ_generator_shape(layout_b_pack: Path, tmp_path: Path) -> None:
    from megaglest_to_0ad.core.converter import _load_factions
    from megaglest_to_0ad.megaglest.parser import discover_pack

    pack = discover_pack(layout_b_pack)
    _load_factions(pack, ("all",))
    faction = pack.factions["elves"]
    stats = MediaConversionStats(music_files=["theme.ogg"])

    civ_path, _ = generate_civ(faction, tmp_path, stats, Settings())
    payload = json.loads(civ_path.read_text(encoding="utf-8"))

    assert list(payload) == [
        "Code",
        "Culture",
        "Music",
        "CivBonuses",
        "WallSets",
        "StartEntities",
        "AINames",
        "SkirmishReplacements",
        "SelectableInGameSetup",
    ]
    assert payload["Code"] == "elves"
    assert payload["Music"] == [{"File": "theme.ogg", "Type": "peace"}]
    assert payload["WallSets"] == ["structures/wallset_palisade"]
    # barracks is in starting_units (count 1) and is a building -> TC entry
    assert payload["StartEntities"] == [
        {"Template": "structures/elves/barracks"},
        {"Template": "units/elves/elf", "Count": 3},
    ]
    assert payload["SelectableInGameSetup"] is True


def test_civ_generator_inserts_town_centre_when_starting_units_have_none(
    tmp_path: Path,
) -> None:
    tc = _unit("tree_of_life", is_building=True, parameters={"size": 2})
    faction = Faction(
        name="elves",
        directory=tmp_path,
        xml_path=tmp_path / "elves.xml",
        starting_units=[("elf", 2)],
        units={"elf": _unit("elf"), "tree_of_life": tc},
    )
    civ_path, _ = generate_civ(faction, tmp_path, MediaConversionStats(), Settings())
    payload = json.loads(civ_path.read_text(encoding="utf-8"))
    assert payload["StartEntities"][0] == {"Template": "structures/elves/tree_of_life"}


def test_civ_generator_writes_player_template(tmp_path: Path) -> None:
    faction = Faction(
        name="elves",
        directory=tmp_path,
        xml_path=tmp_path / "elves.xml",
        starting_units=[],
        units={},
    )
    _, player_path = generate_civ(faction, tmp_path, MediaConversionStats(), Settings())
    root = etree.parse(player_path).getroot()
    assert root.tag == "Entity"
    assert root.get("parent") == "template_player"
    assert root.find("Identity/Civ").text == "elves"
    assert root.find("Identity/GenericName").text == "Elves"
    assert root.find("Identity/Icon").text == "emblems/emblem_elves.png"
    emblem = tmp_path / "art/textures/ui/session/portraits/emblems/emblem_elves.png"
    assert emblem.is_file() and emblem.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------


def test_actor_generator_writes_actor_referencing_converted_mesh(tmp_path: Path) -> None:
    g3d = G3D_FIXTURES / "gold.g3d"
    elf = _unit(
        "elf",
        skills={"stop": SkillDef(type="stop", name="stop_skill", animation=g3d)},
    )
    faction = _faction_with(tmp_path, {"elf": elf})
    stats, dae, png = _stats_with_model(g3d, tmp_path)

    written = generate_actors(faction, tmp_path, stats, Settings(target_version="0.29.0"))

    assert [p.name for p in written] == ["elf.xml"]
    actor_path = tmp_path / "art/actors/units/elves/elf.xml"
    root = etree.parse(actor_path).getroot()
    assert root.tag == "actor" and root.get("version") == "1"
    mesh = root.find("group/variant/mesh")
    assert mesh is not None and mesh.text == f"elves/{dae.name}"
    texture = root.find("group/variant/textures/texture")
    assert texture is not None and texture.get("file") == f"units/elves/{png.name}"
    assert texture.get("name") == "baseTex"
    slots = {t.get("name") for t in root.findall("group/variant/textures/texture")}
    assert slots == {"baseTex", "normTex", "specTex"}
    norm = root.find('group/variant/textures/texture[@name="normTex"]')
    assert norm is not None and norm.get("file") == "default_norm.png"
    spec = root.find('group/variant/textures/texture[@name="specTex"]')
    assert spec is not None and spec.get("file") == "null_black.dds"
    assert root.find("material").text == "basic_trans_norm_spec.xml"


def test_actor_material_version_aware(tmp_path: Path) -> None:
    g3d = G3D_FIXTURES / "gold.g3d"
    elf = _unit("elf", skills={"stop": SkillDef(type="stop", name="s", animation=g3d)})
    faction = _faction_with(tmp_path, {"elf": elf})
    stats, _dae, _png = _stats_with_model(g3d, tmp_path)
    generate_actors(faction, tmp_path, stats, Settings(target_version="0.28.0"))
    root = etree.parse(tmp_path / "art/actors/units/elves/elf.xml").getroot()
    assert root.find("material").text == "player_trans_norm_spec.xml"


def test_actor_multi_mesh_emits_props_at_root(tmp_path: Path) -> None:
    g3d = G3D_FIXTURES / "gold.g3d"
    extra = tmp_path / "art" / "meshes" / "elves" / "gold_1.dae"
    png = tmp_path / "art" / "textures" / "units" / "elves" / "skin.png"
    elf = _unit("elf", skills={"stop": SkillDef(type="stop", name="s", animation=g3d)})
    faction = _faction_with(tmp_path, {"elf": elf})
    stats = MediaConversionStats(
        models={
            g3d: ConvertedMesh(
                g3d_path=g3d, mesh_daes=[tmp_path / "art/meshes/elves/gold.dae", extra]
            )
        },
        model_texture={g3d: png},
    )

    written = generate_actors(faction, tmp_path, stats, Settings())

    root = etree.parse(tmp_path / "art/actors/units/elves/elf.xml").getroot()
    # engine actor grammar (0.28 actor.rng) nests <props> inside the base
    # <variant>; actor-level props fail CXeromyces validation
    prop = root.find("group/variant/props/prop")
    assert prop is not None and prop.get("attachpoint") == "root"
    assert prop.get("actor") == "props/elves/gold_1.xml"
    prop_actor = tmp_path / "art/actors/props/elves/gold_1.xml"
    assert prop_actor in written
    prop_root = etree.parse(prop_actor).getroot()
    assert prop_root.find("group/variant/mesh").text == "elves/gold_1.dae"
    assert (
        prop_root.find("group/variant/textures/texture").get("file")
        == "units/elves/skin.png"
    )


def test_actor_transparent_model_uses_transparent_material(tmp_path: Path) -> None:
    g3d = G3D_FIXTURES / "gold.g3d"
    elf = _unit("elf", skills={"stop": SkillDef(type="stop", name="s", animation=g3d)})
    faction = _faction_with(tmp_path, {"elf": elf})
    stats, _dae, _png = _stats_with_model(g3d, tmp_path)
    stats.transparent_models.add(g3d)

    generate_actors(faction, tmp_path, stats, Settings(target_version="0.28.0"))
    root = etree.parse(tmp_path / "art/actors/units/elves/elf.xml").getroot()
    assert root.find("material").text == "basic_trans_norm_spec.xml"


def test_actor_generator_skips_unit_without_model(tmp_path: Path) -> None:
    missing = G3D_FIXTURES / "nope.g3d"
    elf = _unit("elf", skills={"stop": SkillDef(type="stop", name="s", animation=missing)})
    faction = _faction_with(tmp_path, {"elf": elf})
    stats = MediaConversionStats()
    written = generate_actors(faction, tmp_path, stats, Settings())
    assert written == []
    assert not (tmp_path / "art/actors").exists()
    assert any("no converted model" in w for w in stats.warnings)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def _template_parent(tmp_path: Path, unit: UnitDef) -> str:
    tc = _unit("tree_of_life", is_building=True)
    units = {"tree_of_life": tc, unit.name: unit}
    faction = _faction_with(tmp_path, units)
    generate_templates(faction, tmp_path, MediaConversionStats(), Settings())
    sub = "structures" if unit.is_building else "units"
    root = etree.parse(
        tmp_path / "simulation/templates" / sub / "elves" / f"{unit.name}.xml"
    ).getroot()
    return root.get("parent")


def test_template_parent_selection(tmp_path: Path) -> None:
    tc = _unit("tree_of_life", is_building=True)
    barracks = _unit("barracks", is_building=True, commands=[CommandDef(type="produce", name="p")])
    forge = _unit("forge", is_building=True, commands=[CommandDef(type="upgrade", name="u")])
    granary = _unit("granary", is_building=True)
    assert _template_parent(tmp_path, tc) == "template_structure_civic_civil_centre"
    assert _template_parent(tmp_path, barracks) == "template_structure_military_barracks"
    assert _template_parent(tmp_path, forge) == "template_structure_economic"
    assert _template_parent(tmp_path, granary) == "template_structure"

    fast = _unit("reaver", skills={"move": SkillDef(type="move", name="m", speed=500)})
    archer = _unit(
        "archer",
        skills={
            "attack": SkillDef(
                type="attack",
                name="a",
                attack=AttackStats(strength=10, range=12, attack_type="pierce"),
            )
        },
    )
    swordsman = _unit(
        "swordsman",
        skills={
            "attack": SkillDef(
                type="attack",
                name="a",
                attack=AttackStats(strength=10, range=1, attack_type="pierce"),
            )
        },
    )
    peasant = _unit("peasant")
    assert _template_parent(tmp_path, fast) == "template_unit_cavalry_melee"
    assert _template_parent(tmp_path, archer) == "template_unit_infantry_ranged"
    assert _template_parent(tmp_path, swordsman) == "template_unit_infantry_melee"
    assert _template_parent(tmp_path, peasant) == "template_unit_support"


def test_template_unit_stats_scaled(tmp_path: Path) -> None:
    elf = _unit(
        "elf",
        parameters={
            "max_hp": 600,
            "armor": 15,
            "size": 1,
            "height": 2.0,
            "sight": 18,
            "resource_requirements": {"gold": 100, "grace": 50},
        },
        skills={
            "move": SkillDef(type="move", name="m", speed=200),
            "attack": SkillDef(
                type="attack",
                name="a",
                attack=AttackStats(strength=30, range=1, attack_type="pierce"),
            ),
        },
    )
    faction = _faction_with(tmp_path, {"elf": elf})
    generate_templates(faction, tmp_path, MediaConversionStats(), Settings())
    root = etree.parse(tmp_path / "simulation/templates/units/elves/elf.xml").getroot()

    assert root.find("Health/Max").text == "60"  # 600 / 10
    assert root.find("Health/RegenRate").text == "0"
    assert root.find("Health/IdleRegenRate").text == "0"
    assert root.find("Health/DeathType").text == "corpse"
    assert [d.text for d in root.findall("Resistance/Entity/Damage/*")] == [
        "1.5",
        "1.5",
        "1.5",
    ]
    damage = root.find("Attack/Melee/Damage")
    assert damage is not None and damage.find("Pierce").text == "3.0"
    assert root.find("Attack/Melee/AttackName").text == "Melee"
    assert root.find("Attack/Melee/MaxRange").text == "4.0"  # 1 tile * 4 m
    assert int(root.find("Attack/Melee/RepeatTime").text) >= 500
    assert root.find("UnitMotion/WalkSpeed").text == "6.7"  # 200 / 30
    assert root.find("UnitMotion/FormationController").text == "false"
    assert root.find("UnitMotion/PassabilityClass").text == "default"
    assert root.find("Vision/Range").text == "72"  # 18 tiles * 4 m
    assert root.find("Identity/Undeletable").text == "false"
    assert root.find("Cost/Population").text == "1"
    assert root.find("VisualActor/SilhouetteDisplay").text == "false"
    # grace dropped, gold -> metal
    resources = root.find("Cost/Resources")
    assert resources.find("metal").text == "100"
    assert resources.find("gold") is None
    assert resources.find("grace") is None


def test_template_non_morph_unit_promotes_to_self(tmp_path: Path) -> None:
    elf = _unit("elf", parameters={"max_hp": 100})
    faction = _faction_with(tmp_path, {"elf": elf})
    generate_templates(faction, tmp_path, MediaConversionStats(), Settings())
    root = etree.parse(tmp_path / "simulation/templates/units/elves/elf.xml").getroot()
    assert root.find("Promotion/Entity").text == "units/elves/elf"
    assert root.find("Promotion/RequiredXp").text == "1000000"


def test_template_morph_unit_promotes_to_target(tmp_path: Path) -> None:
    elf = _unit(
        "elf",
        parameters={"max_hp": 100},
        commands=[CommandDef(type="morph", name="m", morph_unit="dryad")],
    )
    faction = _faction_with(tmp_path, {"elf": elf})
    generate_templates(faction, tmp_path, MediaConversionStats(), Settings())
    root = etree.parse(tmp_path / "simulation/templates/units/elves/elf.xml").getroot()
    assert root.find("Promotion/Entity").text == "units/elves/dryad"
    assert root.find("Promotion/RequiredXp").text == "100"


def test_template_building_components(tmp_path: Path) -> None:
    barracks = _unit(
        "barracks",
        is_building=True,
        parameters={
            "time": 50,
            "size": 4,
            "height": 6.0,
            "max_hp": 3000,
            "resource_requirements": {"wood": 300, "gold": 200},
        },
        commands=[
            CommandDef(type="produce", name="train", produced_unit="elf"),
            CommandDef(type="upgrade", name="weaponry"),
            # display name differs from the canonical upgrade id
            CommandDef(type="upgrade", name="gather_wisdom", produced_upgrade="wisdom"),
        ],
    )
    faction = _faction_with(tmp_path, {"barracks": barracks})
    generate_templates(faction, tmp_path, MediaConversionStats(), Settings())
    root = etree.parse(tmp_path / "simulation/templates/structures/elves/barracks.xml").getroot()

    assert root.find("Cost/BuildTime").text == "5"  # 50 / 10
    assert root.find("Cost/Population").text == "0"
    footprint = root.find("Footprint/Square")
    assert footprint.get("width") == "16.0" and footprint.get("depth") == "16.0"
    obstruction = root.find("Obstruction/Static")
    assert obstruction.get("width") == "16.0"
    assert root.find("Obstruction/Active").text == "true"
    # <produced-upgrade> wins over the display <name>
    assert root.find("Researcher/Technologies").text == "elves/weaponry\nelves/wisdom"
    assert root.find("Trainer/Entities").text == "units/elves/elf"
    assert root.find("VisualActor/Actor").text == "structures/elves/barracks.xml"
    assert root.find("VisualActor/SilhouetteDisplay").text == "true"


def test_template_sound_component(tmp_path: Path) -> None:
    """SoundGroup files are wired to the engine's query keys, not to
    MegaGlest skill names: select from selection-sounds, engine animation
    names (gather_*, death, attack_melee) from skill sounds."""
    elf = _unit(
        "elf",
        skills={
            "harvest": SkillDef(
                type="harvest",
                name="harvest",
                sounds=[Path("worker_mining1.wav"), Path("worker_mining2.wav")],
            ),
            "die": SkillDef(type="die", name="die", sounds=[Path("worker_die1.wav")]),
            "attack": SkillDef(
                type="attack",
                name="attack",
                sounds=[Path("archer_attack1.wav")],
                attack=AttackStats(range=1.0),
            ),
        },
    )
    elf.selection_sounds = [Path("worker_select1.wav")]
    faction = _faction_with(tmp_path, {"elf": elf})
    generate_templates(faction, tmp_path, MediaConversionStats(), Settings())
    root = etree.parse(tmp_path / "simulation/templates/units/elves/elf.xml").getroot()

    groups = root.find("Sound/SoundGroups")
    assert groups.find("select").text == "groups/elf_select.xml"
    assert groups.find("death").text == "groups/elf_die.xml"
    assert groups.find("gather_food").text == "groups/elf_harvest.xml"
    assert groups.find("gather_wood").text == "groups/elf_harvest.xml"
    assert groups.find("attack_melee").text == "groups/elf_attack.xml"
    assert groups.find("attack_ranged") is None  # melee per AttackStats(range=1)
    # components stay in engine registration (alphabetical) order
    tags = [el.tag for el in root]
    assert tags == sorted(tags)
def test_template_footprint_from_model_bbox(tmp_path: Path) -> None:
    """Footprint/Obstruction derive from the base model's measured bbox."""
    g3d = G3D_FIXTURES / "gold.g3d"
    dae = tmp_path / "art/meshes/elves/gold.dae"
    hall = _unit(
        "hall",
        is_building=True,
        skills={"stop": SkillDef(type="stop", name="s", animation=g3d)},
        parameters={"size": 8, "height": 9.0},
    )
    faction = _faction_with(tmp_path, {"hall": hall})
    stats = MediaConversionStats(
        models={
            g3d: ConvertedMesh(
                g3d_path=g3d, mesh_daes=[dae], footprint=(3.5, 2.25, 4.0)
            )
        }
    )
    generate_templates(faction, tmp_path, stats, Settings())
    root = etree.parse(tmp_path / "simulation/templates/structures/elves/hall.xml").getroot()
    square = root.find("Footprint/Square")
    assert square.get("width") == "3.5" and square.get("depth") == "2.2"
    assert root.find("Footprint/Height").text == "4.0"
    static = root.find("Obstruction/Static")
    assert static.get("width") == "3.5" and static.get("depth") == "2.2"


# ---------------------------------------------------------------------------
# Techs
# ---------------------------------------------------------------------------


def test_tech_generator_shape(tmp_path: Path) -> None:
    upgrade = UpgradeDef(
        name="weaponry",
        directory=tmp_path,
        xml_path=tmp_path / "weaponry.xml",
        time=250,
        image=tmp_path / "weaponry.png",
        unit_requirements=["elf"],
        upgrade_requirements=["iron_working"],
        resource_requirements={"gold": 200, "wood": 100},
        effects=["elf", "ghost"],  # ghost is not a unit -> filtered from affects
        stats={
            "max_hp": {"value": 100, "start_percentage": 100},
            "armor": 5,
            "attack_strength": 10,
        },
    )
    faction = Faction(
        name="elves",
        directory=tmp_path,
        xml_path=tmp_path / "elves.xml",
        units={"elf": _unit("elf")},
        upgrades={"weaponry": upgrade},
    )
    written = generate_techs(faction, tmp_path, MediaConversionStats(), Settings())
    assert [p.name for p in written] == ["weaponry.json"]

    payload = json.loads(
        (tmp_path / "simulation/data/technologies/elves/weaponry.json").read_text(encoding="utf-8")
    )
    assert list(payload)[:7] == [
        "genericName",
        "description",
        "cost",
        "requirements",
        "requirementsTooltip",
        "icon",
        "researchTime",
    ]
    assert payload["genericName"] == "Weaponry"
    assert payload["cost"] == {"metal": 200, "wood": 100}
    assert payload["requirements"] == {
        "tech": "elves/iron_working",
        "entities": ["units/elves/elf"],
    }
    assert payload["icon"] == "technologies/weaponry.png"
    assert payload["researchTime"] == 25  # 250 / 10
    mods = {m["value"]: m for m in payload["modifications"]}
    assert mods["Health/Max"]["multiply"] == 2.0  # (100 + 100) / 100
    assert mods["Resistance/Entity/Damage/Hack"]["add"] == 5.0
    assert mods["Attack/Melee/Damage/Hack"]["add"] == 1.0  # 10 / 10
    assert payload["affects"] == ["units/elves/elf"]


def test_tech_generator_health_multiply_uses_start_percentage(tmp_path: Path) -> None:
    upgrade = UpgradeDef(
        name="training",
        directory=tmp_path,
        xml_path=tmp_path / "training.xml",
        stats={"max_hp": {"value": 100, "start_percentage": 50}},
    )
    faction = Faction(
        name="elves",
        directory=tmp_path,
        xml_path=tmp_path / "elves.xml",
        upgrades={"training": upgrade},
    )
    generate_techs(faction, tmp_path, MediaConversionStats(), Settings())
    payload = json.loads(
        (tmp_path / "simulation/data/technologies/elves/training.json").read_text(encoding="utf-8")
    )
    # +100% of the 50%-of-base starting HP => back to base max (factor 1.0)
    assert payload["modifications"][0] == {"value": "Health/Max", "multiply": 1.0}


def test_actor_wires_animation_block(tmp_path: Path) -> None:
    """Rigged units get an <animations> block: name, file, speed, event."""
    g3d = G3D_FIXTURES / "gold.g3d"
    stats, _dae, _png = _stats_with_model(g3d, tmp_path)
    anim_dir = tmp_path / "art" / "animation" / "elves"
    anim_dir.mkdir(parents=True)
    idle = anim_dir / "gold_stop.dae"
    attack = anim_dir / "gold_attack.dae"
    idle.write_text("<COLLADA/>")
    attack.write_text("<COLLADA/>")
    stats.animations[g3d] = {"idle": idle, "attack_melee": attack}
    elf = _unit(
        "elf",
        skills={
            "stop": SkillDef(type="stop", name="stop_skill", animation=g3d),
            "attack": SkillDef(
                type="attack",
                name="attack_skill",
                animation=g3d,
                attack=AttackStats(strength=5.0, range=2.0, start_time=0.5),
            ),
        },
    )
    faction = _faction_with(tmp_path, {"elf": elf})
    written = generate_actors(faction, tmp_path, stats, Settings(target_version="0.29.0"))
    root = etree.parse(written[0]).getroot()
    animations = root.xpath("//variant/animations/animation")
    assert [(a.get("name"), a.get("file"), a.get("speed"), a.get("event")) for a in animations] == [
        ("attack_melee", "elves/gold_attack.dae", "100", "0.5"),
        ("idle", "elves/gold_stop.dae", "100", None),
    ]


def test_actor_omits_animations_for_static_units(tmp_path: Path) -> None:
    g3d = G3D_FIXTURES / "gold.g3d"
    stats, _dae, _png = _stats_with_model(g3d, tmp_path)
    elf = _unit("elf", skills={"stop": SkillDef(type="stop", name="stop_skill", animation=g3d)})
    faction = _faction_with(tmp_path, {"elf": elf})
    written = generate_actors(faction, tmp_path, stats, Settings(target_version="0.29.0"))
    root = etree.parse(written[0]).getroot()
    assert root.xpath("//animations") == []


def _building_models(tmp_path: Path) -> tuple[MediaConversionStats, Path, Path, Path]:
    """Stats with base/cons/des models; cons has 4 stage DAEs."""
    base = G3D_FIXTURES / "gold.g3d"
    cons = G3D_FIXTURES / "house_cons.g3d"
    des = G3D_FIXTURES / "house_des.g3d"
    png = tmp_path / "art" / "textures" / "units" / "elves" / "skin.png"
    stats = MediaConversionStats(
        models={
            base: ConvertedMesh(g3d_path=base, mesh_daes=[tmp_path / "art/meshes/elves/house.dae"]),
            cons: ConvertedMesh(
                g3d_path=cons,
                mesh_daes=[
                    tmp_path / "art/meshes/elves/house_cons_0.dae",
                    tmp_path / "art/meshes/elves/house_cons_1.dae",
                    tmp_path / "art/meshes/elves/house_cons_2.dae",
                    tmp_path / "art/meshes/elves/house_cons_3.dae",
                ],
            ),
            des: ConvertedMesh(
                g3d_path=des, mesh_daes=[tmp_path / "art/meshes/elves/house_des.dae"]
            ),
        },
        model_texture={base: png, cons: png, des: png},
    )
    return stats, base, cons, des


def _building_unit(base: Path, cons: Path, des: Path) -> UnitDef:
    return _unit(
        "house",
        is_building=True,
        skills={
            "stop": SkillDef(type="stop", name="s", animation=base),
            "be_built": SkillDef(type="be_built", name="b", animation=cons),
            "die": SkillDef(type="die", name="d", animation=des),
        },
    )


def test_actor_building_health_group_maps_damage_variants(tmp_path: Path) -> None:
    """Buildings get alive/light/medium/heavy variants (Base mesh persists)
    plus a named death variant swapping in the destroyed model."""
    stats, base, cons, des = _building_models(tmp_path)
    faction = _faction_with(tmp_path, {"house": _building_unit(base, cons, des)})
    written = generate_actors(faction, tmp_path, stats, Settings())
    root = etree.parse(next(p for p in written if p.name == "house.xml")).getroot()

    group = root.xpath("group")[-1]  # health group follows the Base group
    names = [v.get("name") for v in group.xpath("variant")]
    assert names == ["alive", "lightdamage", "mediumdamage", "heavydamage", "death"]
    assert group.xpath("variant[@name='alive']")[0].get("frequency") == "1"
    assert group.xpath("variant[@name='lightdamage']/mesh") == []
    death_mesh = group.xpath("variant[@name='death']/mesh")[0].text
    assert death_mesh.endswith("house_des.dae")


def test_building_emits_foundation_actor_with_stage_variants(tmp_path: Path) -> None:
    """fndn_ actor maps construction stages to health selections:
    alive -> newest stage, heavy damage -> earliest, death -> destroyed."""
    stats, base, cons, des = _building_models(tmp_path)
    faction = _faction_with(tmp_path, {"house": _building_unit(base, cons, des)})
    generate_actors(faction, tmp_path, stats, Settings())
    fndn = tmp_path / "art/actors/structures/elves/fndn_house.xml"
    root = etree.parse(fndn).getroot()

    group = root.xpath("group")[0]
    by_name = {v.get("name"): v for v in group.xpath("variant")}
    assert [v.get("name") for v in group.xpath("variant")] == [
        "alive",
        "lightdamage",
        "mediumdamage",
        "heavydamage",
        "death",
    ]
    assert by_name["alive"].get("frequency") == "1"
    assert by_name["alive"].xpath("mesh")[0].text.endswith("house_cons_3.dae")
    assert by_name["lightdamage"].xpath("mesh")[0].text.endswith("house_cons_2.dae")
    assert by_name["mediumdamage"].xpath("mesh")[0].text.endswith("house_cons_1.dae")
    assert by_name["heavydamage"].xpath("mesh")[0].text.endswith("house_cons_0.dae")
    assert by_name["death"].xpath("mesh")[0].text.endswith("house_des.dae")
    anim = root.xpath("group")[1]
    assert [v.get("name") for v in anim.xpath("variant")] == ["Idle", "scaffold"]
    assert anim.xpath("variant[@name='Idle']")[0].get("frequency") == "1"


def test_foundation_actor_single_stage_clamps_all_to_zero(tmp_path: Path) -> None:
    """A single construction mesh (no stages) is used for every health state."""
    base = G3D_FIXTURES / "gold.g3d"
    cons = G3D_FIXTURES / "house_cons.g3d"
    dae = tmp_path / "art/meshes/elves/house_cons_0.dae"
    png = tmp_path / "art/textures/units/elves/skin.png"
    stats = MediaConversionStats(
        models={
            base: ConvertedMesh(g3d_path=base, mesh_daes=[dae]),
            cons: ConvertedMesh(g3d_path=cons, mesh_daes=[dae]),
        },
        model_texture={base: png, cons: png},
    )
    unit = _unit(
        "house",
        is_building=True,
        skills={
            "stop": SkillDef(type="stop", name="s", animation=base),
            "be_built": SkillDef(type="be_built", name="b", animation=cons),
        },
    )
    faction = _faction_with(tmp_path, {"house": unit})
    generate_actors(faction, tmp_path, stats, Settings())
    root = etree.parse(tmp_path / "art/actors/structures/elves/fndn_house.xml").getroot()
    group = root.xpath("group")[0]
    for variant in group.xpath("variant[@name!='death']"):
        assert variant.xpath("mesh")[0].text.endswith("house_cons_0.dae")


def test_foundation_actor_five_stages_starts_at_earliest(tmp_path: Path) -> None:
    """With more stages than health selections the foundation still starts
    at the earliest stage: heavydamage (the placement-time selection,
    Foundation.js begins at 1 HP) maps to stage 0, alive to the newest."""
    base = G3D_FIXTURES / "gold.g3d"
    cons = G3D_FIXTURES / "house_cons.g3d"
    png = tmp_path / "art/textures/units/elves/skin.png"
    stats = MediaConversionStats(
        models={
            base: ConvertedMesh(g3d_path=base, mesh_daes=[tmp_path / "art/meshes/elves/house.dae"]),
            cons: ConvertedMesh(
                g3d_path=cons,
                mesh_daes=[
                    tmp_path / "art/meshes/elves/house_cons_0.dae",
                    tmp_path / "art/meshes/elves/house_cons_1.dae",
                    tmp_path / "art/meshes/elves/house_cons_2.dae",
                    tmp_path / "art/meshes/elves/house_cons_3.dae",
                    tmp_path / "art/meshes/elves/house_cons_4.dae",
                ],
            ),
        },
        model_texture={base: png, cons: png},
    )
    faction = _faction_with(
        tmp_path,
        {"house": _unit(
            "house",
            is_building=True,
            skills={
                "stop": SkillDef(type="stop", name="s", animation=base),
                "be_built": SkillDef(type="be_built", name="b", animation=cons),
            },
        )},
    )
    generate_actors(faction, tmp_path, stats, Settings())
    root = etree.parse(tmp_path / "art/actors/structures/elves/fndn_house.xml").getroot()
    by_name = {v.get("name"): v for v in root.xpath("group")[0].xpath("variant")}
    assert by_name["heavydamage"].xpath("mesh")[0].text.endswith("house_cons_0.dae")
    assert by_name["mediumdamage"].xpath("mesh")[0].text.endswith("house_cons_2.dae")
    assert by_name["lightdamage"].xpath("mesh")[0].text.endswith("house_cons_3.dae")
    assert by_name["alive"].xpath("mesh")[0].text.endswith("house_cons_4.dae")


def test_prop_actor_uses_its_own_texture_group(tmp_path: Path) -> None:
    """Extra-mesh prop actors read mesh_textures (per-DAE), not the model's
    first texture."""
    g3d = G3D_FIXTURES / "gold.g3d"
    dae = tmp_path / "art/meshes/elves/gold.dae"
    extra = tmp_path / "art/meshes/elves/gold_1.dae"
    skin = tmp_path / "art/textures/units/elves/skin.png"
    roof = tmp_path / "art/textures/units/elves/roof.png"
    stats = MediaConversionStats(
        models={g3d: ConvertedMesh(g3d_path=g3d, mesh_daes=[dae, extra])},
        model_texture={g3d: skin},
        mesh_textures={extra: roof},
    )
    elf = _unit("elf", skills={"stop": SkillDef(type="stop", name="s", animation=g3d)})
    faction = _faction_with(tmp_path, {"elf": elf})
    generate_actors(faction, tmp_path, stats, Settings())
    prop = etree.parse(tmp_path / "art/actors/props/elves/gold_1.xml").getroot()
    assert prop.xpath("group/variant/textures/texture")[0].get("file").endswith("roof.png")


def test_template_building_references_foundation_actor(tmp_path: Path) -> None:
    base = G3D_FIXTURES / "gold.g3d"
    cons = G3D_FIXTURES / "house_cons.g3d"
    dae = tmp_path / "art/meshes/elves/house.dae"
    cons_dae = tmp_path / "art/meshes/elves/house_cons_0.dae"
    stats = MediaConversionStats(
        models={
            base: ConvertedMesh(g3d_path=base, mesh_daes=[dae]),
            cons: ConvertedMesh(g3d_path=cons, mesh_daes=[cons_dae]),
        }
    )
    house = _unit(
        "house",
        is_building=True,
        skills={
            "stop": SkillDef(type="stop", name="s", animation=base),
            "be_built": SkillDef(type="be_built", name="b", animation=cons),
        },
    )
    faction = _faction_with(tmp_path, {"house": house})
    generate_templates(faction, tmp_path, stats, Settings())
    root = etree.parse(tmp_path / "simulation/templates/structures/elves/house.xml").getroot()
    assert root.find("VisualActor/FoundationActor").text == "structures/elves/fndn_house.xml"


def test_template_building_without_cons_omits_foundation_actor(tmp_path: Path) -> None:
    """No be_built model -> no fndn_ actor file, so the template must not
    reference one (the engine falls back to the plain Actor)."""
    house = _unit("house", is_building=True)
    faction = _faction_with(tmp_path, {"house": house})
    generate_templates(faction, tmp_path, MediaConversionStats(), Settings())
    root = etree.parse(tmp_path / "simulation/templates/structures/elves/house.xml").getroot()
    assert root.find("VisualActor/FoundationActor") is None
