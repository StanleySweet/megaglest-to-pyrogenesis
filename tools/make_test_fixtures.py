"""Generate every binary file under tests/fixtures from code.

The repository ships no third-party game assets. Each .g3d, .png, .tga, .bmp,
.wav and .ogg under tests/fixtures is produced by this script, so the test suite
carries no pack data. Run it from anywhere:

    python tools/make_test_fixtures.py

Output is byte-deterministic, which is what lets
tests/test_fixture_assets.py regenerate into a temp dir and byte-compare with
the committed files.

The G3D writer reuses vendor/g3d/g3dlib.py, which reads and writes v4. The v3
layout has no writer in the tree, so it is packed here against the reader in
src/megaglest_to_0ad/converters/mesh_converter.py.
"""

from __future__ import annotations

import io
import math
import struct
import sys
import wave
import zlib
from collections.abc import Callable
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "vendor" / "g3d"))

import g3dlib  # noqa: E402

Shape = tuple[list[float], list[float], list[float], list[int]]

JUNK_MODEL = b"G3D\x03\x00\x00\x00"
OGG_STUB = b"OggS\x00\x02not-a-real-ogg-stream"

LAYOUT_A = "packs/layout_a/techs/demo_tech/factions/romans/units/legion"
demo = "packs/layout_b/factions/demo"


def box(cols: int, rows: int, size: float = 1.0, height: float = 1.0) -> Shape:
    """Cube with each face split into a cols x rows grid. Flat normals."""
    faces = (
        ((size, 0.0, 0.0), (0.0, 0.0, -size), (0.0, height, 0.0)),
        ((0.0, 0.0, size), (0.0, 0.0, size), (0.0, height, 0.0)),
        ((0.0, height, size), (size, 0.0, 0.0), (0.0, 0.0, -size)),
        ((0.0, 0.0, 0.0), (size, 0.0, 0.0), (0.0, 0.0, size)),
        ((0.0, 0.0, size), (size, 0.0, 0.0), (0.0, height, 0.0)),
        ((size, 0.0, 0.0), (-size, 0.0, 0.0), (0.0, height, 0.0)),
    )
    vertices: list[float] = []
    normals: list[float] = []
    tex_coords: list[float] = []
    indices: list[int] = []
    for origin, u_dir, v_dir in faces:
        normal = [
            u_dir[1] * v_dir[2] - u_dir[2] * v_dir[1],
            u_dir[2] * v_dir[0] - u_dir[0] * v_dir[2],
            u_dir[0] * v_dir[1] - u_dir[1] * v_dir[0],
        ]
        length = math.sqrt(sum(component * component for component in normal)) or 1.0
        normal = [component / length for component in normal]
        base = len(vertices) // 3
        for row in range(rows):
            for col in range(cols):
                u = col / (cols - 1)
                v = row / (rows - 1)
                vertices.extend(
                    origin[axis] + u * u_dir[axis] + v * v_dir[axis] for axis in range(3)
                )
                normals.extend(normal)
                tex_coords.extend((u, v))
        for row in range(rows - 1):
            for col in range(cols - 1):
                a = base + row * cols + col
                indices.extend((a, a + 1, a + cols + 1, a, a + cols + 1, a + cols))
    return vertices, normals, tex_coords, indices


def grid(cols: int, rows: int, size: float = 1.0) -> Shape:
    """Flat quad grid in the XY plane. All normals point +Z."""
    vertices: list[float] = []
    normals: list[float] = []
    tex_coords: list[float] = []
    indices: list[int] = []
    for row in range(rows):
        for col in range(cols):
            u = col / (cols - 1)
            v = row / (rows - 1)
            vertices.extend((u * size, v * size, 0.0))
            normals.extend((0.0, 0.0, 1.0))
            tex_coords.extend((u, v))
    for row in range(rows - 1):
        for col in range(cols - 1):
            a = row * cols + col
            indices.extend((a, a + 1, a + cols + 1, a, a + cols + 1, a + cols))
    return vertices, normals, tex_coords, indices


