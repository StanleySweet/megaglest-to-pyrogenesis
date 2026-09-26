"""Validate generated DAEs against the 0 A.D. COLLADA importer contract.

The checks mirror what the engine's importer actually requires
(``source/collada/CommonConvert.cpp``, ``PMDConvert.cpp``, ``PSAConvert.cpp``):
a single instanced object per file, triangle-only geometry with exactly one
polygon set, POSITION/NORMAL/TEXCOORD inputs that resolve to float sources of
equal length, in-range interleaved indices, and — for skinned models — a
consistent skin (joint names/counts, bind poses, normalized vertex weights)
plus animation channels targeting existing armature joints.

Mesh DAEs (``art/meshes``) and animation DAEs (``art/animation``) share the
geometry+skin contract; animation DAEs additionally carry
``library_animations`` and are validated by ``validate_animation_dae``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

LOGGER = logging.getLogger(__name__)

_NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
_C = f"{{{_NS['c']}}}"


def _tag(name: str) -> str:
    return f"{_C}{name}"


@dataclass
class DaeAudit:
    """Result of validating every DAE under a mod's ``art/`` tree."""

    checked: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def validate_mod_meshes(mod_dir: Path) -> DaeAudit:
    """Validate every mesh and animation DAE under the mod's ``art/`` tree."""
    audit = DaeAudit()
    for path in sorted((mod_dir / "art/meshes").rglob("*.dae")):
        audit.checked += 1
        failures = validate_mesh_dae(path)
        _record(audit, path, mod_dir, failures)
    for path in sorted((mod_dir / "art/animation").rglob("*.dae")):
        audit.checked += 1
        failures = validate_animation_dae(path)
        _record(audit, path, mod_dir, failures)
    return audit


def _record(audit: DaeAudit, path: Path, mod_dir: Path, failures: list[str]) -> None:
    if not failures:
        audit.passed += 1
        return
    audit.failures.append(f"{path.relative_to(mod_dir)}: {failures[0]}")


def validate_mesh_dae(path: Path) -> list[str]:
    """Return the importer-contract violations for one mesh DAE ([] = importable).

    Mirrors CommonConvert::CommonConvert (visual scene root, single instance,
    up axis) and PMDConvert::GetPolysFromGeometry + WritePMD (mesh, one
    triangle set, POSITION/NORMAL/TEXCOORD, index/data consistency). Both
    instanced geometry (static) and instanced controller (skinned) forms are
    accepted; skinned files additionally satisfy the skin contract.
    """
    root = _parse(path)
    if root is None:
        return [f"XML parse error: {path}"]
    failures: list[str] = []

    # CommonConvert: visual scene root + single instanced object
    scene = root.find(f".//{_tag('library_visual_scenes')}/{_tag('visual_scene')}")
    if scene is None:
        failures.append("no visual scene root")
    instances = root.xpath("//c:instance_geometry | //c:instance_controller", namespaces=_NS)
    if len(instances) != 1:
        failures.append(f"expected exactly one instanced object, found {len(instances)}")

    # up axis: importer assumes Y_UP or Z_UP
    up = root.xpath("//c:asset/c:up_axis/text()", namespaces=_NS)
    if up and up[0] not in ("Y_UP", "Z_UP"):
        failures.append(f"unsupported up_axis {up[0]!r}")

    instance = instances[0] if instances else None
    controller = None
    if instance is not None and instance.tag == _tag("instance_controller"):
        controller = _resolve_controller(root, instance)
        if controller is None:
            failures.append("instance_controller url does not resolve to a controller")
            return failures
        skin = controller.find(_tag("skin"))
        if skin is None:
            failures.append("controller has no skin")
            return failures
        geom_id = skin.get("source", "").lstrip("#")
    else:
        geom_id = instance.get("url", "").lstrip("#") if instance is not None else ""

    geometry = next(
        (g for g in root.xpath("//c:geometry", namespaces=_NS) if g.get("id") == geom_id),
        None,
    )
    if geometry is None:
        failures.append(f"instance url #{geom_id} does not resolve to a geometry")
        return failures
    failures.extend(_geometry_checks(geometry, root))

    if controller is not None:
        failures.extend(_skin_checks(controller, root, _geometry_vertex_count(geometry)))
    return failures


