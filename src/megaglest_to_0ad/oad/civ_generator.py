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
from pathlib import Path

from lxml import etree

from ..core.config import Settings
from ..core.media_conversion import MediaConversionStats
from ..megaglest.civ_loader import Faction
from .common import humanize_name, town_centre_candidate, unmapped_resources
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
    hue = sum(ord(ch) for ch in civ) % 360
    bg = _hsl_to_rgb(hue, 0.35, 0.30)
    disc = _hsl_to_rgb(hue, 0.55, 0.62)
    radius = size * 0.31
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter: None
        for x in range(size):
            dx, dy = x + 0.5 - size / 2, y + 0.5 - size / 2
            color = disc if dx * dx + dy * dy <= radius * radius else bg
            rows.extend(color)
    raw = bytes(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path = mod_dir / "art/textures/ui/session/portraits/emblems" / f"emblem_{civ}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


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
    del settings  # civ schema is version-stable
    civ = sanitize_mod_name(faction.name)

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
        "AINames": [],
        "SkirmishReplacements": {},
        "SelectableInGameSetup": True,
    }
    path = mod_dir / "simulation/data/civs" / f"{civ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, _write_player_template(faction, mod_dir, civ)


def _write_player_template(faction: Faction, mod_dir: Path, civ: str) -> Path:
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
    etree.SubElement(identity, "GenericName").text = humanize_name(faction.name)
    etree.SubElement(identity, "Icon").text = f"emblems/emblem_{civ}.png"
    etree.SubElement(identity, "Undeletable").text = "false"
    _write_emblem(civ, mod_dir)
    path = mod_dir / "simulation/templates/special/players" / f"{civ}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    tree = etree.ElementTree(root)
    etree.indent(tree, space="  ")
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="utf-8"))
    return path
