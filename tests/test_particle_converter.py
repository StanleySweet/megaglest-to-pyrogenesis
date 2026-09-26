"""Particle conversion tests: MG particle XML parsing + 0 A.D. emission."""

from __future__ import annotations

from pathlib import Path

from lxml import etree
from PIL import Image

from megaglest_to_0ad.core.config import Settings
from megaglest_to_0ad.core.media_conversion import MediaConversionStats
from megaglest_to_0ad.megaglest.civ_loader import AttackStats, Faction, SkillDef, UnitDef
from megaglest_to_0ad.oad.particle_converter import (
    convert_particles,
    parse_particle,
    write_particle_system,
)

SPLASH_XML = """<?xml version="1.0" standalone="yes"?>
<splash-particle-system>
    <texture value="true" path="images/particle_splash.bmp" luminance="true"/>
    <model value="false"/>
    <primitive value="quad"/>
    <offset x="0" y="0" z="0"/>
    <color red="0.8" green="0.3" blue="0.0" alpha="0.3" />
    <color-no-energy red="0.0" green="0.0" blue="0.0" alpha="0.0" />
    <size value="0.8" />
    <size-no-energy value="0.8" />
    <speed value="7.0" />
    <gravity value="0.4"/>
    <emission-rate value="80" />
    <energy-max value="30" />
    <energy-var value="2" />
    <emission-rate-fade value="100"/>
    <vertical-spread a="1" b="0.5"/>
    <horizontal-spread a="1" b="0"/>
</splash-particle-system>
"""

UNIT_XML = """<?xml version="1.0" standalone="yes"?>
<unit-particle-system>
    <texture value="true" path="images/healing.bmp" luminance="true"/>
    <primitive value="quad"/>
    <offset x="0" y="1.4" z="0"/>
    <direction x="0" y="5" z="0"/>
    <color red=".4" green="1.0" blue=".4" alpha="0.5" />
    <size value="0.45" />
    <size-no-energy value="0.25" />
    <speed value="0.6" />
    <gravity value="0.05"/>
    <emission-rate value="0.1" />
    <energy-max value="40" />
    <energy-var value="30" />
    <fixed value="false" />
</unit-particle-system>
"""

PROJ_XML = """<?xml version="1.0" standalone="yes"?>
<projectile-particle-system>
    <texture value="true" path="images/arrow_glow.bmp" luminance="true"/>
    <model value="true" path="models/arrow.g3d"/>
    <primitive value="quad"/>
    <offset x="0" y="1.5" z="0"/>
    <size value="1" />
    <speed value="0" />
    <gravity value="0"/>
    <emission-rate value="1" />
    <energy-max value="20" />
    <energy-var value="0" />
    <trajectory type="parabolic"><speed value="20"/><scale value="1.5"/></trajectory>
</projectile-particle-system>
"""


def _make_bmp(path: Path) -> None:
    img = Image.new("RGBA", (16, 16), (255, 128, 0, 128))
    img.save(path, "BMP")


def _write_particle(dir_: Path, name: str, content: str) -> Path:
    parent = dir_ if isinstance(dir_, Path) else Path(dir_)
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / name
    path.write_text(content, encoding="utf-8")
    return path


def test_parse_splash_particle(tmp_path: Path) -> None:
    path = _write_particle(tmp_path / "units" / "x", "particle_splash.xml", SPLASH_XML)
    particle = parse_particle(path)
    assert particle.kind == "splash"
    assert particle.texture == (path.parent / "images" / "particle_splash.bmp").resolve()
    assert particle.luminance is True
    assert particle.color == (0.8, 0.3, 0.0, 0.3)
    assert particle.size == 0.8
    assert particle.speed == 7.0
    assert particle.gravity == 0.4
    assert particle.emission_rate == 80
    assert particle.energy_max == 30
    assert particle.energy_var == 2


def test_parse_unit_particle(tmp_path: Path) -> None:
    path = _write_particle(tmp_path, "healing.xml", UNIT_XML)
    particle = parse_particle(path)
    assert particle.kind == "unit"
    assert particle.direction == (0.0, 5.0, 0.0)
    assert particle.offset == (0.0, 1.4, 0.0)
    assert particle.color == (0.4, 1.0, 0.4, 0.5)
    assert particle.fixed is False


def test_parse_projectile_particle(tmp_path: Path) -> None:
    path = _write_particle(tmp_path, "proj.xml", PROJ_XML)
    particle = parse_particle(path)
    assert particle.kind == "projectile"
    assert particle.model == (path.parent / "models" / "arrow.g3d").resolve()
    assert particle.luminance is True


def test_write_particle_system_format(tmp_path: Path) -> None:
    path = _write_particle(tmp_path, "splash.xml", SPLASH_XML)
    particle = parse_particle(path)
    mod = tmp_path / "mod"
    tex = mod / "art" / "textures" / "particles" / "particle_splash.png"
    tex.parent.mkdir(parents=True)
    tex.write_bytes(b"x")
    out = mod / "art" / "particles" / "splash.xml"
    write_particle_system(particle, tex, mod, out)
    root = etree.parse(out).getroot()
    assert root.tag == "particles"
    assert root.find("texture").text == "art/textures/particles/particle_splash.png"
    assert root.find("blend").get("mode") == "add"
    rate = root.find("constant[@name='emissionrate']")
    assert rate is not None and float(rate.get("value")) == 80.0
    lifetime = root.find("uniform[@name='lifetime']")
    assert lifetime is not None
    assert float(lifetime.get("min")) == 28.0
    assert float(lifetime.get("max")) == 32.0
    vx = root.find("uniform[@name='velocity.x']")
    assert vx is None  # direction is 0
    force = root.find("force")
    assert force is not None and float(force.get("y")) == -0.4


def test_convert_particles_end_to_end(tmp_path: Path, layout_b_pack: Path) -> None:
    """A unit-level splash particle is converted + recorded on stats."""
    splash = _write_particle(tmp_path / "units" / "grunt", "splash.xml", SPLASH_XML)
    tex = tmp_path / "units" / "grunt" / "images" / "particle_splash.bmp"
    tex.parent.mkdir(parents=True, exist_ok=True)
    _make_bmp(tex)

    faction = Faction(name="demo", units={
            "grunt": UnitDef(name="grunt", directory=tmp_path / "units" / "grunt", skills={
                    "attack": SkillDef(
                        type="attack",
                        name="attack",
                        attack=AttackStats(projectile=False, splash_particle=splash),
                    )
                })
        })
    mod = tmp_path / "mod"
    stats = MediaConversionStats()
    convert_particles(faction, mod, Settings(), stats)

    # splash particle system + actor wrapper written
    assert (mod / "art" / "particles" / "splash.xml").is_file()
    assert (mod / "art" / "actors" / "particle" / "splash.xml").is_file()
    # particle texture converted to PNG
    assert (mod / "art" / "textures" / "particles" / "particle_splash.png").is_file()
    assert splash in stats.particle_systems
    # no projectile actor since splash has no projectile model
    assert stats.projectile_actor == {}
