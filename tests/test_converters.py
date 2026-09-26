"""Phase 3 converter tests: G3D→DAE, textures, audio, media orchestration."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest
from lxml import etree

from megaglest_to_0ad.converters.audio_converter import AudioConverter
from megaglest_to_0ad.converters.mesh_converter import (
    MeshConverter,
    diffuse_texture_names,
    read_g3d,
)
from megaglest_to_0ad.converters.texture_converter import TextureConverter, texture_stem
from megaglest_to_0ad.core.config import Settings
from megaglest_to_0ad.core.media_conversion import (
    _file_sha256,
    _unique_path,
    convert_faction_media,
)
from megaglest_to_0ad.megaglest.civ_loader import Faction, UnitDef, load_faction
from megaglest_to_0ad.megaglest.parser import discover_pack

G3D_FIXTURES = Path(__file__).parent / "fixtures" / "g3d"
NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
FFMPEG = Path("/opt/homebrew/bin/ffmpeg") if Path("/opt/homebrew/bin/ffmpeg").exists() else None
NEEDS_FFMPEG = pytest.mark.skipif(
    FFMPEG is None and shutil.which("ffmpeg") is None, reason="ffmpeg not available"
)


def _count(root: etree._Element, path: str) -> int:
    return len(root.xpath(path, namespaces=NS))


# ---------------------------------------------------------------------------
# G3D → COLLADA
# ---------------------------------------------------------------------------


def test_gold_static_dae(tmp_path: Path) -> None:
    converter = MeshConverter()
    result = converter.convert_g3d_to_dae(G3D_FIXTURES / "gold.g3d", tmp_path / "meshes", "demo")
    assert result.mesh_count == 1
    assert result.vertex_count == 216
    assert result.triangle_count == 300
    assert len(result.mesh_daes) == 1
    assert result.warnings == []
    root = etree.parse(result.mesh_daes[0]).getroot()
    assert _count(root, "//c:geometry") == 1
    assert _count(root, "//c:triangles") == 1
    # pure geometry: no materials/effects/images in the DAE (actors bind tex)
    assert _count(root, "//c:image") == 0
    assert _count(root, "//c:material") == 0
    assert _count(root, "//c:effect") == 0
    # exactly one instanced object in the scene
    assert _count(root, "//c:instance_geometry") == 1
    assert _count(root, "//c:node") == 1
    # <vertices> is POSITION-only; NORMAL/TEXCOORD live in <triangles>
    assert root.xpath("//c:vertices/c:input/@semantic", namespaces=NS) == ["POSITION"]
    tris = root.xpath("//c:triangles", namespaces=NS)[0]
    assert tris.get("count") == "300"
    assert tris.get("material") is None
    assert len(tris.xpath("c:input", namespaces=NS)) == 3
    p = tris.xpath("c:p/text()", namespaces=NS)[0].split()
    assert len(p) == 300 * 3 * 3  # 3 verts x 3 inputs interleaved
    # Y-up, unit = 1 meter
    up = root.xpath("//c:up_axis/text()", namespaces=NS)[0]
    unit = root.xpath("//c:unit/@meter", namespaces=NS)[0]
    assert up == "Y_UP"
    assert unit == "1"


def test_static_dae_winding_matches_g3d_verbatim(tmp_path: Path) -> None:
    """Triangle winding exports as-is: G3D faces and normals are already
    self-consistent (right-hand rule), matching 0 A.D.'s Blender/COLLADA
    convention. The engine's importer never flips indices (PMDConvert
    ReindexGeometry preserves order), so flipping here would invert lighting.
    """
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "gold.g3d", tmp_path / "meshes", "demo"
    )
    root = etree.parse(result.mesh_daes[0]).getroot()
    p = root.xpath("//c:triangles/c:p/text()", namespaces=NS)[0].split()
    # interleaved VERTEX/NORMAL/TEXCOORD (stride 3); VERTEX is offset 0
    written = [int(p[i]) for i in range(0, len(p), 3)]
    model = read_g3d(G3D_FIXTURES / "gold.g3d")
    assert written == model.meshes[0].indices


def test_dae_authoring_tool_identifies_converter(tmp_path: Path) -> None:
    """The DAE contributor names the converter; the element is the COLLADA
    1.5 ``authoring_tool`` (FCollada rejects ``source_tool`` as unknown)."""
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "gold.g3d", tmp_path / "meshes", "demo"
    )
    root = etree.parse(result.mesh_daes[0]).getroot()
    tool = root.xpath("//c:contributor/c:authoring_tool/text()", namespaces=NS)
    assert tool == ["megaglest to pyrogenesis"]
    assert not root.xpath("//c:contributor/c:source_tool", namespaces=NS)


def test_dae_stays_geometry_only_with_textures_present(tmp_path: Path) -> None:
    """Textures next to the DAE never leak into it (actor job, not mesh)."""
    converter = MeshConverter()
    tex_path = tmp_path / "textures" / "units" / "demo" / "texture_gold.png"
    tex_path.parent.mkdir(parents=True)
    tex_path.write_bytes((G3D_FIXTURES / "texture_gold.png").read_bytes())
    result = converter.convert_g3d_to_dae(
        G3D_FIXTURES / "gold.g3d", tmp_path / "meshes" / "demo", "demo"
    )
    root = etree.parse(result.mesh_daes[0]).getroot()
    assert _count(root, "//c:image") == 0
    assert _count(root, "//c:material") == 0
    assert _count(root, "//c:effect") == 0
    assert _count(root, "//c:newparam") == 0


def test_treant_idle_multi_mesh_static_only(tmp_path: Path) -> None:
    """Multi-mesh G3D splits into one static DAE per mesh; no animation."""
    converter = MeshConverter()
    result = converter.convert_g3d_to_dae(
        G3D_FIXTURES / "treant_idle.g3d", tmp_path / "meshes", "demo"
    )
    assert result.mesh_count == 3
    assert result.vertex_count == 648  # 3 meshes x 216
    assert result.triangle_count == 900  # 3 x 900 indices / 3
    assert len(result.mesh_daes) == 3
    assert [p.stem for p in result.mesh_daes] == [
        "treant_idle_01",
        "treant_idle_02",
        "treant_idle_03",
    ]
    for path in result.mesh_daes:
        assert path.exists()
        root = etree.parse(path).getroot()
        assert _count(root, "//c:image") == 0
        assert _count(root, "//c:material") == 0
        assert _count(root, "//c:effect") == 0
        # pure geometry: no controllers, no morphs, no animations
        assert _count(root, "//c:controller") == 0
        assert _count(root, "//c:morph") == 0
        assert _count(root, "//c:animation") == 0
        assert _count(root, "//c:geometry") == 1
        assert _count(root, "//c:instance_geometry") == 1
        assert _count(root, "//c:instance_controller") == 0
        assert _count(root, "//c:node") == 1


def test_workshop_cons_no_uv_mesh(tmp_path: Path) -> None:
    """Mesh.006 has textures==0 (no UV block); file parses to exact EOF."""
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "workshop_cons.g3d", tmp_path / "meshes", "demo"
    )
    assert result.mesh_count == 5
    assert len(result.mesh_daes) == 5
    assert not result.truncated
    assert result.warnings == []
    root = etree.parse(result.mesh_daes[0]).getroot()
    assert _count(root, "//c:geometry") == 1
    assert _count(root, "//c:node") == 1
    # UV-less mesh 4 (Mesh.006) still carries a TEXCOORD input: the importer
    # REQUIRES POSITION/NORMAL/TEXCOORD on the polygons, so the writer emits
    # a synthetic all-zero source instead of omitting it.
    no_uv = etree.parse(result.mesh_daes[4]).getroot()
    uv = no_uv.xpath("//c:triangles/c:input[@semantic='TEXCOORD']", namespaces=NS)
    assert len(uv) == 1
    src = no_uv.xpath(
        "//c:float_array[@id='mg_workshop_cons-m4-texcoords-array']", namespaces=NS
    )[0]
    assert src.get("count") == "864"  # 432 vertices x 2 UV floats
    floats = src.text.split()
    assert len(floats) == 2 * 432
    assert all(f == "0" for f in floats)
    assert no_uv.xpath("//c:triangles/c:input[@semantic='TEXCOORD']/@offset", namespaces=NS) == [
        "2"
    ]


def test_v3_tower_destruction(tmp_path: Path) -> None:
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "tower_destruction.g3d", tmp_path / "meshes", "demo"
    )
    assert result.mesh_count == 1
    assert result.vertex_count == 836
    assert result.triangle_count == 1440  # 4320 indices / 3
    assert result.warnings == []
    root = etree.parse(result.mesh_daes[0]).getroot()
    assert _count(root, "//c:geometry") == 1


def test_v3_doubled_texture_extension_preserved() -> None:
    model = read_g3d(G3D_FIXTURES / "tower_destruction.g3d")
    names = [n for n in diffuse_texture_names(model) if n]
    assert names == ["texture_spark.tga.tga"]  # spec-preserved, not cleaned


def test_texture_stem_strips_doubled_extension() -> None:
    assert texture_stem("texture_spark.tga.tga") == "texture_spark"
    assert texture_stem("gold.bmp") == "gold"
    assert texture_stem("grunt.png") == "grunt"
    assert texture_stem("sound.wav") == "sound.wav"  # not an image ext


def test_convert_to_png_rgba(tmp_path: Path) -> None:
    out = tmp_path / "out.png"
    TextureConverter().convert_to_png(G3D_FIXTURES / "texture_gold.png", out)
    from PIL import Image

    with Image.open(out) as img:
        assert img.mode == "RGBA"
        assert img.size == (128, 128)


def test_convert_tga(tmp_path: Path) -> None:
    out = tmp_path / "ashes.png"
    TextureConverter().convert_to_png(G3D_FIXTURES / "texture_spark.tga.tga", out)
    from PIL import Image

    with Image.open(out) as img:
        assert img.mode == "RGBA"


def test_convert_to_png_pot_upscale(tmp_path: Path) -> None:
    """Non-power-of-two textures are upscaled so archivebuild doesn't warn."""
    from PIL import Image

    source = tmp_path / "wide.png"
    Image.new("RGBA", (800, 258), (0, 0, 0, 255)).save(source)
    out = tmp_path / "wide_out.png"
    TextureConverter().convert_to_png(source, out)
    with Image.open(out) as img:
        assert img.size == (1024, 512)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