def merge(shapes: list[Shape], spacing: float = 0.75) -> Shape:
    """Join shapes into one mesh, offsetting each along X."""
    vertices: list[float] = []
    normals: list[float] = []
    tex_coords: list[float] = []
    indices: list[int] = []
    for index, (shape_verts, shape_normals, shape_uvs, shape_indices) in enumerate(shapes):
        offset = index * spacing
        base = len(vertices) // 3
        vertices.extend(
            value + (offset if axis == 0 else 0.0) for axis, value in enumerate(shape_verts)
        )
        normals.extend(shape_normals)
        tex_coords.extend(shape_uvs)
        indices.extend(base + value for value in shape_indices)
    return vertices, normals, tex_coords, indices


def bobbed(vertices: list[float], frame_count: int, amplitude: float = 0.05) -> list[float]:
    """Base pose repeated per frame with a smooth vertical offset, so rig
    synthesis sees real motion instead of N identical frames."""
    out: list[float] = []
    for frame in range(frame_count):
        offset = amplitude * math.sin(2 * math.pi * frame / frame_count)
        out.extend(
            value + (offset if axis == 1 else 0.0) for axis, value in enumerate(vertices)
        )
    return out


def mesh(
    name: str,
    shape: Shape,
    texture: str | None = None,
    frame_count: int = 1,
) -> g3dlib.Mesh:
    vertices, normals, tex_coords, indices = shape
    return g3dlib.Mesh(
        name=name,
        frame_count=frame_count,
        vertex_count=len(vertices) // 3,
        index_count=len(indices),
        diffuse_color=(1.0, 1.0, 1.0),
        specular_color=(0.0, 0.0, 0.0),
        specular_power=0.0,
        opacity=1.0,
        properties=0,
        textures=g3dlib.TEX_DIFFUSE if texture else 0,
        texture_names=[(g3dlib.TEX_DIFFUSE, texture)] if texture else [],
        vertices=bobbed(vertices, frame_count),
        normals=normals * frame_count,
        tex_coords=tex_coords if texture else [],
        indices=indices,
    )


def name64(name: str) -> bytes:
    encoded = name.encode("ascii")
    if len(encoded) >= g3dlib.NAMESIZE:
        raise ValueError(f"name too long: {name!r}")
    return encoded + b"\x00" * (g3dlib.NAMESIZE - len(encoded))


def v4_model(meshes: list[g3dlib.Mesh]) -> bytes:
    buffer = io.BytesIO()
    g3dlib.G3DModel(version=4, model_type=0, meshes=meshes).write_stream(buffer)
    return buffer.getvalue()


def v3_model(meshes: list[g3dlib.Mesh]) -> bytes:
    """Pack the v3 layout: counts, 64-byte texture name, vertices, normals,
    tex coords, diffuse colour, opacity, then indices. colorFrameCount is 1, so
    no extra colour frames follow."""
    out = bytearray(b"G3D\x03")
    out += struct.pack("<I", len(meshes))
    for item in meshes:
        textured = bool(item.textures & g3dlib.TEX_DIFFUSE)
        out += struct.pack(
            "<7I", item.frame_count, item.frame_count, 1, 1, item.vertex_count, item.index_count, 0
        )
        out += name64(item.texture_names[0][1] if textured else "")
        out += struct.pack(f"<{len(item.vertices)}f", *item.vertices)
        out += struct.pack(f"<{len(item.normals)}f", *item.normals)
        if textured:
            out += struct.pack(f"<{len(item.tex_coords)}f", *item.tex_coords)
        out += struct.pack("<3f", *item.diffuse_color)
        out += struct.pack("<f", item.opacity)
        out += struct.pack(f"<{len(item.indices)}I", *item.indices)
    return bytes(out)


def chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def png(
    width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int, int]]
) -> bytes:
    """RGBA PNG. The IDAT stream uses stored deflate blocks, whose bytes are
    identical on every zlib build, unlike a real compressor's output."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(pixel(x, y))
    compressor = zlib.compressobj(level=0)
    body = compressor.compress(bytes(raw)) + compressor.flush()
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", body)
        + chunk(b"IEND", b"")
    )


def solid_png(size: int, rgba: tuple[int, int, int, int]) -> bytes:
    return png(size, size, lambda x, y: rgba)


def checker_png(
    size: int, first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> bytes:
    return png(size, size, lambda x, y: first if (x // 8 + y // 8) % 2 == 0 else second)


def rings_png(size: int) -> bytes:
    """Flat-shaded stand-in for a diffuse map: concentric colour bands."""
    center = (size - 1) / 2
    bands = ((238, 196, 106, 255), (176, 124, 48, 255))
    return png(
        size,
        size,
        lambda x, y: bands[min(int(math.hypot(x - center, y - center) / (size / 6)), 1)],
    )


def tga(size: int, rgba: tuple[int, int, int, int]) -> bytes:
    """Uncompressed 32-bit BGRA Targa, top-left origin."""
    header = struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, size, size, 32, 0x28)
    red, green, blue, alpha = rgba
    return header + bytes((blue, green, red, alpha)) * (size * size)


def bmp(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Uncompressed 24-bit BMP, bottom-up rows."""
    red, green, blue = rgb
    row = bytes((blue, green, red)) * width
    body = (row + b"\x00" * ((-width * 3) % 4)) * height
    offset = 14 + 40
    header = b"BM" + struct.pack("<IHHI", offset + len(body), 0, 0, offset)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    return header + dib + body


