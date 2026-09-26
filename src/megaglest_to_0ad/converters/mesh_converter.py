"""G3D → COLLADA 1.4.1 mesh + animation conversion.

Parses MegaGlest `.g3d` models (v3 and v4) with the vendored importer
(`vendor/g3d/g3dlib.py`, format spec in `vendor/g3d/g3d_format.md`) and
writes COLLADA 1.4.1 files consumable by 0 A.D.

Two output families:

- **Static DAEs** (models without a synthesized rig): one base-pose DAE
  (frame 0) per mesh -> ``art/meshes/{civ}/``.
- **Skinned DAEs** (models with a rig, see ``converters/rig.py``): the
  geometry is merged per resolved texture (``texture_groups``) and each
  group becomes one DAE carrying geometry + skin controller + armature.
  The rig is synthetic: G3D has no bones, so k-means displacement
  clustering + rigid fits provide the joint contract.
- **Animation DAEs** (``write_animation_dae``): one file per skill
  animation -> ``art/animation/{civ}/``. Each is self-contained
  (geometry + skin + armature + samplers), as PSAConvert requires a skin
  and evaluates the hierarchy.

DAE content contract (0 A.D. importer rules): no materials, effects, or
images; exactly one instanced object per file; textures are declared in
Actor XML, never in the mesh.
"""

from __future__ import annotations

import io
import logging
import math
import re
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from lxml import etree

from ..core.errors import ConversionError

# ---------------------------------------------------------------------------
# vendored importer bootstrap (must run before any module that imports
# g3dlib — including rig.py — is imported)
# ---------------------------------------------------------------------------

_VENDOR_G3D_DIR = Path(__file__).resolve().parents[3] / "vendor" / "g3d"
if str(_VENDOR_G3D_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_G3D_DIR))

import g3dlib  # noqa: E402  (vendored third-party module; see vendor/g3d/)

from .rig import Rig, animation_duration, fit_group_frames  # noqa: E402

LOGGER = logging.getLogger(__name__)

_COLLADA_NS = "http://www.collada.org/2005/11/COLLADASchema"
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# v3 mesh property flags (model_header.h: enum MeshPropertyV3)
_MP3_NO_TEXTURE = 1
_MP3_TWO_SIDED = 2
_MP3_CUSTOM_COLOR = 4

_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

_IDENTITY4 = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _fmt(value: float) -> str:
    """Format a float32-derived value with full round-trip precision."""
    return f"{value:.7g}"


def _sanitize_id(value: str, fallback: str = "mesh") -> str:
    """Collapse non-XML-ID characters; ensure the id starts with a letter."""
    cleaned = _NAME_RE.sub("_", value).strip("_")
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}"
    return cleaned


# ---------------------------------------------------------------------------
# model loading
# ---------------------------------------------------------------------------


# Parsed models are reused: the parent process already reads every model to
# build the rig, the mesh pass asks for the same file again by path, and each of
# a pack's animations re-reads its base model in a worker. Parsing is pure, so
# the result is kept per (path, mtime, size).
#
# 16 entries is a deliberate ceiling rather than a tuned number. Measured on the
# committed fixtures, a parsed model retains ~2.8 MB (648 vertices, 19 frames),
# so 16 entries hold ~45 MB; that covers the models in flight for one unit while
# keeping the cache from becoming the dominant memory cost of a conversion.
# Nothing mutates a parsed model, so sharing one object is safe.
_MODEL_CACHE_SIZE = 16


def clear_model_cache() -> None:
    """Forget every parsed model. For tests and long-lived processes."""
    _parse_g3d.cache_clear()


def read_g3d(path: Path) -> g3dlib.G3DModel:
    """Parse a G3D file (v3 or v4) into the shared ``G3DModel`` shape.

    v4 goes through the vendored importer with truncation tolerance (a short
    final mesh is clamped to EOF and flagged, never fatal). v3 is a separate,
    simpler format (see ``vendor/g3d/g3d_format.md``).

    Cached per ``(path, mtime, size)``, so a rewritten model is never served
    stale. The returned model is shared between callers and must be treated as
    read-only.
    """
    try:
        stamp = path.stat()
    except OSError as exc:
        raise ConversionError(f"cannot read model {path}: {exc}") from exc
    return _parse_g3d(str(path), stamp.st_mtime_ns, stamp.st_size)


@lru_cache(maxsize=_MODEL_CACHE_SIZE)
def _parse_g3d(path: str, mtime_ns: int, size: int) -> g3dlib.G3DModel:
    """Uncached parse. ``mtime_ns`` and ``size`` are cache-key material only."""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise ConversionError(f"cannot read model {target}: {exc}") from exc
    if raw[:3] != b"G3D":
        raise ConversionError(f"not a G3D file (bad magic): {target}")
    version = raw[3]
    if version == 4:
        stream = io.BytesIO(raw)
        return g3dlib.G3DModel.read_stream(stream, tolerate_truncation=True)
    if version == 3:
        return _read_v3(raw, target.stem)
    raise ConversionError(
        f"unsupported G3D version {version} in {target} (v3 and v4 only)"
    )