def validate_animation_dae(path: Path) -> list[str]:
    """Mesh/skin checks plus the animation-channel contract for one DAE."""
    failures = validate_mesh_dae(path)
    if failures:
        return failures
    root = _parse(path)
    if root is None:
        return [f"XML parse error: {path}"]
    library = root.find(_tag("library_animations"))
    anims = library.findall(_tag("animation")) if library is not None else []
    if not anims:
        return ["no library_animations (animation DAE must carry animations)"]

    ids = {s.get("id"): s for s in root.xpath("//c:source", namespaces=_NS)}
    joint_ids = {
        n.get("id")
        for n in root.xpath(
            "//c:library_visual_scenes//c:node[@type='JOINT']",
            namespaces=_NS,
        )
    }
    joint_ids.update(
        n.get("id")
        for n in root.xpath("//c:library_visual_scenes//c:node", namespaces=_NS)
        if n.get("type") == "NODE"
    )
    for anim in anims:
        sampler = anim.find(_tag("sampler"))
        if sampler is None:
            failures.append(f"animation {anim.get('id')}: no sampler")
            continue
        inputs = sampler.findall(_tag("input"))
        by_semantic = {inp.get("semantic"): inp.get("source", "").lstrip("#") for inp in inputs}
        lengths: dict[str, int] = {}
        for semantic in ("INPUT", "OUTPUT", "INTERPOLATION"):
            sid = by_semantic.get(semantic)
            if sid is None:
                failures.append(f"animation {anim.get('id')}: missing {semantic} input")
                continue
            source = ids.get(sid)
            if source is None:
                failures.append(f"animation {anim.get('id')}: {semantic} source #{sid} missing")
                continue
            if semantic == "OUTPUT":
                floats = _source_floats(source)
                if not floats or floats % 16:
                    failures.append(f"animation {anim.get('id')}: OUTPUT is not 16-float data")
                    lengths[semantic] = floats // 16 if floats else 0
                else:
                    lengths[semantic] = floats // 16
            elif semantic == "INPUT":
                lengths[semantic] = _source_floats(source)
            else:
                interp = _source_names(source)
                if interp is None or set(interp) != {"LINEAR"}:
                    failures.append(f"animation {anim.get('id')}: interpolation is not LINEAR")
                lengths[semantic] = len(interp) if interp is not None else 0
        if lengths and len(set(lengths.values())) != 1:
            failures.append(f"animation {anim.get('id')}: source length mismatch {lengths}")
        channel = anim.find(_tag("channel"))
        if channel is None:
            failures.append(f"animation {anim.get('id')}: no channel")
            continue
        target = channel.get("target", "")
        joint, _, prop = target.partition("/")
        if prop != "transform" or joint not in joint_ids:
            failures.append(
                f"animation {anim.get('id')}: channel target {target!r} is not "
                "an armature joint transform"
            )
        source_uri = channel.get("source", "").lstrip("#")
        if source_uri != sampler.get("id"):
            failures.append(f"animation {anim.get('id')}: channel source does not match sampler")
    return failures


# ---------------------------------------------------------------------------
# geometry checks
# ---------------------------------------------------------------------------


