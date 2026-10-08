"""civ.json generation (``simulation/data/civs/{civ}.json``).

Schema follows Millennium A.D. 0.28 / 0 A.D. 0.29 civ files; key order is
exact: Code, Culture, Music, CivBonuses, WallSets, StartEntities, AINames,
SkirmishReplacements, SelectableInGameSetup.
"""

from __future__ import annotations

import json
import logging
import struct
import zlib
from collections.abc import Callable
from pathlib import Path

from lxml import etree

from ..core.config import Settings
from ..core.media_conversion import MediaConversionStats
from ..megaglest.civ_loader import Faction
from .common import (
    humanize_name,
    town_centre_candidate,
    unmapped_resources,
    write_xml,
)
from .mod_builder import sanitize_mod_name

LOGGER = logging.getLogger(__name__)


def _write_emblem(civ: str, mod_dir: Path) -> Path:
    """Write a deterministic placeholder emblem (``portraits/emblems/``).

    The 0.28+ Identity schema requires ``Icon`` on the player template; the
    engine resolves it under ``art/textures/ui/session/portraits/`` (see
    ``session.js``: ``"stretched:session/portraits/" + icon``). The pack ships
    no emblem art, so a 128x128 two-tone PNG is generated: a solid disc in a
    hue derived from the civ code, on a lighter ground. Stdlib only.
    """
    size = 128
    hue = _civ_hue(civ)
    bg = _hsl_to_rgb(hue, 0.35, 0.30)
    disc = _hsl_to_rgb(hue, 0.55, 0.62)
    radius = size * 0.31

    def pixel(dx: float, dy: float) -> bytes:
        return disc if dx * dx + dy * dy <= radius * radius else bg

    path = mod_dir / "art/textures/ui/session/portraits/emblems" / f"emblem_{civ}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png_rgba(size, pixel))
    return path


def _write_minimap_background(civ: str, mod_dir: Path) -> Path:
    """Write the session minimap background (``icons/bkg/``).

    ``gui/session/minimap/MiniMapPanel.js`` draws
    ``session/icons/bkg/background_circle_{civ}.png`` behind the minimap, and
    the pack has no art for it. The stock ones are a dark 512x512 disc with
    transparent corners; this one is the same, with a ring in the civ's
    emblem hue.
    """
    size = 512
    hue = _civ_hue(civ)
    ground = _hsl_to_rgb(hue, 0.12, 0.14)
    ring = _hsl_to_rgb(hue, 0.30, 0.24)
    clear = b"\x00\x00\x00\x00"
    outer = size / 2
    ring_out, ring_in = outer * 0.94, outer * 0.88

    def pixel(dx: float, dy: float) -> bytes:
        dist2 = dx * dx + dy * dy
        if dist2 > outer * outer:
            return clear
        if ring_in * ring_in <= dist2 <= ring_out * ring_out:
            return ring
        return ground

    path = mod_dir / "art/textures/ui/session/icons/bkg" / f"background_circle_{civ}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png_rgba(size, pixel))
    return path


def _png_rgba(size: int, pixel: Callable[[float, float], bytes]) -> bytes:
    """Encode a square 8-bit RGBA PNG; ``pixel`` gets the offset from centre."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter: None
        for x in range(size):
            rows.extend(pixel(x + 0.5 - size / 2, y + 0.5 - size / 2))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def _civ_hue(civ: str) -> int:
    """Deterministic hue for a civ code, shared by its emblem and minimap art."""
    return sum(ord(ch) for ch in civ) % 360


def _hsl_to_rgb(hue: float, sat: float, light: float) -> bytes:
    c = (1 - abs(2 * light - 1)) * sat
    x = c * (1 - abs((hue / 60.0) % 2 - 1))
    m = light - c / 2
    if hue < 60:
        r, g, b = c, x, 0.0
    elif hue < 120:
        r, g, b = x, c, 0.0
    elif hue < 180:
        r, g, b = 0.0, c, x
    elif hue < 240:
        r, g, b = 0.0, x, c
    elif hue < 300:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x
    rgb = bytes(round((v + m) * 255) for v in (r, g, b))
    return rgb + b"\xff"  # 8-bit RGBA, opaque alpha


def generate_civ(
    faction: Faction,
    mod_dir: Path,
    stats: MediaConversionStats,
    settings: Settings,
) -> tuple[Path, Path]:
    """Write the faction's civ definition; returns (civ JSON, player template).

    The player template (``simulation/templates/special/players/{civ}.xml``)
    is what makes the civ playable: the engine loads it for every player
    entity and aborts the match with "Failed to load entity template
    'special/players/{civ}'" when it is missing — no units, no animation.
    """
    civ = sanitize_mod_name(faction.name)
    display_name = dict(settings.civ_names).get(civ, humanize_name(faction.name))

    dropped = unmapped_resources(faction.starting_resources, grace_is_mapped=False)
    if dropped:
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(dropped.items()))
        message = (
            f"starting resources '{summary}' have no 0 A.D. civ-level analog "
            "(starting wealth/population come from the game setup); dropped"
        )
        stats.warnings.append(message)
        LOGGER.warning("civ %s: %s", civ, message)

    start_entities: list[dict[str, object]] = []
    for name, count in faction.starting_units:
        unit = faction.units.get(name)
        if unit is None:
            stats.warnings.append(f"starting unit {name!r} not found; skipped")
            continue
        if count <= 0:
            continue
        sub = "structures" if unit.is_building else "units"
        entry: dict[str, object] = {"Template": f"{sub}/{civ}/{name}"}
        if count > 1:
            entry["Count"] = count
        start_entities.append(entry)
    tc = town_centre_candidate(faction)
    if tc is not None and not any(
        e.get("Template") == f"structures/{civ}/{tc.name}" for e in start_entities
    ):
        start_entities.insert(0, {"Template": f"structures/{civ}/{tc.name}"})

    payload: dict[str, object] = {
        "Code": civ,
        "Culture": civ,
        "Music": [{"File": file, "Type": "peace"} for file in stats.music_files],
        "CivBonuses": [],
        "WallSets": ["structures/wallset_palisade"],
        "StartEntities": start_entities,
        # Must be non-empty: gamesettings' PlayerName re-picks an AI name until
        # one sticks, so an empty list hangs launch ("Infinite loop picking
        # random items"). Duplicates get a " (2)" suffix from the engine.
        "AINames": [display_name],
        "SkirmishReplacements": {},
        "SelectableInGameSetup": True,
    }
    path = mod_dir / "simulation/data/civs" / f"{civ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, _write_player_template(display_name, mod_dir, civ)


def _write_player_template(display_name: str, mod_dir: Path, civ: str) -> Path:
    """``special/players/{civ}.xml``: parent ``template_player`` (public mod).

    Mirrors the public per-civ player templates. The 0.28+ Identity schema
    requires Civ, GenericName and Icon; the Icon resolves under
    ``art/textures/ui/session/portraits/``, so a generated emblem is written
    alongside (see ``_write_emblem``).
    """
    root = etree.Element("Entity")
    root.set("parent", "template_player")
    identity = etree.SubElement(root, "Identity")
    etree.SubElement(identity, "Civ").text = civ
    etree.SubElement(identity, "GenericName").text = display_name
    etree.SubElement(identity, "Icon").text = f"emblems/emblem_{civ}.png"
    etree.SubElement(identity, "Undeletable").text = "false"
    _write_emblem(civ, mod_dir)
    _write_minimap_background(civ, mod_dir)
    path = mod_dir / "simulation/templates/special/players" / f"{civ}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xml(path, root)
    return path
