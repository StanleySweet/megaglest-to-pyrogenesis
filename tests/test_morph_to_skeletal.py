"""Contract tests for the standalone ``tools/morph_to_skeletal.py`` script."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from lxml import etree

_NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
_FIXTURE = Path(__file__).parent / "fixtures/g3d/dryad_idle.g3d"
_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "morph_to_skeletal.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("morph_to_skeletal", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_morph_to_skeletal_writes_skinned_mesh_and_skeletal_anim(tmp_path: Path) -> None:
    module = _load_script()
    output = tmp_path / "mod"

    rc = module.main([str(_FIXTURE), "-o", str(output), "--rig-bones", "6"])

    assert rc == 0
    meshes = sorted((output / "art" / "meshes" / "dryad_idle").glob("*.dae"))
    anims = sorted((output / "art" / "animation" / "dryad_idle").glob("*.dae"))
    assert meshes, "mesh DAE(s) written"
    assert anims == [output / "art" / "animation" / "dryad_idle" / "dryad_idle_idle.dae"]

    mesh = etree.parse(meshes[0]).getroot()
    # Skeletal output: a skin controller, never a morph target.
    assert mesh.find(".//c:library_controllers", _NS) is not None
    joint_array = mesh.find(".//c:skin//c:Name_array", _NS)

    assert joint_array is not None
    bone_count = int(joint_array.get("count"))
    assert bone_count >= 2

    anim = etree.parse(anims[0]).getroot()
    # One animation channel per bone: the PSA key count matches the PMD bones.
    channels = anim.findall(".//c:channel", _NS)
    assert len(channels) == bone_count
    assert anim.find(".//c:morph", _NS) is None
    # FCollada resolves the visual scene root through <scene>, so every
    # DAE -- animation files included -- must carry the instance link
    # (archivebuild: "failed requirement 'has root object'" otherwise).
    assert anim.find(".//c:scene/c:instance_visual_scene", _NS) is not None
    assert mesh.find(".//c:scene/c:instance_visual_scene", _NS) is not None


def test_morph_to_skeletal_rejects_static_model(tmp_path: Path) -> None:
    module = _load_script()
    static = Path(__file__).parent / "fixtures/g3d/gold.g3d"
    rc = module.main([str(static), "-o", str(tmp_path / "mod")])
    assert rc == 1
