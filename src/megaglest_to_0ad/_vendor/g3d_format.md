# MegaGlest G3D binary format

Specification for the `.g3d` model format used by Glest and MegaGlest.
Derived from the authoritative reader in the MegaGlest source tree
(`megaglest-source` branch `develop`):

- `source/shared_lib/include/shared/graphics/model_header.h` — header structs
- `source/shared_lib/sources/shared/graphics/model.cpp` — `Mesh::loadV3`,
  `Mesh::loadV4` and friends

Validated empirically against all 96 `.g3d` files in the `demo_A10` pack
(single-mesh and multi-mesh, versions 3 and 4).

## File layout (version 4)

All integers are little-endian. All offsets are relative to file start.

```
FileHeader { uint8 id[3]; uint8 version; }        ; 4 bytes, id == "G3D", version == 4
ModelHeader { uint16 meshCount; uint8 type; }     ; 3 bytes, type == 0 (mtMorphMesh)

for each mesh m:
  MeshHeader {                                        ; 116 bytes, packed
    uint8  name[64]
    uint32 frameCount
    uint32 vertexCount
    uint32 indexCount
    float32 diffuseColor[3]
    float32 specularColor[3]
    float32 specularPower
    float32 opacity
    uint32 properties
    uint32 textures
  }
  uint8 textureName[64]  x  (number of set bits in `textures`)   ; LSB first

  float32 vertices[frameCount * vertexCount * 3]    ; positions, frame-blocked
  float32 normals[frameCount * vertexCount * 3]     ; normals, frame-blocked
  if textures & TEX_DIFFUSE:
    float32 texCoords[vertexCount * 2]              ; UVs, once (not per frame)
  uint32 indices[indexCount]
```

- Frames are blocked position-then-normal: all positions of frame 0, then all
  normals of frame 0, then positions of frame 1, etc. Frame 0 is the base
  pose.
- The first mesh's header block is 187 bytes (7-byte file+model header + 116 +
  64). Every additional mesh is preceded by a 180-byte sub-header (116 + 64)
  with no magic/count prefix. Total size:
  `187 + 180 * (meshCount - 1) + sum_m (frameCount_m * vertexCount_m * 24
  + vertexCount_m * 8 + indexCount_m * 4)` when the diffuse bit is set.
- Texture bits (`enum MeshTexture`): `1` = diffuse, `2` = specular,
  `4` = normal, `8` = reflection, `16` = color mask. Slot bit `1` is the only
  one present in the observed packs.
- Property flags (`enum MeshPropertyFlag`): `1` = custom color, `2` = two
  sided, `4` = no select, `8` = glow, `16` = alpha-is-transparency (texture
  alpha modulates opacity for team-colored meshes).
- Texture names are resolved relative to the directory containing the `.g3d`
  file.

### Observed values (demo_A10 pack)

- `meshCount` ranges 1–9 (it is a count, not a bitmask). All sub-meshes share
  the main mesh's `frameCount`. Zero mismatches between the header count and
  the number of sections that parse, across all 87 version-4 files.
- Indices are `uint32` and always `< vertexCount`.
- Normals are unit-length in files exported with `fmt=3`+; some older files
  (`bard_idle.g3d`) carry unnormalized normals (lengths ~1.05–1.36) and
  must be normalized on import.
- The diffuse texture bit is optional: `workshop_cons.g3d`'s final mesh
  (`Mesh.006`) has `textures == 0` and therefore **no UV block**; the file
  ends exactly at EOF after its index array. Do not assume every mesh has UVs.
  `gold.g3d` lives at `resources/gold/models/gold.g3d` (not under `units/`).

## File layout (version 3)

Observed in exactly one pack file: `tower_destruction.g3d`.

```
FileHeader                                    ; 4 bytes, version == 3
ModelHeaderV3 { uint32 meshCount; }           ; 4 bytes, always 1 in practice
MeshHeaderV3 {                                  ; 92 bytes, packed
  uint32 vertexFrameCount
  uint32 normalFrameCount
  uint32 texCoordFrameCount
  uint32 colorFrameCount
  uint32 pointCount
  uint32 indexCount
  uint32 properties
  uint8  texName[64]
}
skip (colorFrameCount - 1) * 16 bytes         ; extra color frames
uint32 indices[indexCount]
```

- Property flags (`enum MeshPropertyV3`): `1` = no texture, `2` = two sided,
  `4` = custom color. Texture flag `1` (diffuse) is implied when
  `mp3NoTexture` is not set; the texture name is the 64-byte `texName` slot.
- `vertexFrameCount` must equal `normalFrameCount` (the loader rejects
  mismatches).
- Front faces are **clockwise** (comment in `model_header.h`). Importers that
  target counter-clockwise front faces (OpenGL default, 0 A.D.) must flip the
  winding, i.e. emit triangle indices `(a, c, b)` instead of `(a, b, c)`.
- `tower_destruction.g3d` quirk: the header's texture name is
  `texture_spark.tga.tga` — the extension is doubled in the pack.
  Preserve the name when resolving the file; the actual on-disk file is
  `texture_spark.tga` (the doubled suffix is dropped on disk). Indices
  end exactly at EOF; `pointCount=836`, `indexCount=2004`, all index values
  `< 836`.

## Coordinates

- Right-handed, Y-up (MegaGlest and 0 A.D. agree; no axis conversion needed).
- Unit scale: glest models are authored in metres-ish world units
  (~1.5–2 units tall for a humanoid), matching 0 A.D. unit scale.