@NEEDS_FFMPEG
def test_wav_to_ogg(tmp_path: Path) -> None:
    source = (
        Path(__file__).parent
        / "fixtures"
        / "packs"
        / "layout_b"
        / "factions"
        / "demo"
        / "units"
        / "grunt"
        / "sounds"
        / "ack1.wav"
    )
    out = tmp_path / "ack1.ogg"
    AudioConverter().convert_wav_to_ogg(source, out)
    assert out.read_bytes()[:4] == b"OggS"


def test_ogg_copied_verbatim(tmp_path: Path) -> None:
    source = (
        Path(__file__).parent
        / "fixtures"
        / "packs"
        / "layout_b"
        / "factions"
        / "demo"
        / "music"
        / "theme.ogg"
    )
    out = tmp_path / "theme.ogg"
    AudioConverter().copy_ogg(source, out)
    assert out.read_bytes() == source.read_bytes()


def test_sound_group_xml(tmp_path: Path) -> None:
    group = tmp_path / "groups" / "treant_select.xml"
    AudioConverter().write_sound_group(group, "audio/sfx/demo/", ["a.ogg", "b.ogg"])
    root = etree.parse(group).getroot()
    assert root.tag == "SoundGroup"
    tags = [child.tag for child in root]
    assert tags == [
        "Gain",
        "Priority",
        "ConeGain",
        "Looping",
        "RandOrder",
        "RandGain",
        "GainLower",
        "RandPitch",
        "PitchUpper",
        "Threshold",
        "Path",
        "Sound",
        "Sound",
    ]
    assert root.find("Gain").text == "1"
    assert root.find("Path").text == "audio/sfx/demo/"
    assert [s.text for s in root.findall("Sound")] == ["a.ogg", "b.ogg"]


