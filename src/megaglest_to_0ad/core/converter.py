"""High-level conversion orchestration."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..core.config import Settings
from ..core.errors import PackStructureError
from ..core.media_conversion import convert_faction_media
from ..megaglest.asset_inventory import AssetInventory, build_inventory
from ..megaglest.civ_loader import Faction, load_faction
from ..megaglest.parser import LayoutKind, MegaglestPack, discover_pack
from ..oad.actor_generator import generate_actors
from ..oad.civ_generator import generate_civ
from ..oad.mod_builder import (
    ModMetadata,
    build_mod_skeleton,
    default_metadata,
    write_mod_json,
)
from ..oad.tech_generator import generate_techs
from ..oad.template_generator import generate_templates

LOGGER = logging.getLogger(__name__)


@dataclass
class ConversionReport:
    """Summary of a conversion run: converted-output counts + generated files.

    ``meshes/textures/sounds/music`` count files actually written into the mod
    (DAEs, PNGs, OGGs) — content-deduped, so they may be well below the source
    pack's referenced-asset counts (which the JSON keeps under ``assets``).
    """

    pack_name: str
    layout: LayoutKind
    factions: list[str]
    units: int
    buildings: int
    upgrades: int
    meshes: int
    animations: int
    textures: int
    sounds: int
    music: int
    particles: int
    maps: int
    referenced_assets: int
    mod_dir: Path
    generated_files: list[str] = field(default_factory=list)


def convert_pack(
    megaglest_data: Path,
    output: Path,
    factions: tuple[str, ...] = ("all",),
    settings: Settings | None = None,
) -> ConversionReport:
    """Convert the pack at ``megaglest_data`` into a 0 A.D. mod skeleton.

    Pipeline: discover pack -> load factions -> inventory + reference
    validation -> mod skeleton + ``mod.json`` -> media conversion
    (mesh/texture/audio, unless ``settings.skip_media``) -> game-data
    generation (civ JSON, actors, simulation templates, technologies) ->
    ``conversion_report.json``.
    """
    settings = settings or Settings.from_env()
    LOGGER.info("Starting conversion", extra={"root": str(megaglest_data)})
    pack = discover_pack(megaglest_data)
    _load_factions(pack, factions)
    inventory = build_inventory(pack)
    metadata = default_metadata(pack.name, settings.target_version, settings.mod_version)
    mod_dir = build_mod_skeleton(output, metadata.name)
    generated = [str(write_mod_json(mod_dir, metadata).relative_to(mod_dir))]
    media_stats = [
        convert_faction_media(faction, mod_dir, settings, pack.resources_dir)
        for faction in pack.factions.values()
    ]
    for stats in media_stats:
        generated.extend(stats.generated)
    for faction, stats in zip(pack.factions.values(), media_stats):
        civ_path, player_path = generate_civ(faction, mod_dir, stats, settings)
        generated.append(str(civ_path.relative_to(mod_dir)))
        generated.append(str(player_path.relative_to(mod_dir)))
        generated.extend(
            str(p.relative_to(mod_dir)) for p in generate_actors(faction, mod_dir, stats, settings)
        )
        generated.extend(
            str(p.relative_to(mod_dir))
            for p in generate_templates(faction, mod_dir, stats, settings)
        )
        generated.extend(
            str(p.relative_to(mod_dir)) for p in generate_techs(faction, mod_dir, stats, settings)
        )

    unit_counts = _count_entities(pack)
    converted = {
        "meshes": sum(
            len(result.mesh_daes) for stats in media_stats for result in stats.models.values()
        ),
        "animations": sum(stats.animation_count for stats in media_stats),
        "textures": sum(stats.textures for stats in media_stats),
        "sounds": sum(stats.sounds for stats in media_stats),
        "music": sum(stats.music for stats in media_stats),
    }
    report = ConversionReport(
        pack_name=pack.name,
        layout=pack.layout,
        factions=sorted(pack.factions),
        units=unit_counts["units"],
        buildings=unit_counts["buildings"],
        upgrades=unit_counts["upgrades"],
        meshes=converted["meshes"],
        animations=converted["animations"],
        textures=converted["textures"],
        sounds=converted["sounds"],
        music=converted["music"],
        particles=len(inventory.particles),
        maps=len(inventory.maps),
        referenced_assets=len(inventory.referenced),
        mod_dir=mod_dir,
        generated_files=generated,
    )
    report_entry = str((mod_dir / "conversion_report.json").relative_to(mod_dir))
    generated.append(report_entry)
    _write_report(mod_dir, pack, inventory, metadata, generated, converted)
    report.generated_files = generated
    LOGGER.info(
        "Conversion complete",
        extra={
            "pack": pack.name,
            "layout": pack.layout.value,
            "mod_dir": str(mod_dir),
            "units": report.units,
            "buildings": report.buildings,
            "upgrades": report.upgrades,
        },
    )
    return report


def _load_factions(pack: MegaglestPack, requested: tuple[str, ...]) -> None:
    available = sorted(
        p.name for p in pack.factions_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if "all" in requested:
        names = available
    else:
        names = [faction for faction in requested if faction != "all"]
    for name in names:
        if name not in available:
            raise PackStructureError(
                f"Faction {name!r} not found in {pack.factions_dir}; available: {available}"
            )
        pack.factions[name] = load_faction(pack, pack.factions_dir / name)


def _count_entities(pack: MegaglestPack) -> dict[str, int]:
    units = buildings = upgrades = 0
    for faction in pack.factions.values():
        for unit in faction.units.values():
            if unit.is_building:
                buildings += 1
            else:
                units += 1
        upgrades += len(faction.upgrades)
    return {"units": units, "buildings": buildings, "upgrades": upgrades}


def _write_report(
    mod_dir: Path,
    pack: MegaglestPack,
    inventory: AssetInventory,
    metadata: ModMetadata,
    generated: list[str],
    converted: dict[str, int],
) -> Path:
    payload = {
        "tool": "megaglest-to-pyrogenesis",
        "version": __version__,
        "pack": {
            "name": pack.name,
            "layout": pack.layout.value,
            "root": str(pack.root),
        },
        "target": {"mod_name": metadata.name, "0ad_version": metadata.dependencies[0]},
        "factions": _faction_summary(pack),
        "assets": {
            "meshes": len(inventory.meshes),
            "textures": len(inventory.textures),
            "sounds": len(inventory.sounds),
            "music": len(inventory.music),
            "particles": len(inventory.particles),
            "maps": len(inventory.maps),
            "referenced": len(inventory.referenced),
        },
        "converted": converted,
        "generated_files": generated,
    }
    path = mod_dir / "conversion_report.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _faction_summary(pack: MegaglestPack) -> dict[str, object]:
    summary: dict[str, object] = {}
    for name, faction in sorted(pack.factions.items()):
        faction_data: Faction = faction
        summary[name] = {
            "units": sorted(faction_data.units),
            "buildings": sorted(u for u, unit in faction_data.units.items() if unit.is_building),
            "upgrades": sorted(faction_data.upgrades),
            "starting_resources": faction_data.starting_resources,
            "starting_units": faction_data.starting_units,
        }
    return summary
