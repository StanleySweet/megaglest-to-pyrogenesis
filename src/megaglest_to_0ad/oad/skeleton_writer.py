"""Skeleton XML generation (``art/skeletons/{civ}.xml``).

Each rigged base model contributes one ``<standard_skeleton>`` (the joint
contract: one ``<bone>`` per joint in ``rig.bone_names`` order) and one
mapped ``<skeleton>`` whose identifier root is the armature's root joint
name. The engine's ``FindSkeleton`` (CommonConvert.cpp) walks up from the
controller's joint 0 until a joint *name* matches a mapped skeleton's
identifier root, so the root joint name is the DAE<->skeleton link.

Mapping rule: ``targetId`` (PMD vertex influences) and ``realTargetId``
(PSA key slots) both equal the standard bone's index, and the mapped bones
list the DAE joints in the same order as the standard bones — so PMD bone
IDs and PSA keys line up with ``rig.bone_names`` indices (root = 0).
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from ..converters.rig import Rig


def write_skeletons(entries: list[tuple[str, Rig]], output_dir: Path, civ: str) -> Path | None:
    """Write one skeleton pair per rigged base model; returns the XML path.

    ``entries`` is ``[(deduped mesh stem, rig)]`` in deterministic order.
    Standard skeletons are emitted before the mapped ones (the loader
    resolves ``<skeleton target>`` against already-loaded standard ids).
    No entries -> no file (a faction with only static models needs none).
    """
    if not entries:
        return None
    root = etree.Element("skeletons")
    for stem, rig in entries:
        std = etree.SubElement(root, "standard_skeleton", id=f"mg_{civ}_{stem}")
        for bone in rig.bone_names:
            etree.SubElement(std, "bone", name=bone)
    for stem, rig in entries:
        mapped = etree.SubElement(root, "skeleton", target=f"mg_{civ}_{stem}")
        identifier = etree.SubElement(mapped, "identifier")
        etree.SubElement(identifier, "root").text = rig.root_name
        for bone in rig.bone_names:
            bone_el = etree.SubElement(mapped, "bone", name=bone)
            etree.SubElement(bone_el, "target").text = bone
    path = output_dir / f"{civ}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    tree = etree.ElementTree(root)
    etree.indent(tree, space="  ")
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="utf-8"))
    return path