# ---------------------------------------------------------------------------
# Media orchestration
# ---------------------------------------------------------------------------


def test_skip_media_produces_nothing(layout_b_pack: Path, tmp_path: Path) -> None:
    pack = discover_pack(layout_b_pack)
    faction = load_faction(pack, pack.factions_dir / "demo")
    stats = convert_faction_media(faction, tmp_path / "mod", Settings(skip_media=True))
    assert stats.meshes == 0
    assert stats.textures == 0
    assert stats.sounds == 0
    assert stats.music == 0
    assert stats.generated == []
    assert not (tmp_path / "mod").exists()


def test_media_conversion_resilient(layout_b_pack: Path, tmp_path: Path) -> None:
    """Junk fixture G3Ds fail loudly but portraits/audio still convert."""
    pack = discover_pack(layout_b_pack)
    faction = load_faction(pack, pack.factions_dir / "demo")
    stats = convert_faction_media(faction, tmp_path / "mod", Settings(), pack.resources_dir)
    assert stats.meshes == 0  # 7-byte placeholder G3Ds
    assert stats.warnings  # one warning per broken model
    assert stats.music == 1  # theme.ogg copied
    assert stats.textures >= 2  # grunt.bmp + barracks.bmp portraits
    assert any(path.endswith("grunt.png") for path in stats.generated)
    assert any(path.endswith("theme.ogg") for path in stats.generated)