def wav(frames: int = 400, rate: int = 8000, freq: float = 440.0) -> bytes:
    """Mono 16-bit PCM sine. The stdlib writer emits a fixed 44-byte header."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * n / rate)))
                for n in range(frames)
            )
        )
    return buffer.getvalue()


@cache
def build() -> dict[str, bytes]:
    """Every generated fixture, keyed by path under tests/fixtures."""
    files: dict[str, bytes] = {
        "g3d/gold.g3d": v4_model([mesh("gold", box(6, 6), "texture_gold.png")]),
        "g3d/treant_idle.g3d": v4_model(
            [
                mesh("branch", box(6, 6, size=0.4), "bark2.png", 19),
                mesh("trunk", box(6, 6, size=0.9, height=1.8), "texture_treant.png", 19),
                mesh("leaves", box(6, 6, size=1.2, height=0.5), "texture_treant.png", 19),
            ]
        ),
        "g3d/workshop_cons.g3d": v4_model(
            [
                mesh("Mesh.000", box(6, 6, height=2.0), "texture_workshop.png"),
                mesh("Mesh.001", box(6, 6, size=0.8, height=1.6), "scaffold_texture.png"),
                mesh("Mesh.002", box(6, 6, size=1.2, height=1.2), "texture_workshop.png"),
                mesh("Mesh.003", box(6, 6, size=0.6, height=2.4), "scaffold_texture.png"),
                mesh("Mesh.004", box(6, 12, size=0.5, height=0.3)),
            ]
        ),
        "g3d/tower_destruction.g3d": v3_model(
            [
                mesh(
                    "tower_destruction",
                    merge([grid(11, 19) for _ in range(4)]),
                    "texture_spark.tga.tga",
                )
            ]
        ),
        "g3d/texture_gold.png": rings_png(128),
        "g3d/texture_spark.tga.tga": tga(128, (120, 88, 40, 255)),
        "g3d/texture_treant.png": checker_png(64, (58, 96, 46, 255), (36, 64, 30, 255)),
        "g3d/bark2.png": checker_png(64, (96, 70, 42, 255), (64, 46, 28, 255)),
        "g3d/texture_workshop.png": checker_png(64, (198, 186, 160, 255), (150, 138, 112, 255)),
        "g3d/scaffold_texture.png": checker_png(64, (140, 120, 96, 255), (104, 88, 68, 255)),
        f"{LAYOUT_A}/images/legion.bmp": bmp(4, 4, (140, 40, 32)),
        f"{LAYOUT_A}/models/legion.g3d": JUNK_MODEL,
        "packs/layout_b/commondata/sounds/shared_attack.wav": wav(freq=220.0),
        f"{demo}/loading_screen.png": solid_png(8, (24, 32, 48, 255)),
        f"{demo}/music/theme.ogg": OGG_STUB,
        f"{demo}/units/barracks/images/barracks.bmp": bmp(4, 4, (110, 92, 64)),
        f"{demo}/units/barracks/models/barracks.g3d": JUNK_MODEL,
        f"{demo}/units/grunt/images/grunt.bmp": bmp(4, 4, (60, 120, 70)),
        f"{demo}/units/grunt/models/grunt_stand.g3d": JUNK_MODEL,
        f"{demo}/units/grunt/models/grunt_walk.g3d": JUNK_MODEL,
        f"{demo}/units/grunt/sounds/ack1.wav": wav(freq=660.0),
        f"{demo}/upgrades/weaponry/images/weaponry.bmp": bmp(4, 4, (150, 130, 90)),
        f"{demo}/cancel.bmp": bmp(4, 4, (200, 200, 200)),
        "packs/layout_b/resources/gold/images/gold.bmp": bmp(4, 4, (212, 176, 64)),
        "packs/broken/factions/ghosts/images/ghost.bmp": bmp(4, 4, (128, 128, 128)),
    }
    return files


def write_all(fixtures_root: Path) -> list[Path]:
    """Write every fixture under fixtures_root. Returns the paths written."""
    written = []
    for relative, payload in sorted(build().items()):
        target = fixtures_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        written.append(target)
    return written


def verify(fixtures_root: Path) -> None:
    """Re-read every written file and check it parses as its own format."""
    for relative, payload in sorted(build().items()):
        target = fixtures_root / relative
        assert target.read_bytes() == payload, f"{relative} did not round-trip"

    for name in ("gold.g3d", "treant_idle.g3d", "workshop_cons.g3d"):
        model = g3dlib.G3DModel.read(str(fixtures_root / "g3d" / name))
        assert model.version == 4, name
        for item in model.meshes:
            expected = item.frame_count * item.vertex_count * 3
            assert len(item.vertices) == expected, f"{name}: vertices"
            assert len(item.normals) == expected, f"{name}: normals"
            assert len(item.indices) == item.index_count, f"{name}: indices"
            assert all(value < item.vertex_count for value in item.indices), f"{name}: range"
            if item.has_diffuse_texture:
                assert len(item.tex_coords) == item.vertex_count * 2, f"{name}: tex coords"

    raw = (fixtures_root / "g3d" / "tower_destruction.g3d").read_bytes()
    assert raw[:4] == b"G3D\x03"
    assert struct.unpack("<I", raw[4:8])[0] == 1
    point_count, index_count = struct.unpack("<2I", raw[24:32])
    assert point_count == 836 and index_count > 0
    assert len(raw) == 8 + 28 + 64 + point_count * 24 + point_count * 8 + 16 + index_count * 4

    for relative in ("g3d/texture_gold.png", f"{demo}/loading_screen.png"):
        assert (fixtures_root / relative).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", relative

    tga_header = (fixtures_root / "g3d/texture_spark.tga.tga").read_bytes()[:18]
    assert tga_header == struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, 128, 128, 32, 0x28)

    for relative in (
        f"{demo}/units/grunt/sounds/ack1.wav",
        "packs/layout_b/commondata/sounds/shared_attack.wav",
    ):
        with wave.open(io.BytesIO((fixtures_root / relative).read_bytes())) as handle:
            assert (handle.getnchannels(), handle.getframerate()) == (1, 8000), relative
            assert handle.getnframes() == 400, relative

    for relative in (f"{LAYOUT_A}/images/legion.bmp", f"{demo}/cancel.bmp"):
        assert (fixtures_root / relative).read_bytes()[:2] == b"BM", relative


def main() -> None:
    write_all(FIXTURES)
    verify(FIXTURES)
    print(f"wrote and verified {len(build())} fixture files under {FIXTURES}")


if __name__ == "__main__":
    main()
