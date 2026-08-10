#!/usr/bin/env python3
"""Upgrade old 0 A.D. mod materials to modern _norm_spec/_parallax_spec.

Old-style mods reference pre-0.27 material names (``basic_spec.xml``,
``blend_spec.xml``, ``objectcolor_spec.xml``, ``playercolor_spec.xml``) and
declare only ``baseTex``; modern materials additionally want ``normTex`` and
``specTex`` slots, and the norm/spec variants read a height map from the
normTex alpha channel.

Pipeline per scanned file (``art/actors/*.xml`` and ``art/terrains/*.xml``):

1. Rename the four legacy spec materials to their modern names
   (``basic_spec -> no_trans_spec``, ``blend_spec -> basic_trans_spec``,
   ``objectcolor_spec -> objectcolor_specmap``,
   ``playercolor_spec -> player_trans_spec``).
2. Upgrade every material with a _norm_spec sibling to that variant; when the
   actor's own normTex (not the placeholder) has non-opaque alpha pixels the
   height map is present, so the ``_parallax_spec`` variant is used instead.
3. Fill missing texture slots: ``normTex -> default_norm.png`` and
   ``specTex -> null_black.dds`` (the placeholders 0 A.D. ships in
   ``art/textures/skins/`` for exactly this).
4. Warn on unknown materials, missing texture assets, and cases needing
   manual attention; save only the files that changed and report the count.

Usage::

    python tools/upgrade_materials.py --mod-dir /path/to/mod [--dry-run]

``--dry-run`` prints what would change without writing anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lxml import etree

# Legacy spec material -> renamed intermediate (user-specified mapping).
_RENAME = {
    "basic_spec.xml": "no_trans_spec.xml",
    "blend_spec.xml": "basic_trans_spec.xml",
    "objectcolor_spec.xml": "objectcolor_specmap.xml",
    "playercolor_spec.xml": "player_trans_spec.xml",
}

# Upgradable material -> (norm_spec variant, parallax variant or None).
_UPGRADE = {
    "no_trans_spec.xml": ("no_trans_norm_spec.xml", "no_trans_parallax_spec.xml"),
    "basic_trans_spec.xml": ("basic_trans_norm_spec.xml", "basic_trans_parallax_spec.xml"),
    "player_trans_spec.xml": ("player_trans_norm_spec.xml", "player_trans_parallax_spec.xml"),
    "objectcolor_specmap.xml": ("objectcolor_norm_spec.xml", None),
    "basic_trans.xml": ("basic_trans_norm_spec.xml", "basic_trans_parallax_spec.xml"),
    "player_trans.xml": ("player_trans_norm_spec.xml", "player_trans_parallax_spec.xml"),
    "objectcolor.xml": ("objectcolor_norm_spec.xml", None),
}

# Modern materials the tool leaves alone (0.28/0.29 public material set).
_KNOWN_MODERN = frozenset(
    {
        "aura_norm_spec.xml",
        "basic_glow_norm_spec.xml",
        "basic_glow_wind_norm_spec.xml",
        "basic_trans_ao_norm_spec.xml",
        "basic_trans_ao_parallax_spec.xml",
        "basic_trans_norm_spec.xml",
        "basic_trans_parallax_spec.xml",
        "basic_trans_wind.xml",
        "basic_trans_wind_grain.xml",
        "basic_trans_wind_grain_norm_spec.xml",
        "basic_trans_wind_norm_spec.xml",
        "default.xml",
        "no_trans_ao_norm_spec.xml",
        "no_trans_ao_parallax_spec.xml",
        "no_trans_norm_spec.xml",
        "no_trans_parallax_spec.xml",
        "objectcolor_norm_spec.xml",
        "player_trans_ao_norm_spec.xml",
        "player_trans_ao_parallax_spec.xml",
        "player_trans_ao_spec.xml",
        "player_trans_norm_spec.xml",
        "player_trans_norm_spec_helmet.xml",
        "player_trans_parallax_spec.xml",
        "player_trans_parallax_spec_helmet.xml",
        "player_water.xml",
        "rock_norm_spec.xml",
        "rock_norm_spec_ao.xml",
        "rock_normstrong_spec.xml",
        "rock_normstrong_spec_ao.xml",
        "terrain_norm_spec.xml",
        "terrain_normstrong_spec.xml",
        "terrain_normweak_spec.xml",
        "waterfall.xml",
    }
)

_PLACEHOLDER_NORM = "default_norm.png"
_PLACEHOLDER_SPEC = "null_black.dds"
_ALPHA_THRESHOLD = 255  # any pixel with alpha < 255 = height map present


def _scan_files(mod_dir: Path) -> list[Path]:
    return sorted(
        list((mod_dir / "art/actors").rglob("*.xml"))
        + list((mod_dir / "art/terrains").rglob("*.xml"))
    )


def _has_alpha_heightmap(norm_file: Path) -> bool:
    """True when normTex PNG has non-opaque pixels (parallax height map)."""
    try:
        from PIL import Image

        image = Image.open(norm_file)
    except Exception:
        return False
    if image.mode not in ("RGBA", "LA", "L"):
        image = image.convert("RGBA")
    alpha = image.getchannel("A") if image.mode == "RGBA" else image.convert("RGBA").getchannel("A")
    return alpha.getextrema()[0] < _ALPHA_THRESHOLD


def _resolve_texture(mod_dir: Path, ref: str) -> Path | None:
    """Locate an actor/terrain texture ref under the mod's art/textures tree.

    Actor refs resolve against ``art/textures/skins/`` (ObjectBase.cpp:264);
    terrain refs against ``art/textures/``; either base may hold the file as
    a source PNG/DDS or as a ``.cached.dds`` (0.28+ texture cache).
    """
    for base in (mod_dir / "art/textures/skins", mod_dir / "art/textures"):
        for candidate in (base / ref, base / f"{ref}.cached.dds"):
            if candidate.is_file():
                return candidate
    return None


def _norm_file_for(mod_dir: Path, root: etree._Element) -> Path | None:
    norm = root.find(".//texture[@name='normTex']")
    if norm is None or norm.get("file") == _PLACEHOLDER_NORM:
        return None
    return _resolve_texture(mod_dir, norm.get("file"))


def _material_text(root: etree._Element) -> str:
    node = root.find("material")
    return node.text if node is not None and node.text else ""


def _serialize(root: etree._Element) -> bytes:
    tree = etree.ElementTree(root)
    etree.indent(tree, space="  ")
    return etree.tostring(tree, xml_declaration=True, encoding="utf-8")


def upgrade_file(
    path: Path, mod_dir: Path, warnings: list[str], apply: bool = True
) -> tuple[str, ...]:
    """Apply the upgrade pipeline to one XML; returns a summary of changes."""
    tree = etree.parse(str(path))
    root = tree.getroot()
    material = _material_text(root)
    if not material:
        return ()
    changes: list[str] = []
    original = _serialize(root)

    renamed = _RENAME.get(material)
    if renamed is not None:
        material = renamed
        changes.append(f"material {path.name}: renamed to {renamed}")
    upgrade = _UPGRADE.get(material)
    if upgrade is None:
        if material not in _KNOWN_MODERN:
            warnings.append(f"{path}: unknown material '{material}' (manual review)")
        if material.endswith(("_spec.xml", "_specmap.xml")) and not (
            material in _KNOWN_MODERN
            or any(k in material for k in ("norm_spec", "parallax_spec", "ao_spec"))
        ):
            # legacy-spec-like but not in the rename/upgrade maps
            if not changes:
                warnings.append(f"{path}: legacy spec material '{material}' not mapped")
    else:
        norm_spec, parallax = upgrade
        norm_file = _norm_file_for(mod_dir, root)
        target = (
            parallax
            if parallax is not None and norm_file and _has_alpha_heightmap(norm_file)
            else norm_spec
        )
        if target != material:
            root.find("material").text = target
            changes.append(f"material {path.name}: upgraded to {target}")

    for variant in root.findall(".//variant"):
        textures = variant.find("textures")
        if textures is None:
            continue
        names = {t.get("name") for t in textures.findall("texture")}
        if "normTex" not in names:
            etree.SubElement(textures, "texture", file=_PLACEHOLDER_NORM, name="normTex")
            changes.append(f"{path.name}: added normTex -> {_PLACEHOLDER_NORM}")
        if "specTex" not in names:
            etree.SubElement(textures, "texture", file=_PLACEHOLDER_SPEC, name="specTex")
            changes.append(f"{path.name}: added specTex -> {_PLACEHOLDER_SPEC}")

    for texture in root.iter("texture"):
        ref = texture.get("file")
        if ref in (_PLACEHOLDER_NORM, _PLACEHOLDER_SPEC):
            continue  # resolved through the public mod layer at runtime
        if ref and _resolve_texture(mod_dir, ref) is None:
            warnings.append(f"{path}: texture asset missing: {ref}")

    serialized = _serialize(root)
    if serialized != original:
        if apply:
            path.write_bytes(serialized)
        return tuple(changes)
    return ()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mod-dir", required=True, type=Path, help="mod directory to scan")
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = parser.parse_args(argv)
    warnings: list[str] = []
    fixed_files = 0
    renamed_materials = upgraded_materials = slots_added = 0
    for path in _scan_files(args.mod_dir):
        changes = upgrade_file(path, args.mod_dir, warnings, apply=not args.dry_run)
        if changes:
            fixed_files += 1
            renamed_materials += sum("renamed to" in c for c in changes)
            upgraded_materials += sum("upgraded to" in c for c in changes)
            slots_added += sum("added normTex" in c or "added specTex" in c for c in changes)
            if args.dry_run:
                print(path.relative_to(args.mod_dir))
                print("\n".join(f"  {c}" for c in changes))

    print(f"scanned {len(_scan_files(args.mod_dir))} files")
    print(
        f"would fix {fixed_files} file(s)" if args.dry_run else
        f"fixed {fixed_files} file(s): {renamed_materials} material rename(s), "
        f"{upgraded_materials} upgrade(s), {slots_added} placeholder slot(s) added"
    )
    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
