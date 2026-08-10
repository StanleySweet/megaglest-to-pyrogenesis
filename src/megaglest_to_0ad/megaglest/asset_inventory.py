"""Asset discovery and reference validation for a MegaGlest pack.

Every asset referenced from faction/unit/upgrade XML must exist on disk;
unresolvable references are conversion errors (:class:`AssetReferenceError`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import AssetReferenceError
from ..utils.constants import (
    MAP_SUFFIXES,
    MESH_SUFFIXES,
    MUSIC_SUFFIXES,
    SOUND_SUFFIXES,
    TEXTURE_SUFFIXES,
)
from .civ_loader import Faction, UnitDef, UpgradeDef
from .parser import MegaglestPack

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnresolvedRef:
    """A referenced asset that does not exist on disk."""

    owner: str
    ref: str
    resolved: Path


@dataclass
class AssetInventory:
    """Media files found in the pack plus reference validation results."""

    meshes: list[Path] = field(default_factory=list)
    textures: list[Path] = field(default_factory=list)
    sounds: list[Path] = field(default_factory=list)
    music: list[Path] = field(default_factory=list)
    particles: list[Path] = field(default_factory=list)
    maps: list[Path] = field(default_factory=list)
    referenced: list[Path] = field(default_factory=list)
    unresolved: list[UnresolvedRef] = field(default_factory=list)


def build_inventory(pack: MegaglestPack) -> AssetInventory:
    """Scan the pack and validate every XML-referenced asset.

    Raises :class:`AssetReferenceError` listing all broken references.
    """
    inventory = AssetInventory()
    for path in _walk_files(pack.root):
        suffix = path.suffix.lower()
        if suffix in MESH_SUFFIXES:
            inventory.meshes.append(path)
        elif suffix in TEXTURE_SUFFIXES:
            inventory.textures.append(path)
        elif suffix in MUSIC_SUFFIXES:
            # .ogg/.mp3 in a pack is music; .wav is a sound effect.
            inventory.music.append(path)
        elif suffix in SOUND_SUFFIXES:
            inventory.sounds.append(path)
        elif suffix in MAP_SUFFIXES:
            inventory.maps.append(path)

    for faction in pack.factions.values():
        _collect_faction_refs(inventory, faction)

    if inventory.unresolved:
        details = "\n".join(
            f"  {entry.owner}: {entry.ref!r} -> {entry.resolved}" for entry in inventory.unresolved
        )
        raise AssetReferenceError(
            f"{len(inventory.unresolved)} broken asset reference(s):\n{details}"
        )

    LOGGER.info(
        "Asset inventory",
        extra={
            "meshes": len(inventory.meshes),
            "textures": len(inventory.textures),
            "sounds": len(inventory.sounds),
            "music": len(inventory.music),
            "particles": len(inventory.particles),
            "maps": len(inventory.maps),
            "referenced": len(inventory.referenced),
        },
    )
    return inventory


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        files.append(path)
    return sorted(files)


def _collect_faction_refs(inventory: AssetInventory, faction: Faction) -> None:
    owner = f"factions/{faction.name}"
    _add_ref(inventory, owner, faction.music)
    _add_ref(inventory, owner, faction.loading_screen)
    for particle in faction.particles:
        _add_ref(inventory, owner, particle)
    for unit in faction.units.values():
        _collect_unit_refs(inventory, owner, unit)
    for upgrade in faction.upgrades.values():
        _collect_upgrade_refs(inventory, owner, upgrade)


def _collect_unit_refs(inventory: AssetInventory, owner_base: str, unit: UnitDef) -> None:
    owner = f"{owner_base}/units/{unit.name}"
    _add_ref(inventory, owner, unit.image)
    _add_ref(inventory, owner, unit.image_cancel)
    for sound in unit.selection_sounds:
        _add_ref(inventory, owner, sound)
    for sound in unit.command_sounds:
        _add_ref(inventory, owner, sound)
    for skill in unit.skills.values():
        _add_ref(inventory, owner, skill.animation)
        for sound in skill.sounds:
            _add_ref(inventory, owner, sound)
        for particle in skill.particles:
            _add_ref(inventory, owner, particle)
        if skill.attack is not None:
            _add_ref(inventory, owner, skill.attack.projectile_particle)
            _add_ref(inventory, owner, skill.attack.splash_particle)
            for sound in skill.attack.sounds:
                _add_ref(inventory, owner, sound)
    for command in unit.commands:
        _add_ref(inventory, owner, command.image)


def _collect_upgrade_refs(inventory: AssetInventory, owner_base: str, upgrade: UpgradeDef) -> None:
    owner = f"{owner_base}/upgrades/{upgrade.name}"
    _add_ref(inventory, owner, upgrade.image)
    _add_ref(inventory, owner, upgrade.image_cancel)


def _add_ref(inventory: AssetInventory, owner: str, ref: Path | None) -> None:
    if ref is None:
        return
    inventory.referenced.append(ref)
    if not ref.exists():
        inventory.unresolved.append(UnresolvedRef(owner=owner, ref=str(ref), resolved=ref))
