"""Read/write the Glest G3D v4 binary format.

Format reference: megaglest source/tools/glexemel/g3dv4.h and g3d_support_b290.py.
Round-trips byte-identical for v4 files when no data is modified.

Upstream: https://github.com/MegaGlest/megaglest-source
  file: source/tools/glexemel/g3dlib.py (develop branch)
License: GPL-3.0 (see LICENSE in this directory); upstream is GPL-2.0-or-later
  with the GPL-3.0-or-later option, so this vendored copy is GPL-3.0.

Patches applied on top of upstream (marked with "PATCH" below):
  - PROP_ALPHA_IS_TRANSPARENCY flag added to match model_header.h.
  - read_stream gained ``tolerate_truncation``: upstream raises on any short
    read. Some packs in the wild ship files whose trailing mesh data is cut
    off at EOF; with tolerance enabled the final mesh of the file is clamped
    to whatever bytes remain and ``G3DModel.truncated`` is set.
  - The mesh reader was split into header + data so the data section can be
    read length-checked.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import BinaryIO

NAMESIZE = 64

PROP_CUSTOM_COLOR = 1
PROP_TWO_SIDED = 2
PROP_NO_SELECT = 4
PROP_GLOW = 8
# PATCH: flag from shared/graphics/model_header.h (upstream g3dlib omits it).
PROP_ALPHA_IS_TRANSPARENCY = 16

TEX_DIFFUSE = 1
TEX_SPECULAR = 2
TEX_NORMAL = 4


def _decode_name(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _encode_name(name: str) -> bytes:
    encoded = name.encode("ascii")
    if len(encoded) >= NAMESIZE:
        raise ValueError(f"name {name!r} too long ({len(encoded)} >= {NAMESIZE})")
    return encoded + b"\x00" * (NAMESIZE - len(encoded))


@dataclass
class Mesh:
    name: str
    frame_count: int
    vertex_count: int
    index_count: int
    diffuse_color: tuple[float, float, float]
    specular_color: tuple[float, float, float]
    specular_power: float
    opacity: float
    properties: int
    textures: int
    texture_names: list[tuple[int, str]]  # (slot_bit, raw_name_decoded)
    # raw_name is decoded with nulls trimmed; on write we re-pad.
    # For exact byte preservation of trailing garbage in name fields,
    # we also keep the original padded bytes.
    _raw_mesh_name: bytes = b""
    _raw_texture_names: list[tuple[int, bytes]] = field(default_factory=list)

    # Geometry data
    vertices: list[float] = field(default_factory=list)  # frame_count * vertex_count * 3
    normals: list[float] = field(default_factory=list)  # frame_count * vertex_count * 3
    tex_coords: list[float] = field(default_factory=list)  # vertex_count * 2 (if diffuseTexture)
    indices: list[int] = field(default_factory=list)  # index_count

    # PATCH: set when the on-disk index array was shorter than index_count
    # (only possible when reading with tolerate_truncation=True).
    truncated: bool = False

    @property
    def has_diffuse_texture(self) -> bool:
        return bool(self.textures & TEX_DIFFUSE)

    @property
    def two_sided(self) -> bool:
        return bool(self.properties & PROP_TWO_SIDED)

    @property
    def custom_color(self) -> bool:
        return bool(self.properties & PROP_CUSTOM_COLOR)


@dataclass
class G3DModel:
    version: int
    model_type: int
    meshes: list[Mesh]

    # PATCH: True when tolerate_truncation recovered a short final mesh.
    truncated: bool = False
    truncated_mesh_names: list[str] = field(default_factory=list)

    @classmethod
    def read(cls, path: str) -> G3DModel:
        with open(path, "rb") as f:
            return cls.read_stream(f)

    @classmethod
    def read_stream(cls, f: BinaryIO, *, tolerate_truncation: bool = False) -> G3DModel:
        try:
            return cls._read_stream(f)
        except ValueError:
            if not tolerate_truncation:
                raise
            f.seek(0)
            return cls._read_stream_tolerant(f)

    @classmethod
    def _read_stream(cls, f: BinaryIO) -> G3DModel:
        magic = f.read(3)
        if magic != b"G3D":
            raise ValueError(f"not a G3D file (magic={magic!r})")
        (version,) = struct.unpack("<B", f.read(1))
        if version != 4:
            raise ValueError(f"unsupported G3D version {version}; only v4 supported")
        mesh_count, model_type = struct.unpack("<HB", f.read(3))
        if model_type != 0:
            raise ValueError(f"unsupported model type {model_type}")

        meshes = []
        for _ in range(mesh_count):
            meshes.append(_read_mesh(f))
        return cls(version=version, model_type=model_type, meshes=meshes)

    @classmethod
    def _read_stream_tolerant(cls, f: BinaryIO) -> G3DModel:
        """Length-checked read; only the final mesh's index array may be short.

        A short read in any earlier array is genuine corruption (sequential
        layout makes it unrecoverable) and raises.
        """
        magic = f.read(3)
        if magic != b"G3D":
            raise ValueError(f"not a G3D file (magic={magic!r})")
        (version,) = struct.unpack("<B", f.read(1))
        if version != 4:
            raise ValueError(f"unsupported G3D version {version}; only v4 supported")
        mesh_count, model_type = struct.unpack("<HB", f.read(3))
        if model_type != 0:
            raise ValueError(f"unsupported model type {model_type}")

        meshes: list[Mesh] = []
        for mesh_index in range(mesh_count):
            mesh = _read_mesh_header(f)
            _read_mesh_data(f, mesh, tolerate_final=mesh_index == mesh_count - 1)
            meshes.append(mesh)

        model = cls(version=version, model_type=model_type, meshes=meshes)
        model.truncated = any(mesh.truncated for mesh in meshes)
        model.truncated_mesh_names = [mesh.name for mesh in meshes if mesh.truncated]
        return model

    def write(self, path: str) -> None:
        with open(path, "wb") as f:
            self.write_stream(f)

    def write_stream(self, f: BinaryIO) -> None:
        f.write(b"G3D")
        f.write(struct.pack("<B", self.version))
        f.write(struct.pack("<HB", len(self.meshes), self.model_type))
        for mesh in self.meshes:
            _write_mesh(f, mesh)


_MESH_HEADER_FMT = "<64s3I8f2I"
_MESH_HEADER_SIZE = struct.calcsize(_MESH_HEADER_FMT)


# PATCH: header read split out of _read_mesh so the tolerant path can reuse it.
def _read_mesh_header(f: BinaryIO) -> Mesh:
    raw_header = f.read(_MESH_HEADER_SIZE)
    if len(raw_header) != _MESH_HEADER_SIZE:
        raise ValueError("truncated mesh header")
    fields = struct.unpack(_MESH_HEADER_FMT, raw_header)
    raw_name = fields[0]
    frame_count, vertex_count, index_count = fields[1], fields[2], fields[3]
    diffuse = (fields[4], fields[5], fields[6])
    specular = (fields[7], fields[8], fields[9])
    specular_power = fields[10]
    opacity = fields[11]
    properties = fields[12]
    textures = fields[13]

    texture_names: list[tuple[int, str]] = []
    raw_texture_names: list[tuple[int, bytes]] = []
    tex = textures
    # Iterate bit by bit from LSB; for each set bit, read a 64-byte name.
    bit_pos = 0
    while tex:
        if tex & 1:
            raw_tex = f.read(NAMESIZE)
            if len(raw_tex) != NAMESIZE:
                raise ValueError("truncated texture name")
            slot_bit = 1 << bit_pos
            texture_names.append((slot_bit, _decode_name(raw_tex)))
            raw_texture_names.append((slot_bit, raw_tex))
        tex >>= 1
        bit_pos += 1

    return Mesh(
        name=_decode_name(raw_name),
        frame_count=frame_count,
        vertex_count=vertex_count,
        index_count=index_count,
        diffuse_color=diffuse,
        specular_color=specular,
        specular_power=specular_power,
        opacity=opacity,
        properties=properties,
        textures=textures,
        texture_names=texture_names,
        _raw_mesh_name=raw_name,
        _raw_texture_names=raw_texture_names,
    )


# PATCH: data read split out of _read_mesh so the tolerant path can reuse it.
def _read_mesh_data(f: BinaryIO, mesh: Mesh, *, tolerate_final: bool = False) -> None:
    floats_per_frame = mesh.vertex_count * 3
    n_vert_floats = mesh.frame_count * floats_per_frame
    n_norm_floats = mesh.frame_count * floats_per_frame

    raw = f.read(4 * n_vert_floats)
    if len(raw) != 4 * n_vert_floats:
        raise ValueError(f"truncated vertex array for mesh {mesh.name!r}")
    mesh.vertices = list(struct.unpack(f"<{n_vert_floats}f", raw))

    raw = f.read(4 * n_norm_floats)
    if len(raw) != 4 * n_norm_floats:
        raise ValueError(f"truncated normal array for mesh {mesh.name!r}")
    mesh.normals = list(struct.unpack(f"<{n_norm_floats}f", raw))

    if mesh.textures & TEX_DIFFUSE:
        n_tex_floats = mesh.vertex_count * 2
        raw = f.read(4 * n_tex_floats)
        if len(raw) != 4 * n_tex_floats:
            raise ValueError(f"truncated tex-coord array for mesh {mesh.name!r}")
        mesh.tex_coords = list(struct.unpack(f"<{n_tex_floats}f", raw))

    raw_indices = f.read(4 * mesh.index_count)
    n_present = len(raw_indices) // 4
    if n_present != mesh.index_count:
        if not tolerate_final:
            raise ValueError(
                f"truncated index array for mesh {mesh.name!r}: "
                f"{n_present} of {mesh.index_count} present"
            )
        mesh.truncated = True
    mesh.indices = list(struct.unpack(f"<{n_present}I", raw_indices[: 4 * n_present]))


def _read_mesh(f: BinaryIO) -> Mesh:
    mesh = _read_mesh_header(f)
    _read_mesh_data(f, mesh)
    return mesh


def _write_mesh(f: BinaryIO, mesh: Mesh) -> None:
    # Preserve the original raw name padding when possible (some files have
    # non-null garbage after the terminator).
    if mesh._raw_mesh_name and _decode_name(mesh._raw_mesh_name) == mesh.name:
        name_bytes = mesh._raw_mesh_name
    else:
        name_bytes = _encode_name(mesh.name)

    header = struct.pack(
        _MESH_HEADER_FMT,
        name_bytes,
        mesh.frame_count,
        mesh.vertex_count,
        mesh.index_count,
        mesh.diffuse_color[0],
        mesh.diffuse_color[1],
        mesh.diffuse_color[2],
        mesh.specular_color[0],
        mesh.specular_color[1],
        mesh.specular_color[2],
        mesh.specular_power,
        mesh.opacity,
        mesh.properties,
        mesh.textures,
    )
    f.write(header)

    raw_by_slot = {slot: raw for slot, raw in mesh._raw_texture_names}
    for slot_bit, decoded in mesh.texture_names:
        raw = raw_by_slot.get(slot_bit)
        if raw is not None and _decode_name(raw) == decoded:
            f.write(raw)
        else:
            f.write(_encode_name(decoded))

    n_vert = mesh.frame_count * mesh.vertex_count * 3
    n_norm = mesh.frame_count * mesh.vertex_count * 3
    if len(mesh.vertices) != n_vert:
        raise ValueError(f"vertex array size mismatch: {len(mesh.vertices)} != {n_vert}")
    if len(mesh.normals) != n_norm:
        raise ValueError(f"normal array size mismatch: {len(mesh.normals)} != {n_norm}")
    f.write(struct.pack(f"<{n_vert}f", *mesh.vertices))
    f.write(struct.pack(f"<{n_norm}f", *mesh.normals))

    if mesh.textures & TEX_DIFFUSE:
        n_tex = mesh.vertex_count * 2
        if len(mesh.tex_coords) != n_tex:
            raise ValueError(f"tex_coords size mismatch: {len(mesh.tex_coords)} != {n_tex}")
        f.write(struct.pack(f"<{n_tex}f", *mesh.tex_coords))

    if len(mesh.indices) != mesh.index_count:
        raise ValueError(f"index array size mismatch: {len(mesh.indices)} != {mesh.index_count}")
    f.write(struct.pack(f"<{mesh.index_count}I", *mesh.indices))
