"""Shared helpers for the Phase 4 output generators.

Unit conversions scale MegaGlest numbers to 0 A.D. magnitudes. The factors
are documented heuristics, chosen so a typical pack lands in 0 A.D.'s ranges
(citizen HP ~60-100, melee range 3-6, WalkSpeed 6-20, research 20-60 s):

- HP / damage / armor: ``HP_SCALE`` (10)
- train / build / research time: ``TIME_SCALE`` (10)
- move speed: ``SPEED_SCALE`` (30)
- MG tile cells -> 0 A.D. metres: ``TILE_METERS`` (4)
"""

from __future__ import annotations

import re

from ..megaglest.civ_loader import Faction, UnitDef

HP_SCALE = 10.0
TIME_SCALE = 10.0
SPEED_SCALE = 30.0
TILE_METERS = 4.0

# MegaGlest resource -> 0 A.D. resource. ``grace`` has no 0 A.D. analog and
# is dropped by the callers.
RESOURCE_MAP = {"gold": "metal", "wood": "wood", "stone": "stone", "food": "food"}


def humanize_name(raw: str) -> str:
    """``grey_elf_archer`` -> ``Grey Elf Archer`` (template/tech names)."""
    return re.sub(r"[_-]+", " ", raw).strip().title()


def material_for(target_version: str) -> str:
    """Player material XML: renamed ``player_trans`` -> ``basic_trans`` in 0.29."""
    if target_version.startswith("0.28"):
        return "player_trans.xml"
    return "basic_trans.xml"


def resource_cost(resources: dict[str, int]) -> dict[str, int]:
    """Map MG resources to 0 A.D. resources, dropping zeros and ``grace``."""
    out: dict[str, int] = {}
    for mg_name, amount in resources.items():
        oad_name = RESOURCE_MAP.get(mg_name)
        if oad_name is None or amount <= 0:
            continue
        out[oad_name] = amount
    return out


def town_centre_candidate(faction: Faction) -> UnitDef | None:
    """The building used as the civ's town centre.

    Prefers the first building in ``starting_units`` (MegaGlest packs start
    with their town centre); falls back to the first building in the faction.
    """
    for name, _count in faction.starting_units:
        unit = faction.units.get(name)
        if unit is not None and unit.is_building:
            return unit
    for unit in faction.units.values():
        if unit.is_building:
            return unit
    return None