def _geometry_checks(geometry: etree._Element, root: etree._Element) -> list[str]:
    """GetPolysFromGeometry contract for one geometry element."""
    failures: list[str] = []
    mesh_el = geometry.find(_tag("mesh"))
    if mesh_el is None:
        return ["geometry is not a mesh"]

    # GetPolysFromGeometry: triangles only, exactly one polygon set
    if mesh_el.findall(_tag("polylist")):
        failures.append("polylist geometry (importer requires triangles)")
    triangles = mesh_el.findall(_tag("triangles"))
    if len(triangles) != 1:
        failures.append(f"expected exactly one triangles set, found {len(triangles)}")
        return failures
    tris = triangles[0]

    # Resolve sources: polygons' POSITION comes via <vertices>; NORMAL and
    # TEXCOORD are direct inputs (FCollada links both forms).
    ids = {s.get("id"): s for s in root.xpath("//c:source", namespaces=_NS)}
    vertices = mesh_el.find(_tag("vertices"))
    vertex_position = None
    if vertices is not None:
        pos = vertices.find(_tag("input"))
        if pos is not None and pos.get("semantic") == "POSITION":
            vertex_position = ids.get(pos.get("source", "").lstrip("#"))

    inputs = tris.findall(_tag("input"))
    if not inputs:
        failures.append("triangles has no inputs")
        return failures
    offset_count = max(int(i.get("offset", "0")) for i in inputs) + 1

    p = tris.find(_tag("p"))
    if p is None or not p.text:
        failures.append("triangles has no index data")
        return failures
    indices = [int(v) for v in p.text.split()]

    position = normal = texcoord = None
    for inp in inputs:
        semantic = inp.get("semantic")
        source = inp.get("source", "").lstrip("#")
        if semantic == "VERTEX":
            if vertex_position is not None:
                position = vertex_position
        elif semantic == "POSITION":
            position = ids.get(source)
        elif semantic == "NORMAL":
            normal = ids.get(source)
        elif semantic == "TEXCOORD":
            texcoord = ids.get(source)

    if position is None:
        failures.append("missing POSITION input")
    if normal is None:
        failures.append("missing NORMAL input (importer REQUIRES it)")
    if texcoord is None:
        failures.append("missing TEXCOORD input (importer REQUIRES it)")

    if position is not None and normal is not None and texcoord is not None:
        n_pos, n_norm, n_uv = (
            _source_floats(position),
            _source_floats(normal),
            _source_floats(texcoord),
        )
        if not n_pos or n_pos % 3:
            failures.append("POSITION source is not 3-float data")
        if not n_norm or n_norm % 3:
            failures.append("NORMAL source is not 3-float data")
        if not n_uv or n_uv % 2:
            failures.append("TEXCOORD source is not 2-float data")
        vertex_count = n_pos // 3 if n_pos else 0
        if vertex_count and (n_norm != 3 * vertex_count or n_uv != 2 * vertex_count):
            failures.append(f"source length mismatch (pos {n_pos}, norm {n_norm}, uv {n_uv})")
        if vertex_count:
            for offset, source, per in ((0, position, 3), (1, normal, 3), (2, texcoord, 2)):
                n = _source_floats(source)
                if n and any(
                    idx >= vertex_count or idx * per + (per - 1) >= n
                    for idx in indices[offset::offset_count]
                ):
                    failures.append(f"index out of range in input offset {offset}")

    expected = int(tris.get("count", "-1")) * 3 * offset_count
    if len(indices) != expected:
        failures.append(
            f"index count {len(indices)} != triangles count {tris.get('count')} "
            f"* {3 * offset_count}"
        )
    return failures


# ---------------------------------------------------------------------------
# skin checks
# ---------------------------------------------------------------------------


def _resolve_controller(root: etree._Element, instance: etree._Element) -> etree._Element | None:
    cid = instance.get("url", "").lstrip("#")
    return next(
        (c for c in root.xpath("//c:controller", namespaces=_NS) if c.get("id") == cid),
        None,
    )