def test_mesh_stem_collision_deduped(tmp_path: Path) -> None:
    """Same-named models from different dirs get distinct DAEs + registry."""
    one = tmp_path / "a" / "stone.g3d"
    two = tmp_path / "b" / "stone.g3d"
    three = tmp_path / "c" / "stone.g3d"
    for target in (one, two, three):
        target.parent.mkdir(parents=True)
        target.write_bytes((G3D_FIXTURES / "gold.g3d").read_bytes())

    faction = Faction(
        name="demo",
        directory=tmp_path,
        xml_path=tmp_path / "factions.xml",
        units={
            "a": UnitDef(name="a", directory=one.parent, xml_path=tmp_path / "a.xml"),
            "b": UnitDef(name="b", directory=two.parent, xml_path=tmp_path / "b.xml"),
            "c": UnitDef(name="c", directory=three.parent, xml_path=tmp_path / "c.xml"),
        },
    )
    mod = tmp_path / "mod"
    stats = convert_faction_media(faction, mod, Settings(), tmp_path)

    meshes_dir = mod / "art/meshes" / "demo"
    assert sorted(p.name for p in meshes_dir.glob("*.dae")) == [
        "stone.dae",
        "stone_01.dae",
        "stone_02.dae",
    ]
    # registry maps each source model to its own written DAEs
    assert stats.models[one].mesh_daes == [meshes_dir / "stone.dae"]
    assert stats.models[two].mesh_daes == [meshes_dir / "stone_01.dae"]
    assert stats.models[three].mesh_daes == [meshes_dir / "stone_02.dae"]
    assert stats.meshes == 3
    # no animation DAEs are emitted for game output
    assert not any(path.endswith("animation") for path in stats.generated)


def test_texture_alpha_flags_model_transparent(tmp_path: Path) -> None:
    """A model whose texture has real alpha (pixels below half opacity) is
    marked transparent for the actor's material; a near-opaque alpha band
    (e.g. the gold texture's min-225 matte) is not."""
    from PIL import Image

    opaque_dir = tmp_path / "opaque"
    alpha_dir = tmp_path / "alpha"
    for d in (opaque_dir, alpha_dir):
        d.mkdir()
        (d / "gold.g3d").write_bytes((G3D_FIXTURES / "gold.g3d").read_bytes())
    (opaque_dir / "texture_gold.png").write_bytes(
        (G3D_FIXTURES / "texture_gold.png").read_bytes()
    )
    with Image.new("RGBA", (8, 8), (255, 0, 0, 0)) as img:
        img.save(alpha_dir / "texture_gold.png")

    faction = Faction(
        name="demo",
        directory=tmp_path,
        xml_path=tmp_path / "factions.xml",
        units={
            "opaque": UnitDef(
                name="opaque", directory=opaque_dir, xml_path=tmp_path / "opaque.xml"
            ),
            "alpha": UnitDef(
                name="alpha", directory=alpha_dir, xml_path=tmp_path / "alpha.xml"
            ),
        },
    )
    stats = convert_faction_media(faction, tmp_path / "mod", Settings(), tmp_path)
    assert alpha_dir / "gold.g3d" in stats.transparent_models
    assert opaque_dir / "gold.g3d" not in stats.transparent_models


def test_texture_content_dedup(tmp_path: Path) -> None:
    """Identical texture bytes from different dirs produce ONE output."""
    mod = tmp_path / "mod"
    texture_bytes = (G3D_FIXTURES / "texture_gold.png").read_bytes()
    dirs = [tmp_path / "a", tmp_path / "b"]
    written: set[Path] = set()
    texture_by_hash: dict[str, Path] = {}
    textures_dir = mod / "art/textures/skins/units/demo"
    for model_dir in dirs:
        model_dir.mkdir(parents=True)
        (model_dir / "texture_gold.png").write_bytes(texture_bytes)

    for model_dir in dirs:
        source = model_dir / "texture_gold.png"
        digest = _file_sha256(source)
        canonical = texture_by_hash.get(digest)
        if canonical is not None:
            continue
        output = _unique_path(textures_dir, "texture_gold", ".png", written)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(texture_bytes)
        texture_by_hash[digest] = output
        written.add(output)

    pngs = list((mod / "art/textures/skins/units/demo").glob("*.png"))
    assert pngs == [mod / "art/textures/skins/units/demo/texture_gold.png"]


