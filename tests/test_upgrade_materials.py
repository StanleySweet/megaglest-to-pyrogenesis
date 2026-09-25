"""Contract tests for the standalone ``tools/upgrade_materials.py`` script."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from lxml import etree

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "upgrade_materials.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("upgrade_materials", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _actor(
    mod: Path,
    name: str,
    material: str,
    norm: str | None = None,
    spec: str | None = None,
) -> Path:
    """Write an actor with one textured variant; optional existing slots."""
    root = etree.Element("actor", version="1")
    group = etree.SubElement(root, "group")
    variant = etree.SubElement(group, "variant", frequency="1", name="Base")
    mesh = etree.SubElement(variant, "mesh")
    mesh.text = f"{name}.dae"
    textures = etree.SubElement(variant, "textures")
    etree.SubElement(textures, "texture", file=f"units/demo/{name}.png", name="baseTex")
    if norm is not None:
        etree.SubElement(textures, "texture", file=norm, name="normTex")
    if spec is not None:
        etree.SubElement(textures, "texture", file=spec, name="specTex")
    material_node = etree.SubElement(root, "material")
    material_node.text = material
    path = mod / "art/actors/units/demo" / f"{name}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(etree.tostring(root, xml_declaration=True, encoding="utf-8"))
    return path


def _norm_png(mod: Path, name: str, alpha: int) -> Path:
    """Write a normTex PNG whose alpha channel is uniformly ``alpha``."""
    from PIL import Image

    path = mod / "art/textures/skins/units/demo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (4, 4), (128, 128, 255, alpha)).save(path)
    return path


def _read(path: Path):
    return etree.parse(str(path)).getroot()


def _upgrade(mod: Path, warnings: list[str]) -> int:
    fixed = 0
    for path in sorted((mod / "art/actors").rglob("*.xml")):
        if _load_script().upgrade_file(path, mod, warnings):
            fixed += 1
    return fixed


def _skin(mod: Path, name: str) -> Path:
    path = mod / "art/textures/skins/units/demo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")
    return path


def test_upgrade_renames_four_spec_materials_and_fills_slots(tmp_path: Path) -> None:
    module = _load_script()
    mod = tmp_path / "mod"
    for name, material in (
        ("a", "basic_spec.xml"),
        ("b", "blend_spec.xml"),
        ("c", "objectcolor_spec.xml"),
        ("d", "playercolor_spec.xml"),
    ):
        _actor(mod, name, material)
        _skin(mod, f"{name}.png")

    warnings: list[str] = []
    for path in sorted((mod / "art/actors").rglob("*.xml")):
        module.upgrade_file(path, mod, warnings)

    expected = {
        "a": "no_trans_norm_spec.xml",
        "b": "basic_trans_norm_spec.xml",
        "c": "objectcolor_norm_spec.xml",
        "d": "player_trans_norm_spec.xml",
    }
    for name, material in expected.items():
        root = _read(mod / "art/actors/units/demo" / f"{name}.xml")
        assert root.find("material").text == material
        slots = {
            t.get("name"): t.get("file")
            for t in root.findall(".//variant/textures/texture")
        }
        assert slots["normTex"] == "default_norm.png"
        assert slots["specTex"] == "null_black.dds"
    assert warnings == []


def test_upgrade_uses_parallax_when_normtex_has_alpha(tmp_path: Path) -> None:
    module = _load_script()
    mod = tmp_path / "mod"
    _actor(mod, "alpha", "no_trans_spec.xml", norm="units/demo/norm_hm.png")
    _actor(mod, "flat", "no_trans_spec.xml", norm="units/demo/norm_flat.png")
    _norm_png(mod, "norm_hm.png", alpha=0)
    _norm_png(mod, "norm_flat.png", alpha=255)
    _skin(mod, "alpha.png")
    _skin(mod, "flat.png")

    warnings: list[str] = []
    for path in sorted((mod / "art/actors").rglob("*.xml")):
        module.upgrade_file(path, mod, warnings)

    assert _read(mod / "art/actors/units/demo/alpha.xml").find("material").text == (
        "no_trans_parallax_spec.xml"
    )
    assert _read(mod / "art/actors/units/demo/flat.xml").find("material").text == (
        "no_trans_norm_spec.xml"
    )


def test_upgrade_warns_unknown_material_and_missing_assets(tmp_path: Path) -> None:
    module = _load_script()
    mod = tmp_path / "mod"
    _actor(mod, "weird", "mystery_spec.xml")
    _skin(mod, "weird.png")

    warnings: list[str] = []
    for path in sorted((mod / "art/actors").rglob("*.xml")):
        module.upgrade_file(path, mod, warnings)

    root = _read(mod / "art/actors/units/demo/weird.xml")
    assert root.find("material").text == "mystery_spec.xml"  # untouched
    assert any("unknown material 'mystery_spec.xml'" in w for w in warnings)


def test_upgrade_writes_only_changed_files_and_counts(tmp_path: Path) -> None:
    module = _load_script()
    mod = tmp_path / "mod"
    old = _actor(mod, "old", "basic_spec.xml")
    modern = _actor(
        mod,
        "modern",
        "basic_trans_norm_spec.xml",
        norm="default_norm.png",
        spec="null_black.dds",
    )
    for name in ("old", "modern"):
        _skin(mod, f"{name}.png")
    before = {p: p.read_bytes() for p in (old, modern)}

    fixed = 0
    for path in sorted((mod / "art/actors").rglob("*.xml")):
        if module.upgrade_file(path, mod, []):
            fixed += 1

    assert fixed == 1
    assert old.read_bytes() != before[old]
    assert modern.read_bytes() == before[modern]  # no rewrite when nothing changed
