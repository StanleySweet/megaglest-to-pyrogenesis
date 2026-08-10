"""MegaGlest pack discovery and layout auto-detection.

Two pack layouts exist in the wild and both are supported:

* **Layout A (classic)**: ``techs/{tech}/factions/{faction}/...``
* **Layout B (flat)**: ``factions/{faction}/...`` with the tech-tree XML at
  the pack root (e.g. ``{pack_name}.xml``).

Detection is purely structural: presence of a ``techs/`` directory selects
Layout A, otherwise a ``factions/`` directory selects Layout B.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.errors import PackStructureError
from .xmlutil import parse_xml

if TYPE_CHECKING:
    from .civ_loader import Faction

LOGGER = logging.getLogger(__name__)


class LayoutKind(Enum):
    """The detected MegaGlest pack layout."""

    CLASSIC = "classic"
    FLAT = "flat"


@dataclass(frozen=True)
class TechTree:
    """Attack/armor types and damage multipliers from the tech-tree XML."""

    attack_types: tuple[str, ...] = ()
    armor_types: tuple[str, ...] = ()
    damage_multipliers: dict[tuple[str, str], float] = field(default_factory=dict)


@dataclass
class MegaglestPack:
    """A discovered MegaGlest pack (structure only; factions load lazily)."""

    root: Path
    layout: LayoutKind
    name: str
    factions_dir: Path
    tech_root: Path | None = None
    tech_xml: Path | None = None
    factions: dict[str, Faction] = field(default_factory=dict)
    resources_dir: Path | None = None
    tilesets_dir: Path | None = None
    maps_dir: Path | None = None
    scenarios_dir: Path | None = None
    tech_tree: TechTree | None = None
    factions: dict[str, object] = field(default_factory=dict)

    def macro_map(self) -> dict[str, Path]:
        """Path macros used in asset references (``$COMMONDATAPATH`` etc.)."""
        macros = {"COMMONDATAPATH": self.commondata_dir} if self.commondata_dir else {}
        if self.tech_root is not None:
            macros["TECHSPATH"] = self.tech_root
        return macros


def discover_pack(root: Path) -> MegaglestPack:
    """Locate and describe the pack at ``root``.

    Raises :class:`PackStructureError` when neither a classic nor a flat
    layout can be identified.
    """
    root = root.resolve()
    if not root.is_dir():
        raise PackStructureError(f"Pack root is not a directory: {root}")

    techs_dir = root / "techs"
    flat_factions = root / "factions"

    if techs_dir.is_dir():
        tech_dirs = _subdirs(techs_dir)
        if not tech_dirs:
            raise PackStructureError(f"{techs_dir} exists but contains no tech directories")
        tech_root = tech_dirs[0]
        pack = MegaglestPack(
            root=root,
            layout=LayoutKind.CLASSIC,
            name=tech_root.name,
            tech_root=tech_root,
            factions_dir=tech_root / "factions",
        )
        pack.tech_xml = _first_xml(tech_root, tech_root.name)
    elif flat_factions.is_dir():
        pack = MegaglestPack(
            root=root,
            layout=LayoutKind.FLAT,
            name=root.name,
            factions_dir=flat_factions,
        )
        pack.tech_root = root
        pack.tech_xml = _first_xml(root, root.name)
    else:
        raise PackStructureError(
            f"Unrecognized pack layout at {root}: expected 'techs/' (classic) "
            "or 'factions/' (flat) directory"
        )

    pack.commondata_dir = _optional_dir(root / "commondata")
    pack.resources_dir = _optional_dir(root / "resources")
    pack.tilesets_dir = _optional_dir(root / "tilesets")
    pack.maps_dir = _optional_dir(root / "maps")
    pack.scenarios_dir = _optional_dir(root / "scenarios")
    if not pack.factions_dir.is_dir():
        raise PackStructureError(f"Factions directory missing: {pack.factions_dir}")

    if pack.tech_xml is not None:
        pack.tech_tree = _parse_tech_tree(pack.tech_xml)
    else:
        LOGGER.warning("No tech-tree XML found for pack %s", pack.name)

    LOGGER.info(
        "Discovered pack",
        extra={"pack_name": pack.name, "layout": pack.layout.value, "root": str(root)},
    )
    return pack


def _subdirs(directory: Path) -> list[Path]:
    return sorted(
        (p for p in directory.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name,
    )


def _optional_dir(path: Path) -> Path | None:
    return path if path.is_dir() else None


def _first_xml(directory: Path, preferred_stem: str) -> Path | None:
    """``{stem}.xml`` in ``directory``, else the first ``*.xml`` file."""
    preferred = directory / f"{preferred_stem}.xml"
    if preferred.is_file():
        return preferred
    matches = sorted(directory.glob("*.xml"))
    return matches[0] if matches else None


def _parse_tech_tree(tech_xml: Path) -> TechTree:
    root = parse_xml(tech_xml)
    attack_types: tuple[str, ...] = ()
    armor_types: tuple[str, ...] = ()
    multipliers: dict[tuple[str, str], float] = {}

    node = root.get("attack-types")
    if node is not None:
        attack_types = tuple(
            str(item.value() or item.name_attr() or "") for item in node.get_all("attack-type")
        )
    node = root.get("armor-types")
    if node is not None:
        armor_types = tuple(
            str(item.value() or item.name_attr() or "") for item in node.get_all("armor-type")
        )
    node = root.get("damage-multipliers")
    if node is not None:
        for item in node.get_all("damage-multiplier"):
            attack = item.attr("attack")
            armor = item.attr("armor")
            value = item.float_value()
            if attack and armor and value is not None:
                multipliers[(attack, armor)] = value
    return TechTree(
        attack_types=attack_types,
        armor_types=armor_types,
        damage_multipliers=multipliers,
    )