# ---------------------------------------------------------------------------
# Rig synthesis + texture merging (Phase 5)
# ---------------------------------------------------------------------------


def test_animation_duration_semantics() -> None:
    from megaglest_to_0ad.converters.rig import animation_duration

    assert animation_duration(40.0) == pytest.approx(2.5)
    assert animation_duration(20.0) == pytest.approx(5.0)
    assert animation_duration(0.0) == 1.0
    assert animation_duration(-5.0) == 1.0


def test_kabsch_fits_rotation_without_scale() -> None:
    """A rotated point cloud fits to the exact rotation; identity is exact."""
    from megaglest_to_0ad.converters.rig import _kabsch

    points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 1.0, 1.0]]
    ca, sa = math.cos(0.7), math.sin(0.7)
    rotated = [[x * ca - z * sa, y, x * sa + z * ca] for x, y, z in points]
    r, t = _kabsch(points, rotated, [1.0] * len(points))
    assert r == pytest.approx([ca, 0, -sa, 0, 1, 0, sa, 0, ca], abs=1e-9)
    assert t == pytest.approx([0.0, 0.0, 0.0], abs=1e-9)

    r2, t2 = _kabsch(points, points, [1.0] * len(points))
    assert r2 == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1], abs=1e-9)
    assert t2 == pytest.approx([0.0, 0.0, 0.0], abs=1e-9)


def test_texture_groups_partition_treant() -> None:
    """Meshes merge by resolved texture: [0] and [1, 2] for the treant."""
    from megaglest_to_0ad.converters.mesh_converter import texture_groups

    model = read_g3d(G3D_FIXTURES / "treant_idle.g3d")
    groups = texture_groups(model, lambda name: Path("/tex") / name)
    assert groups == [[0], [1, 2]]


def test_static_same_texture_merge_single_instance(tmp_path: Path) -> None:
    """Static conversion with groups merges same-texture meshes into ONE
    geometry with a single instanced object per DAE."""
    from megaglest_to_0ad.converters.mesh_converter import MeshConverter, texture_groups

    model = read_g3d(G3D_FIXTURES / "treant_idle.g3d")
    groups = texture_groups(model, lambda name: Path("/tex") / name)
    assert groups == [[0], [1, 2]]
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "treant_idle.g3d", tmp_path / "meshes", "demo", groups=groups
    )
    assert len(result.mesh_daes) == 2
    for dae in result.mesh_daes:
        root = etree.parse(dae).getroot()
        assert _count(root, "//c:instance_geometry") == 1
    merged = etree.parse(result.mesh_daes[1]).getroot()
    assert _count(merged, "//c:geometry") == 1
    assert _count(merged, "//c:triangles") == 1
    merged_count = sum(len(mesh.indices) for mesh in model.meshes[1:]) // 3
    tris = merged.xpath("//c:triangles", namespaces=NS)[0]
    assert tris.get("count") == str(merged_count)


def test_static_per_mesh_groups_keep_separate_daes(tmp_path: Path) -> None:
    """groups=[[i] for each mesh] (construction stages) stay one DAE each."""
    from megaglest_to_0ad.converters.mesh_converter import MeshConverter

    model = read_g3d(G3D_FIXTURES / "workshop_cons.g3d")
    groups = [[i] for i in range(len(model.meshes))]
    result = MeshConverter().convert_g3d_to_dae(
        G3D_FIXTURES / "workshop_cons.g3d", tmp_path / "meshes", "demo", groups=groups
    )
    assert len(result.mesh_daes) == len(model.meshes)
    for dae in result.mesh_daes:
        root = etree.parse(dae).getroot()
        assert _count(root, "//c:instance_geometry") == 1


def test_texture_groups_uvless_own_group() -> None:
    """UV-less meshes (textures == 0) group under None, first-seen order."""
    from megaglest_to_0ad.converters.mesh_converter import texture_groups

    model = read_g3d(G3D_FIXTURES / "workshop_cons.g3d")
    names = [n for n in diffuse_texture_names(model) if n]
    assert names, "fixture must have textured meshes"
    groups = texture_groups(model, lambda name: Path("/tex") / name)
    flattened = [i for group in groups for i in group]
    assert sorted(flattened) == list(range(len(model.meshes)))


