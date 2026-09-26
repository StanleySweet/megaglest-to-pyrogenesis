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

if TYPE_CHECKING:
    from .civ_loader import Faction

LOGGER = logging.getLogger(__name__)


class LayoutKind(Enum):
    """The detected MegaGlest pack layout."""

    CLASSIC = "classic"
    FLAT = "flat"


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
    if not pack.factions_dir.is_dir():
        raise PackStructureError(f"Factions directory missing: {pack.factions_dir}")

    if pack.tech_xml is None:
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


