"""Importer-contract DAE validator tests (checks mirror PMDConvert.cpp)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from lxml import etree

from megaglest_to_0ad.converters.mesh_converter import MeshConverter, read_g3d, texture_groups
from megaglest_to_0ad.converters.rig import build_rig
from megaglest_to_0ad.oad.dae_validator import (
    _source_float_list,
    validate_animation_dae,
    validate_mesh_dae,
    validate_mod_meshes,
)


def test_source_float_list_parses_once() -> None:
    """Weight arrays are parsed a single time (per-reference re-parsing made
    skin validation quadratic on vertex x weight counts)."""
    dae = etree.Element(f"{{{NS['c']}}}root")
    source = etree.SubElement(dae, f"{{{NS['c']}}}source")
    array = etree.SubElement(source, f"{{{NS['c']}}}float_array")
    array.text = "0.25 0.5 0.25 1.0"
    assert _source_float_list(source) == [0.25, 0.5, 0.25, 1.0]
    assert _source_float_list(None) == []
    empty = etree.SubElement(dae, f"{{{NS['c']}}}source2")
    assert _source_float_list(empty) == []

G3D_FIXTURES = Path(__file__).parent / "fixtures" / "g3d"
NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}


def _tamper(dae: Path, rewrite) -> None:
    root = etree.parse(str(dae)).getroot()
    rewrite(root)
    etree.ElementTree(root).write(str(dae), encoding="utf-8", xml_declaration=True)


def _mod_tree(tmp_path: Path) -> Path:
    """Convert the three fixture g3ds into an art/ tree like the real pipeline."""
    mod = tmp_path / "mod"
    for g3d in ("gold.g3d", "dryad_idle.g3d", "academy_cons.g3d"):
        result = MeshConverter().convert_g3d_to_dae(
            G3D_FIXTURES / g3d, mod / "art" / "meshes" / "elves", "elves"
        )
        for path in result.mesh_daes:
            assert path.exists()
    return mod


def test_audit_pack_ok(tmp_path: Path) -> None:
    mod = _mod_tree(tmp_path)
    audit = validate_mod_meshes(mod)
    # 1 (gold) + 3 (dryad) + 5 (academy) static DAEs, all importable
    assert audit.checked == 9
    assert audit.passed == 9
    assert audit.ok


def test_static_dae_importable(tmp_path: Path) -> None:
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "gold.g3d", tmp_path / "meshes", "elves"
    )
    assert validate_mesh_dae(result.mesh_daes[0]) == []


def test_uvless_mesh_importable(tmp_path: Path) -> None:
    """academy_cons Mesh.006 has no UV block; synthetic TEXCOORD keeps it legal."""
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "academy_cons.g3d", tmp_path / "meshes", "elves"
    )
    assert validate_mesh_dae(result.mesh_daes[4]) == []


def test_controller_instance_unresolved(tmp_path: Path) -> None:
    """A controller whose url resolves to nothing is flagged."""
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "gold.g3d", tmp_path / "meshes", "elves"
    )
    dae = result.mesh_daes[0]

    def rewrite(root: etree._Element) -> None:
        node = root.xpath("//c:node", namespaces=NS)[0]
        inst = node.xpath("c:instance_geometry", namespaces=NS)[0]
        node.remove(inst)
        controller = etree.SubElement(node, f"{{{NS['c']}}}instance_controller")
        controller.set("url", "#fake-controller")

    _tamper(dae, rewrite)
    failures = validate_mesh_dae(dae)
    assert len(failures) == 1
    assert "does not resolve to a controller" in failures[0]


def _rigged(tmp_path: Path) -> tuple[Path, Path, object]:
    """Skinned mesh DAE + animation DAE for the dryad fixture."""
    model = read_g3d(G3D_FIXTURES / "dryad_idle.g3d")
    groups = texture_groups(model, lambda _name: None)
    rig = build_rig(model, groups, 4, "test_root")
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "dryad_idle.g3d", tmp_path / "meshes", "elves", rig=rig
    )
    anim = tmp_path / "meshes" / "dryad_idle_stop.dae"
    MeshConverter().write_animation_dae(model, model, rig, anim, "elves", 40.0, True)
    return result.mesh_daes[0], anim, rig


def test_skinned_dae_importable(tmp_path: Path) -> None:
    """Skinned mesh DAEs satisfy the geometry+skin contract."""
    mesh_dae, _anim, _rig = _rigged(tmp_path)
    assert validate_mesh_dae(mesh_dae) == []


def test_animation_dae_valid(tmp_path: Path) -> None:
    """Animation DAEs satisfy the geometry+skin+channel contract."""
    _mesh, anim, _rig = _rigged(tmp_path)
    assert validate_animation_dae(anim) == []


def test_animation_dae_detects_bad_channel(tmp_path: Path) -> None:
    """A channel targeting a missing joint is flagged."""
    _mesh, anim, _rig = _rigged(tmp_path)

    def rewrite(root: etree._Element) -> None:
        channel = root.xpath("//c:channel", namespaces=NS)[0]
        channel.set("target", "not_a_joint/transform")

    _tamper(anim, rewrite)
    failures = validate_animation_dae(anim)
    assert any("channel target" in f for f in failures)


def test_skin_detects_unnormalized_weights(tmp_path: Path) -> None:
    """Vertex weights that do not sum to one are flagged."""
    mesh_dae, _anim, _rig = _rigged(tmp_path)

    def rewrite(root: etree._Element) -> None:
        weights = root.xpath(
            "//c:skin/c:source/c:float_array[contains(@id, 'weights')]", namespaces=NS
        )[0]
        values = [float(v) for v in weights.text.split()]
        values[0] *= 2.0
        weights.text = " ".join(f"{v:.7g}" for v in values)

    _tamper(mesh_dae, rewrite)
    failures = validate_mesh_dae(mesh_dae)
    assert any("not normalized" in f for f in failures)


def test_detects_second_instanced_object(tmp_path: Path) -> None:
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "gold.g3d", tmp_path / "meshes", "elves"
    )
    dae = result.mesh_daes[0]

    def duplicate(root: etree._Element) -> None:
        node = root.xpath("//c:node", namespaces=NS)[0]
        scene = root.xpath("//c:visual_scene", namespaces=NS)[0]
        scene.append(copy.deepcopy(node))

    _tamper(dae, duplicate)
    failures = validate_mesh_dae(dae)
    assert any("exactly one instanced object" in f for f in failures)


def test_detects_missing_texcoord(tmp_path: Path) -> None:
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "gold.g3d", tmp_path / "meshes", "elves"
    )
    dae = result.mesh_daes[0]

    def drop_uv(root: etree._Element) -> None:
        tris = root.xpath("//c:triangles", namespaces=NS)[0]
        for inp in tris.findall("c:input", namespaces=NS):
            if inp.get("semantic") == "TEXCOORD":
                tris.remove(inp)

    _tamper(dae, drop_uv)
    failures = validate_mesh_dae(dae)
    assert any("missing TEXCOORD" in f for f in failures)


def test_detects_malformed_xml(tmp_path: Path) -> None:
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "gold.g3d", tmp_path / "meshes", "elves"
    )
    result.mesh_daes[0].write_text("<COLLADA><broken", encoding="utf-8")
    failures = validate_mesh_dae(result.mesh_daes[0])
    assert any("XML parse error" in f for f in failures)


@pytest.mark.parametrize("junk", ["not xml at all", "<foo/>", ""])
def test_detects_junk_files(tmp_path: Path, junk: str) -> None:
    bad = tmp_path / "bad.dae"
    bad.write_text(junk, encoding="utf-8")
    failures = validate_mesh_dae(bad)
    assert failures