def _read_v3(raw: bytes, name: str) -> g3dlib.G3DModel:
    """Parse the v3 layout: meshCount + one MeshHeaderV3 per mesh."""
    view = memoryview(raw)
    offset = 4  # skip "G3D" + version

    def read_u32() -> int:
        nonlocal offset
        value = int.from_bytes(view[offset : offset + 4], "little")
        offset += 4
        return value

    def read_floats(count: int) -> list[float]:
        nonlocal offset
        size = count * 4
        if offset + size > len(raw):
            raise ConversionError(
                f"v3 data truncated: need {size} bytes at 0x{offset:x}, file is {len(raw)} bytes"
            )
        values = list(struct.unpack(f"<{count}f", view[offset : offset + size]))
        offset += size
        return values

    mesh_count = read_u32()
    meshes: list[g3dlib.Mesh] = []
    for mesh_index in range(mesh_count):
        vertex_frame_count = read_u32()
        normal_frame_count = read_u32()
        tex_coord_frame_count = read_u32()
        color_frame_count = read_u32()
        point_count = read_u32()
        index_count = read_u32()
        properties = read_u32()
        if vertex_frame_count != normal_frame_count:
            raise ConversionError(
                "v3 vertex frame count != normal frame count: "
                f"{vertex_frame_count} != {normal_frame_count}"
            )
        tex_name = (
            bytes(view[offset : offset + 64]).split(b"\x00", 1)[0].decode("ascii", errors="replace")
        )
        offset += 64

        frame_verts = vertex_frame_count * point_count
        vertices = read_floats(frame_verts * 3)
        normals = read_floats(frame_verts * 3)
        textured = (properties & _MP3_NO_TEXTURE) == 0
        tex_coords: list[float] = []
        if textured:
            tex_coords = read_floats(tex_coord_frame_count * point_count * 2)
        diffuse = read_floats(3)
        opacity = read_floats(1)[0]
        offset += max(color_frame_count - 1, 0) * 16  # extra color frames
        indices = [
            int.from_bytes(view[offset + i * 4 : offset + i * 4 + 4], "little")
            for i in range(index_count)
        ]
        offset += index_count * 4

        v4_properties = 0
        if properties & _MP3_TWO_SIDED:
            v4_properties |= g3dlib.PROP_TWO_SIDED
        if properties & _MP3_CUSTOM_COLOR:
            v4_properties |= g3dlib.PROP_CUSTOM_COLOR

        meshes.append(
            g3dlib.Mesh(
                name=name if mesh_count == 1 else f"{name}_{mesh_index}",
                frame_count=vertex_frame_count,
                vertex_count=point_count,
                index_count=index_count,
                diffuse_color=tuple(diffuse),
                specular_color=(0.0, 0.0, 0.0),
                specular_power=0.0,
                opacity=opacity,
                properties=v4_properties,
                textures=g3dlib.TEX_DIFFUSE if textured else 0,
                texture_names=[(g3dlib.TEX_DIFFUSE, tex_name)] if textured else [],
                vertices=vertices,
                normals=normals,
                tex_coords=tex_coords,
                indices=indices,
            )
        )
    if offset != len(raw):
        raise ConversionError(f"v3 trailing bytes: {len(raw) - offset} unread at EOF")
    return g3dlib.G3DModel(version=3, model_type=0, meshes=meshes)


def diffuse_texture_names(model: g3dlib.G3DModel) -> list[str | None]:
    """Per-mesh diffuse texture names (slot bit 1), in mesh order."""
    names: list[str | None] = []
    for mesh in model.meshes:
        name = next(
            (n for bit, n in mesh.texture_names if bit == g3dlib.TEX_DIFFUSE),
            None,
        )
        names.append(name)
    return names


def texture_groups(
    model: g3dlib.G3DModel,
    resolve: Callable[[str], Path | None],
) -> list[list[int]]:
    """Partition ``model.meshes`` by resolved diffuse texture.

    Meshes sharing a texture merge into one skinned DAE (one 0 A.D. PMD has
    exactly one material, so each texture group is a separate file). UV-less
    meshes (``textures == 0``) form their own group keyed ``None``. Group
    order follows first-seen texture order; the actor references group 0.
    """
    groups: dict[Path | None, list[int]] = {}
    order: list[Path | None] = []
    for index, name in enumerate(diffuse_texture_names(model)):
        key = resolve(name) if name else None
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(index)
    return [groups[key] for key in order]


def output_count(model: g3dlib.G3DModel, rig: Rig | None) -> int:
    """Number of DAEs ``convert_g3d_to_dae`` writes for ``model``."""
    return len(rig.groups) if rig is not None else len(model.meshes)


# ---------------------------------------------------------------------------
# COLLADA output
# ---------------------------------------------------------------------------