def _skin_checks(controller: etree._Element, root: etree._Element, vertex_count: int) -> list[str]:
    """Skin contract: joints/bind poses/weights consistent and in range."""
    failures: list[str] = []
    skin = controller.find(_tag("skin"))
    if skin is None:
        return ["controller has no skin"]

    ids = {s.get("id"): s for s in root.xpath("//c:source", namespaces=_NS)}
    joints = skin.find(_tag("joints"))
    if joints is None:
        return ["skin has no joints element"]
    joint_sid = next(
        (
            i.get("source", "").lstrip("#")
            for i in joints.findall(_tag("input"))
            if i.get("semantic") == "JOINT"
        ),
        "",
    )
    names = _source_names(ids.get(joint_sid))
    if not names:
        return ["skin joint Name_array missing or empty"]
    bind_sid = next(
        (
            i.get("source", "").lstrip("#")
            for i in joints.findall(_tag("input"))
            if i.get("semantic") == "INV_BIND_MATRIX"
        ),
        "",
    )
    n_bind = _source_floats(ids.get(bind_sid)) // 16 if ids.get(bind_sid) is not None else 0
    if n_bind != len(names):
        failures.append(f"bind pose count {n_bind} != joint count {len(names)}")
    if any(not name for name in names):
        failures.append("skin contains an empty joint name")

    vw = skin.find(_tag("vertex_weights"))
    if vw is None:
        return [*failures, "skin has no vertex_weights"]
    count = int(vw.get("count", "-1"))
    vcount = vw.find(_tag("vcount"))
    v_el = vw.find(_tag("v"))
    if vcount is None or not vcount.text:
        failures.append("vertex_weights has no vcount")
        return failures
    counts = [int(v) for v in vcount.text.split()]
    if len(counts) != count:
        failures.append(f"vcount length {len(counts)} != vertex_weights count {count}")
    if vertex_count and count != vertex_count:
        failures.append(f"vertex_weights count {count} != geometry vertex count {vertex_count}")
    if v_el is None or not v_el.text:
        failures.append("vertex_weights has no v data")
        return failures
    values = [int(v) for v in v_el.text.split()]
    if sum(counts) * 2 != len(values):
        failures.append(f"v length {len(values)} != 2 * {sum(counts)}")

    # weight values stay in range and each vertex's weights are normalized
    weights_sid = next(
        (
            i.get("source", "").lstrip("#")
            for i in vw.findall(_tag("input"))
            if i.get("semantic") == "WEIGHT"
        ),
        "",
    )
    weight_source = ids.get(weights_sid)
    n_weights = _source_floats(weight_source) if weight_source is not None else 0
    weight_values = _source_float_list(weight_source)
    cursor = 0
    per_vertex: list[list[float]] = []
    for c in counts:
        per_vertex.append([])
        for _ in range(c):
            if cursor + 1 < len(values):
                weight_index = values[cursor + 1]
                per_vertex[-1].append(0.0)
                if 0 <= weight_index < n_weights:
                    per_vertex[-1][-1] = weight_values[weight_index]
                else:
                    failures.append(f"weight index {weight_index} out of range")
            cursor += 2
    for i, weights in enumerate(per_vertex):
        if weights and abs(sum(weights) - 1.0) > 1e-2:
            failures.append(f"vertex {i} weights sum to {sum(weights):.4f} (not normalized)")
    return failures


def _source_float_list(source: etree._Element | None) -> list[float]:
    """Floats of a <source>'s float_array, parsed once ([] if missing)."""
    if source is None:
        return []
    array = source.find(_tag("float_array"))
    if array is None or not array.text:
        return []
    return [float(v) for v in array.text.split()]


def _geometry_vertex_count(geometry: etree._Element) -> int:
    positions = geometry.find(f".//{_tag('source')}/{_tag('float_array')}")
    if positions is None or not positions.text:
        return 0
    return len(positions.text.split()) // 3


# ---------------------------------------------------------------------------
# source helpers
# ---------------------------------------------------------------------------


def _parse(path: Path) -> etree._Element | None:
    try:
        return etree.parse(str(path)).getroot()
    except etree.XMLSyntaxError:
        return None


def _source_floats(source: etree._Element | None) -> int:
    """Number of floats in a <source>'s float_array (0 if missing/malformed)."""
    if source is None:
        return 0
    array = source.find(_tag("float_array"))
    if array is None or not array.text:
        return 0
    actual = len(array.text.split())
    declared = int(array.get("count", "-1"))
    return actual if declared == actual else 0


def _source_names(source: etree._Element | None) -> list[str] | None:
    if source is None:
        return None
    array = source.find(_tag("Name_array"))
    if array is None or not array.text:
        return None
    return array.text.split()