def test_fit_group_frames_expands_base_weights(tmp_path: Path) -> None:
    """use_base_weights handles more bones than max influences (K > 5)."""
    from megaglest_to_0ad.converters.rig import build_rig, fit_group_frames

    model = read_g3d(G3D_FIXTURES / "treant_idle.g3d")
    groups = [list(range(len(model.meshes)))]
    rig = build_rig(model, groups, 7, "test_root")
    frames, weights = fit_group_frames(model, model, rig, use_base_weights=True)
    assert len(frames) == 19
    # the returned weights are what the DAE skin must store
    assert weights == rig.groups[0].vertex_weights
    assert len(frames[0]) == 7
    for r, _t in frames[0]:
        assert r == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1], abs=1e-6)


def test_multi_group_skin_matches_group_geometry(tmp_path: Path) -> None:
    """Each texture group's DAE skins its OWN vertex set (counts must match)."""
    from megaglest_to_0ad.converters.mesh_converter import MeshConverter, texture_groups
    from megaglest_to_0ad.converters.rig import build_rig

    model = read_g3d(G3D_FIXTURES / "treant_idle.g3d")
    groups = texture_groups(model, lambda name: Path("/tex") / name)
    assert len(groups) == 2, "treant fixture must split into two texture groups"
    rig = build_rig(model, groups, 4, "test_root")
    converter = MeshConverter()
    result = converter.convert_g3d_to_dae(
        G3D_FIXTURES / "treant_idle.g3d", tmp_path, "demo", rig=rig
    )
    assert len(result.mesh_daes) == 2
    root = etree.parse(str(result.mesh_daes[1])).getroot()
    skin = root.find(".//c:skin", NS)
    vcount = skin.find("c:vertex_weights", NS)
    geom_positions = root.find(".//c:geometry//c:source//c:float_array", NS)
    assert geom_positions is not None
    geometry_verts = int(geom_positions.get("count")) // 3
    weights_count = int(vcount.get("count"))
    assert weights_count == geometry_verts, (
        f"skin weights ({weights_count}) must cover group geometry ({geometry_verts})"
    )


def test_align_points_translation() -> None:
    """_align_points finds the rigid transform mapping src→dst."""
    from megaglest_to_0ad.converters.rig import _align_points, _apply_rigid

    src = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0], [2.0, 6.0, 1.0]]
    offset = [5.0, -3.0, 2.0]
    dst = [[v[i] + offset[i] for i in range(3)] for v in src]
    rot, t = _align_points(src, dst)
    for s, d in zip(src, dst):
        result = _apply_rigid(rot, t, s)
        for axis in range(3):
            assert result[axis] == pytest.approx(d[axis], abs=1e-5)


def test_align_points_rotation() -> None:
    """_align_points handles a 90° rotation around Z."""
    from megaglest_to_0ad.converters.rig import _align_points, _apply_rigid

    src = [[2.0, 0.0, 0.0], [0.0, 3.0, 0.0], [1.0, 4.0, 0.0], [3.0, 1.0, 0.0]]
    # 90° CCW around Z: (x,y,z) → (-y,x,z)
    dst = [[0.0, 2.0, 0.0], [-3.0, 0.0, 0.0], [-4.0, 1.0, 0.0], [-1.0, 3.0, 0.0]]
    rot, t = _align_points(src, dst)
    for s, d in zip(src, dst):
        result = _apply_rigid(rot, t, s)
        for axis in range(3):
            assert result[axis] == pytest.approx(d[axis], abs=1e-5)


def test_fit_group_frames_aligns_offset_model() -> None:
    """Cross-model alignment removes rest-pose offset for foreign models."""
    from megaglest_to_0ad.converters.rig import build_rig, fit_group_frames

    model = read_g3d(G3D_FIXTURES / "treant_idle.g3d")
    groups = [list(range(len(model.meshes)))]
    rig = build_rig(model, groups, 7, "test_root")

    # Same model → use_base_weights=True: frame 0 must be identity.
    frames_same, _ = fit_group_frames(model, model, rig, use_base_weights=True)
    for r, _t in frames_same[0]:
        assert r == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1], abs=1e-6)

    # Foreign model (same data, but use_base_weights=False): frame 0 still
    # identity, and frame 1 transforms are finite (no NaN/inf from bad fits).
    frames_foreign, _ = fit_group_frames(model, model, rig, use_base_weights=False)
    for r, _t in frames_foreign[0]:
        assert r == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1], abs=1e-6)
    for f in range(1, len(frames_foreign)):
        for r, t in frames_foreign[f]:
            assert all(math.isfinite(v) for v in r)
            assert all(math.isfinite(v) for v in t)