@dataclass
class ConvertedMesh:
    """Result of converting one G3D file.

    ``mesh_daes`` holds one DAE per texture-merged group (skinned models)
    or per mesh (static models); actors reference ``mesh_daes[0]``. ``rig``
    is the synthesized joint contract for skinned models (None = static).
    """

    g3d_path: Path
    mesh_daes: list[Path]
    rig: Rig | None = None
    truncated: bool = False
    mesh_count: int = 0
    vertex_count: int = 0
    triangle_count: int = 0
    # (width, depth, height) in model units from the frame-0 bounding box of
    # every mesh; the template generator derives Footprint/Obstruction from
    # it (MegaGlest's `size` parameter is not a usable 0 A.D. footprint).
    footprint: tuple[float, float, float] | None = None
    warnings: list[str] = field(default_factory=list)


class MeshConverter:
    """Convert G3D models to COLLADA 1.4.1 (static or skinned).

    0 A.D.'s COLLADA importer reads POSITION/NORMAL/TEXCOORD only and
    requires exactly one instanced object per file; materials and textures
    never appear in the DAE (actors declare textures). Skinned models
    (``rig`` given) merge same-texture meshes into one geometry per group
    and add a skin controller + armature; the rig's joint contract is
    shared by every animation DAE of the model.
    """

    def convert_g3d_to_dae(
        self,
        g3d_path: Path,
        output_dir: Path,
        civ: str,
        stem: str | None = None,
        rig: Rig | None = None,
        groups: list[list[int]] | None = None,
    ) -> ConvertedMesh:
        """Convert ``g3d_path`` to DAEs under ``output_dir``.

        Static (``rig is None``): one base-pose DAE per texture group
        (``groups`` partitions the meshes; same-texture subobjects merge
        into one geometry, per the importer's one-instanced-object rule).
        Skinned: one DAE per rig texture group. ``stem`` overrides the
        output basename (callers use it to disambiguate same-named models
        from different source directories).
        """
        model = read_g3d(g3d_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = _sanitize_id(stem or g3d_path.stem)
        base = f"mg_{stem}"

        mesh_daes: list[Path] = []
        if rig is None:
            mesh_groups = groups if groups is not None else [[i] for i in range(len(model.meshes))]
            multi = len(mesh_groups) > 1
            for group_index, mesh_indices in enumerate(mesh_groups):
                name = _mesh_variant_name(stem, group_index, multi)
                mesh_dae = output_dir / f"{name}.dae"
                self._write_static_dae(model, group_index, mesh_indices, mesh_dae, base, civ)
                mesh_daes.append(mesh_dae)
        else:
            multi = output_count(model, rig) > 1
            for group_index, group in enumerate(rig.groups):
                name = _mesh_variant_name(stem, group_index, multi)
                mesh_dae = output_dir / f"{name}.dae"
                self._write_skinned_dae(model, group_index, mesh_dae, base, civ, rig)
                mesh_daes.append(mesh_dae)

        result = ConvertedMesh(
            g3d_path=g3d_path,
            mesh_daes=mesh_daes,
            rig=rig,
            truncated=model.truncated,
            mesh_count=len(mesh_daes),
            vertex_count=sum(m.vertex_count for m in model.meshes),
            triangle_count=sum(len(m.indices) // 3 for m in model.meshes),
            footprint=_model_footprint(model),
        )
        if model.truncated:
            result.warnings.append(
                f"truncated model data recovered (meshes: {', '.join(model.truncated_mesh_names)})"
            )
        return result

    def write_animation_dae(
        self,
        base_model: g3dlib.G3DModel,
        anim_model: g3dlib.G3DModel,
        rig: Rig,
        path: Path,
        civ: str,
        anim_speed: float,
        loop: bool,
        use_base_weights: bool | None = None,
        group_index: int = 0,
    ) -> None:
        """Write one self-contained animation DAE for ``anim_model``.

        The DAE reuses the base model's group-0 geometry and skin (its
        joint contract) and adds ``library_animations``: one channel per
        joint with keyframes at MegaGlest animation times. ``anim_model``
        supplies the per-frame poses; its vertices are assigned to the rig's
        clusters by rest proximity (``fit_group_frames``). Keyframe spacing
        encodes ``anim-speed`` (100 / speed seconds per cycle, loop-closed).

        ``group_index`` selects which rig group's geometry/weights to use
        (default 0 for the base actor; non-zero for prop groups whose
        texture-split DAEs also need animation data).
        """
        root = _new_collada_root()
        _add_asset(root, civ)
        base = _sanitize_id(path.stem)
        gid = f"{base}-g0"
        group = rig.groups[group_index]
        meshes = [base_model.meshes[i] for i in group.mesh_indices]
        _add_merged_geometry(root, gid, meshes)
        # Fit before writing the skin: the fit decides which weights the bones
        # were solved against, and those are the weights the skin must store
        # or the file cannot reproduce its own animation.
        frames, vertex_weights = fit_group_frames(
            base_model,
            anim_model,
            rig,
            group_index=group_index,
            use_base_weights=(
                anim_model is base_model if use_base_weights is None else use_base_weights
            ),
        )
        _add_skin(root, gid, rig, group_index, vertex_weights=vertex_weights)
        _add_animations(
            root,
            gid,
            rig,
            frames,
            anim_speed,
            loop,
        )
        _add_skinned_scene(root, gid, rig)
        _add_scene(root)
        _write_xml(root, path)

    def _write_static_dae(
        self,
        model: g3dlib.G3DModel,
        group_index: int,
        indices: list[int],
        path: Path,
        base: str,
        civ: str,
    ) -> None:
        meshes = [model.meshes[i] for i in indices]
        root = _new_collada_root()
        _add_asset(root, civ)
        if len(meshes) == 1:
            _add_mesh_geometry(root, base, indices[0], meshes[0])
            root.append(_visual_scene(base, indices[0], meshes[0]))
        else:
            # Same-texture subobjects merge into one geometry so the file
            # keeps exactly one instanced object (importer contract).
            gid = f"{base}-g{group_index}"
            _add_merged_geometry(root, gid, meshes)
            root.append(_static_merged_scene(gid))
        _add_scene(root)
        _write_xml(root, path)

    def _write_skinned_dae(
        self,
        model: g3dlib.G3DModel,
        group_index: int,
        path: Path,
        base: str,
        civ: str,
        rig: Rig,
    ) -> None:
        group = rig.groups[group_index]
        meshes = [model.meshes[i] for i in group.mesh_indices]
        root = _new_collada_root()
        _add_asset(root, civ)
        gid = f"{base}-g{group_index}"
        _add_merged_geometry(root, gid, meshes)
        _add_skin(root, gid, rig, group_index)
        _add_skinned_scene(root, gid, rig)
        _add_scene(root)
        _write_xml(root, path)


# ---------------------------------------------------------------------------
# COLLADA element helpers
# ---------------------------------------------------------------------------


def _tag(name: str) -> str:
    return f"{{{_COLLADA_NS}}}{name}"


def _new_collada_root() -> etree._Element:
    return etree.Element(
        _tag("COLLADA"),
        nsmap={None: _COLLADA_NS},
        attrib={"version": "1.4.1"},
    )


def _add_asset(root: etree._Element, civ: str) -> None:
    now = datetime.now(UTC).strftime(_ISO_FORMAT)
    asset = etree.SubElement(root, _tag("asset"))
    contributor = etree.SubElement(asset, _tag("contributor"))
    etree.SubElement(contributor, _tag("author")).text = f"megaglest-to-pyrogenesis ({civ})"
    etree.SubElement(contributor, _tag("authoring_tool")).text = "megaglest to pyrogenesis"
    etree.SubElement(asset, _tag("created")).text = now
    etree.SubElement(asset, _tag("modified")).text = now
    unit = etree.SubElement(asset, _tag("unit"))
    unit.set("name", "meter")
    unit.set("meter", "1")
    etree.SubElement(asset, _tag("up_axis")).text = "Y_UP"


def _add_scene(root: etree._Element) -> None:
    scene = etree.SubElement(root, _tag("scene"))
    instance = etree.SubElement(scene, _tag("instance_visual_scene"))
    instance.set("url", "#Scene")


def _source(
    parent: etree._Element,
    sid: str,
    values: list[float],
    stride: int,
    params: list[tuple[str, str]],
) -> etree._Element:
    source = etree.SubElement(parent, _tag("source"))
    source.set("id", sid)
    array = etree.SubElement(source, _tag("float_array"))
    array.set("id", f"{sid}-array")
    array.set("count", str(len(values)))
    array.text = " ".join(_fmt(v) for v in values)
    technique = etree.SubElement(source, _tag("technique_common"))
    accessor = etree.SubElement(technique, _tag("accessor"))
    accessor.set("source", f"#{sid}-array")
    accessor.set("count", str(len(values) // stride))
    accessor.set("stride", str(stride))
    for name, type_ in params:
        param = etree.SubElement(accessor, _tag("param"))
        param.set("name", name)
        param.set("type", type_)
    return source


def _append_geometries(root: etree._Element, geometry: etree._Element) -> None:
    """Attach ``geometry`` to the shared library_geometries container."""
    library = root.find(_tag("library_geometries"))
    if library is None:
        library = etree.Element(_tag("library_geometries"))
        root.append(library)
    library.append(geometry)


def _add_mesh_geometry(
    root: etree._Element,
    base: str,
    index: int,
    mesh: g3dlib.Mesh,
) -> None:
    """Base-pose geometry for one G3D mesh (frame 0, normalized normals).

    Geometry only: 0 A.D.'s importer reads POSITION/NORMAL/TEXCOORD and never
    touches materials, so none are emitted (actors declare textures).
    """
    gid = f"{base}-m{index}"
    geometry = etree.Element(_tag("geometry"))
    geometry.set("id", gid)
    if mesh.name:
        geometry.set("name", mesh.name)
    mesh_el = etree.SubElement(geometry, _tag("mesh"))

    _source(
        mesh_el,
        f"{gid}-positions",
        _frame_positions(mesh, 0),
        3,
        [("X", "float"), ("Y", "float"), ("Z", "float")],
    )
    _source(
        mesh_el,
        f"{gid}-normals",
        _frame_normals(mesh, 0),
        3,
        [("X", "float"), ("Y", "float"), ("Z", "float")],
    )
    _source(
        mesh_el,
        f"{gid}-texcoords",
        _mesh_uvs(mesh),
        2,
        [("S", "float"), ("T", "float")],
    )
    # COLLADA schema order: all <source> elements, then <vertices>, then
    # primitives (Khronos 1.4.1 XSD; FCollada tolerates any order).
    vertices = etree.SubElement(mesh_el, _tag("vertices"))
    vertices.set("id", f"{gid}-vertices")
    pos_input = etree.SubElement(vertices, _tag("input"))
    pos_input.set("semantic", "POSITION")
    pos_input.set("source", f"#{gid}-positions")

    n_triangles = len(mesh.indices) // 3
    if n_triangles:
        triangles = etree.SubElement(mesh_el, _tag("triangles"))
        triangles.set("count", str(n_triangles))
        vert_ref = etree.SubElement(triangles, _tag("input"))
        vert_ref.set("semantic", "VERTEX")
        vert_ref.set("source", f"#{gid}-vertices")
        vert_ref.set("offset", "0")
        normal_ref = etree.SubElement(triangles, _tag("input"))
        normal_ref.set("semantic", "NORMAL")
        normal_ref.set("source", f"#{gid}-normals")
        normal_ref.set("offset", "1")
        uv_ref = etree.SubElement(triangles, _tag("input"))
        uv_ref.set("semantic", "TEXCOORD")
        uv_ref.set("source", f"#{gid}-texcoords")
        uv_ref.set("offset", "2")
        uv_ref.set("set", "0")
        # G3D front faces and normals are self-consistent (right-hand rule),
        # matching 0 A.D.'s Blender/COLLADA convention; export indices verbatim.
        p = etree.SubElement(triangles, _tag("p"))
        p.text = " ".join(f"{i} {i} {i}" for i in mesh.indices[: n_triangles * 3])

    _add_double_sided_extra(geometry, mesh)
    _append_geometries(root, geometry)


def _mesh_uvs(mesh: g3dlib.Mesh) -> list[float]:
    """UV data for a mesh; UV-less meshes get synthetic all-zero TEXCOORD.

    0 A.D.'s importer REQUIRES POSITION/NORMAL/TEXCOORD on the polygons
    (PMDConvert.cpp GetPolysFromGeometry) and fails otherwise.
    """
    if mesh.tex_coords:
        return mesh.tex_coords
    return [0.0, 0.0] * mesh.vertex_count


def _add_merged_geometry(
    root: etree._Element,
    gid: str,
    meshes: list[g3dlib.Mesh],
) -> None:
    """Merged base-pose geometry for a texture group (frame 0 each mesh).

    Vertices concatenate in mesh order (matching the rig's vertex weights);
    indices are offset per mesh and exported in G3D order (right-hand rule).
    """
    geometry = etree.Element(_tag("geometry"))
    geometry.set("id", gid)
    geometry.set("name", gid)
    mesh_el = etree.SubElement(geometry, _tag("mesh"))

    positions: list[float] = []
    normals: list[float] = []
    uvs: list[float] = []
    indices: list[int] = []
    for mesh in meshes:
        offset = len(positions) // 3
        positions.extend(_frame_positions(mesh, 0))
        normals.extend(_frame_normals(mesh, 0))
        uvs.extend(_mesh_uvs(mesh))
        indices.extend(i + offset for i in mesh.indices)

    _source(
        mesh_el,
        f"{gid}-positions",
        positions,
        3,
        [("X", "float"), ("Y", "float"), ("Z", "float")],
    )
    _source(
        mesh_el,
        f"{gid}-normals",
        normals,
        3,
        [("X", "float"), ("Y", "float"), ("Z", "float")],
    )
    _source(
        mesh_el,
        f"{gid}-texcoords",
        uvs,
        2,
        [("S", "float"), ("T", "float")],
    )
    # COLLADA schema order: all <source> elements, then <vertices>, then
    # primitives (Khronos 1.4.1 XSD; FCollada tolerates any order).
    vertices = etree.SubElement(mesh_el, _tag("vertices"))
    vertices.set("id", f"{gid}-vertices")
    pos_input = etree.SubElement(vertices, _tag("input"))
    pos_input.set("semantic", "POSITION")
    pos_input.set("source", f"#{gid}-positions")

    n_triangles = len(indices) // 3
    if n_triangles:
        triangles = etree.SubElement(mesh_el, _tag("triangles"))
        triangles.set("count", str(n_triangles))
        for offset, semantic, source in (
            (0, "VERTEX", f"#{gid}-vertices"),
            (1, "NORMAL", f"#{gid}-normals"),
            (2, "TEXCOORD", f"#{gid}-texcoords"),
        ):
            ref = etree.SubElement(triangles, _tag("input"))
            ref.set("semantic", semantic)
            ref.set("source", source)
            ref.set("offset", str(offset))
            if semantic == "TEXCOORD":
                ref.set("set", "0")
        p = etree.SubElement(triangles, _tag("p"))
        p.text = " ".join(f"{i} {i} {i}" for i in indices)

    if any(m.two_sided for m in meshes):
        extra = etree.SubElement(geometry, _tag("extra"))
        technique = etree.SubElement(extra, _tag("technique"))
        technique.set("profile", "MAYA")
        etree.SubElement(technique, _tag("double_sided")).text = "1"
    _append_geometries(root, geometry)


def _add_double_sided_extra(geometry: etree._Element, mesh: g3dlib.Mesh) -> None:
    """Record two-sidedness as geometry metadata (Phase 4 material concern)."""
    if mesh.two_sided:
        extra = etree.SubElement(geometry, _tag("extra"))
        technique = etree.SubElement(extra, _tag("technique"))
        technique.set("profile", "MAYA")
        etree.SubElement(technique, _tag("double_sided")).text = "1"


def _add_skin(
    root: etree._Element,
    gid: str,
    rig: Rig,
    group_index: int = 0,
    vertex_weights: list[list[tuple[int, float]]] | None = None,
) -> None:
    """Skin controller: identity bind pose + the rig's joint weights.

    The bind shape and every joint bind matrix are identity, so the PMD's
    bind pose is identity and runtime skinning (animated pose x inverse
    bind) applies the animation transforms directly. Weights are the rig's
    per-vertex top-4 influences, normalized -- or ``vertex_weights`` when the
    caller solved the bone fits against a different set (a foreign animation
    reweights the rig's clusters; the file has to hold the fitted ones).
    """
    if vertex_weights is None:
        vertex_weights = rig.groups[group_index].vertex_weights

    library = root.find(_tag("library_controllers"))
    if library is None:
        library = etree.Element(_tag("library_controllers"))
        root.append(library)
    controller = etree.SubElement(library, _tag("controller"))
    controller.set("id", f"{gid}-skin")
    skin = etree.SubElement(controller, _tag("skin"))
    skin.set("source", f"#{gid}")

    bind = etree.SubElement(skin, _tag("bind_shape_matrix"))
    bind.text = " ".join(_fmt(v) for v in _IDENTITY4)

    joints = etree.SubElement(skin, _tag("source"))
    joints.set("id", f"{gid}-joints")
    name_array = etree.SubElement(joints, _tag("Name_array"))
    name_array.set("id", f"{gid}-joints-array")
    name_array.set("count", str(rig.bone_count))
    name_array.text = " ".join(rig.bone_names)
    technique = etree.SubElement(joints, _tag("technique_common"))
    accessor = etree.SubElement(technique, _tag("accessor"))
    accessor.set("source", f"#{gid}-joints-array")
    accessor.set("count", str(rig.bone_count))
    accessor.set("stride", "1")
    param = etree.SubElement(accessor, _tag("param"))
    param.set("name", "JOINT")
    param.set("type", "name")

    _source(
        skin,
        f"{gid}-bind_poses",
        list(_IDENTITY4) * rig.bone_count,
        16,
        [("TRANSFORM", "float4x4")],
    )

    weights: list[float] = []
    vcounts: list[int] = []
    vertex_pairs: list[int] = []
    for vertex_weights_row in vertex_weights:
        vcounts.append(len(vertex_weights_row))
        for bone, weight in vertex_weights_row:
            weights.append(weight)
            # Cluster b occupies bone slot b+1 (slot 0 is the root joint).
            vertex_pairs.append(bone + 1)
            vertex_pairs.append(len(weights) - 1)
    _source(
        skin,
        f"{gid}-weights",
        weights,
        1,
        [("WEIGHT", "float")],
    )

    joints_input = etree.SubElement(skin, _tag("joints"))
    joint_ref = etree.SubElement(joints_input, _tag("input"))
    joint_ref.set("semantic", "JOINT")
    joint_ref.set("source", f"#{gid}-joints")
    bind_ref = etree.SubElement(joints_input, _tag("input"))
    bind_ref.set("semantic", "INV_BIND_MATRIX")
    bind_ref.set("source", f"#{gid}-bind_poses")

    vertex_weights = etree.SubElement(skin, _tag("vertex_weights"))
    vertex_weights.set("count", str(len(vcounts)))
    joint_input = etree.SubElement(vertex_weights, _tag("input"))
    joint_input.set("semantic", "JOINT")
    joint_input.set("source", f"#{gid}-joints")
    joint_input.set("offset", "0")
    weight_input = etree.SubElement(vertex_weights, _tag("input"))
    weight_input.set("semantic", "WEIGHT")
    weight_input.set("source", f"#{gid}-weights")
    weight_input.set("offset", "1")
    vcount_el = etree.SubElement(vertex_weights, _tag("vcount"))
    vcount_el.text = " ".join(str(c) for c in vcounts)
    v_el = etree.SubElement(vertex_weights, _tag("v"))
    v_el.text = " ".join(str(i) for i in vertex_pairs)


def _visual_scene(base: str, index: int, mesh: g3dlib.Mesh) -> etree._Element:
    """One scene with exactly one object node (0 A.D. importer contract)."""
    scene = etree.Element(_tag("library_visual_scenes"))
    visual = etree.SubElement(scene, _tag("visual_scene"))
    visual.set("id", "Scene")
    node = etree.SubElement(visual, _tag("node"))
    node.set("id", f"{base}-m{index}-node")
    if mesh.name:
        node.set("name", mesh.name)
    instance = etree.SubElement(node, _tag("instance_geometry"))
    instance.set("url", f"#{base}-m{index}")
    return scene


def _static_merged_scene(gid: str) -> etree._Element:
    """One scene with one object node instancing a merged static geometry."""
    scene = etree.Element(_tag("library_visual_scenes"))
    visual = etree.SubElement(scene, _tag("visual_scene"))
    visual.set("id", "Scene")
    node = etree.SubElement(visual, _tag("node"))
    node.set("id", f"{gid}-node")
    node.set("name", gid)
    instance = etree.SubElement(node, _tag("instance_geometry"))
    instance.set("url", f"#{gid}")
    return scene


def _add_skinned_scene(root: etree._Element, gid: str, rig: Rig) -> None:
    """Armature (root joint + part joints) and one instanced controller.

    Joint 0 is the root joint; its name matches the mapped skeleton's
    ``<identifier><root>`` so ``FindSkeleton`` recognizes the hierarchy
    immediately. Every joint's bind matrix is identity; the skin's joint
    order equals ``rig.bone_names`` so PMD bone IDs align with PSA keys.
    """
    scene = etree.Element(_tag("library_visual_scenes"))
    visual = etree.SubElement(scene, _tag("visual_scene"))
    visual.set("id", "Scene")
    armature = etree.SubElement(visual, _tag("node"))
    armature.set("id", rig.root_name)
    armature.set("name", rig.root_name)
    armature.set("sid", rig.root_name)
    armature.set("type", "NODE")
    _identity_matrix(armature)
    for bone in rig.bone_names[1:]:
        joint = etree.SubElement(armature, _tag("node"))
        joint.set("id", bone)
        joint.set("name", bone)
        joint.set("sid", bone)
        joint.set("type", "JOINT")
        _identity_matrix(joint)

    node = etree.SubElement(visual, _tag("node"))
    node.set("id", f"{gid}-node")
    node.set("name", gid)
    instance = etree.SubElement(node, _tag("instance_controller"))
    instance.set("url", f"#{gid}-skin")
    skeleton = etree.SubElement(instance, _tag("skeleton"))
    skeleton.text = f"#{rig.root_name}"
    root.append(scene)


def _identity_matrix(parent: etree._Element) -> None:
    matrix = etree.SubElement(parent, _tag("matrix"))
    matrix.set("sid", "transform")
    matrix.text = " ".join(_fmt(v) for v in _IDENTITY4)


def _add_animations(
    root: etree._Element,
    gid: str,
    rig: Rig,
    frames: list[list[tuple[list[float], list[float]]]],
    anim_speed: float,
    loop: bool,
) -> None:
    """One <animation> per joint: time-sampled world transform channels.

    Keyframe times spread the model's frames over one MegaGlest animation
    cycle (``100 / anim_speed`` seconds); looping animations append a
    closing keyframe with the rest pose so the PSA wraps seamlessly.
    Transforms are the per-bone rigid fits from ``fit_group_frames``, which
    the caller has already solved against the weights written by
    ``_add_skin``.
    """
    frame_count = len(frames)
    duration = animation_duration(anim_speed)

    times: list[float] = []
    for f in range(frame_count):
        times.append(f * duration / max(frame_count - 1, 1))
    if loop and frame_count > 1:
        times.append(duration)

    library = root.find(_tag("library_animations"))
    if library is None:
        library = etree.Element(_tag("library_animations"))
        root.append(library)

    for bone_index, bone in enumerate(rig.bone_names):
        matrices: list[float] = []
        for f in range(frame_count):
            if bone_index == 0:
                # The armature root is the parent of every bone; the engine
                # composes world = root * local. The fits below are ABSOLUTE
                # rigid transforms per cluster, so the root must stay frozen
                # at identity - otherwise its whole-model fit is applied
                # twice (root * fit_b).
                matrices.extend(_IDENTITY4)
                continue
            rotation, translation = frames[f][bone_index]
            # COLLADA float4x4 values are column-major (FCollada reads the
            # translation from floats 3/7/11, i.e. the last column). A
            # row-major layout silently drops the translation: the engine
            # would render the rotation only.
            matrices.extend(
                [
                    rotation[0],
                    rotation[1],
                    rotation[2],
                    translation[0],
                    rotation[3],
                    rotation[4],
                    rotation[5],
                    translation[1],
                    rotation[6],
                    rotation[7],
                    rotation[8],
                    translation[2],
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                ]
            )
        if loop and frame_count > 1:
            matrices.extend(_IDENTITY4)

        anim = etree.SubElement(library, _tag("animation"))
        anim.set("id", f"{gid}-{bone}-anim")
        _source(anim, f"{gid}-{bone}-input", times, 1, [("TIME", "float")])
        _source(
            anim,
            f"{gid}-{bone}-output",
            matrices,
            16,
            [("TRANSFORM", "float4x4")],
        )
        interp = etree.SubElement(anim, _tag("source"))
        interp.set("id", f"{gid}-{bone}-interpolation")
        interp_array = etree.SubElement(interp, _tag("Name_array"))
        interp_array.set("id", f"{gid}-{bone}-interpolation-array")
        interp_array.set("count", str(len(times)))
        interp_array.text = " ".join("LINEAR" for _ in times)
        technique = etree.SubElement(interp, _tag("technique_common"))
        accessor = etree.SubElement(technique, _tag("accessor"))
        accessor.set("source", f"#{gid}-{bone}-interpolation-array")
        accessor.set("count", str(len(times)))
        accessor.set("stride", "1")
        param = etree.SubElement(accessor, _tag("param"))
        param.set("name", "INTERPOLATION")
        param.set("type", "name")

        sampler = etree.SubElement(anim, _tag("sampler"))
        sampler.set("id", f"{gid}-{bone}-sampler")
        for semantic, source in (
            ("INPUT", f"#{gid}-{bone}-input"),
            ("OUTPUT", f"#{gid}-{bone}-output"),
            ("INTERPOLATION", f"#{gid}-{bone}-interpolation"),
        ):
            input_el = etree.SubElement(sampler, _tag("input"))
            input_el.set("semantic", semantic)
            input_el.set("source", source)
        channel = etree.SubElement(anim, _tag("channel"))
        channel.set("source", f"#{gid}-{bone}-sampler")
        channel.set("target", f"{bone}/transform")

def _mesh_variant_name(stem: str, index: int, multi: bool) -> str:
    """Output basename for one mesh of a model.

    Numbered variants use zero-padded two-digit suffixes (``_01``, ``_02``),
    never bare ``_1``: the project's mesh-naming rule. Single-mesh models
    keep the bare stem.
    """
    if not multi:
        return stem
    return f"{stem}_{index + 1:02d}"


# -- geometry helpers -------------------------------------------------------


def _frame_positions(mesh: g3dlib.Mesh, frame: int) -> list[float]:
    """Positions of one frame: vc x 3 floats."""
    stride = mesh.vertex_count * 3
    return mesh.vertices[frame * stride : (frame + 1) * stride]


def _model_footprint(model: g3dlib.G3DModel) -> tuple[float, float, float]:
    """(width, depth, height) of the frame-0 bounding box, in model units.

    DAEs are exported Y-up with vertices verbatim, so the ground footprint is
    the X extent (width) x Z extent (depth) and Y is the height. All meshes
    contribute; degenerate axes are floored at 0.5 (a zero-width footprint
    breaks the engine's placement code).
    """
    lo = [1e30, 1e30, 1e30]
    hi = [-1e30, -1e30, -1e30]
    for mesh in model.meshes:
        stride = mesh.vertex_count * 3
        vertices = mesh.vertices[:stride]
        for i in range(0, stride, 3):
            for axis in range(3):
                value = vertices[i + axis]
                if value < lo[axis]:
                    lo[axis] = value
                if value > hi[axis]:
                    hi[axis] = value
    return tuple(max(0.5, hi[a] - lo[a]) for a in (0, 2, 1))  # X, Z, Y


def _frame_normals(mesh: g3dlib.Mesh, frame: int) -> list[float]:
    """Normals of one frame, normalized (G3D may store unnormalized)."""
    stride = mesh.vertex_count * 3
    values = mesh.normals[frame * stride : (frame + 1) * stride]
    return _normalize(values)


def _normalize(values: list[float]) -> list[float]:
    out = list(values)
    for i in range(0, len(out), 3):
        x, y, z = out[i], out[i + 1], out[i + 2]
        length = math.sqrt(x * x + y * y + z * z)
        if length > 1e-9:
            out[i] = x / length
            out[i + 1] = y / length
            out[i + 2] = z / length
    return out


def _write_xml(root: etree._Element, path: Path) -> None:
    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    path.write_bytes(data)
