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

# MegaGlest resource -> 0 A.D. resource. ``grace`` is not a tradable 0 A.D.
# resource; it maps to population instead (see ``grace_amount``), so it never
# enters ``Cost/Resources``.
RESOURCE_MAP = {"gold": "metal", "wood": "wood", "stone": "stone", "food": "food"}


def humanize_name(raw: str) -> str:
    """``skirmisher`` -> ``Skirmisher`` (template/tech names)."""
    return re.sub(r"[_-]+", " ", raw).strip().title()


def material_for(target_version: str) -> str:
    """Player material XML: the ``_norm_spec`` variants (modern 0 A.D. set).

    ``player_trans`` was renamed ``basic_trans`` in 0.29; both versions ship
    the ``_norm_spec`` variant the converter targets (actors always declare
    normTex/specTex slots alongside baseTex).
    """
    if target_version.startswith("0.28"):
        return "player_trans_norm_spec.xml"
    return "basic_trans_norm_spec.xml"


def resource_cost(resources: dict[str, int]) -> dict[str, int]:
    """Map MG resources to 0 A.D. resources, dropping zeros and ``grace``.

    ``grace`` is handled by the template layer as population (see
    :func:`grace_amount`); every other custom resource is dropped and
    reported by :func:`unmapped_resources`.
    """
    out: dict[str, int] = {}
    for mg_name, amount in resources.items():
        oad_name = RESOURCE_MAP.get(mg_name)
        if oad_name is None or amount <= 0:
            continue
        out[oad_name] = amount
    return out


def grace_amount(resources: dict[str, int]) -> int:
    """The MegaGlest ``grace`` requirement, mapped to 0 A.D. population.

    Positive on a unit = the population slots it consumes; negative on a
    building = population cap added while it stands. Returns 0 when unused.
    """
    return int(resources.get("grace", 0))


def unmapped_resources(
    resources: dict[str, int], *, grace_is_mapped: bool = False
) -> dict[str, int]:
    """Custom MG resources with no 0 A.D. analog, for conversion warnings.

    ``grace`` maps to population for unit/building templates, so pass
    ``grace_is_mapped=True`` there; in tech costs and starting resources it
    has no analog and is reported too.
    """
    out: dict[str, int] = {}
    for name, amount in resources.items():
        if name in RESOURCE_MAP:
            continue
        if name == "grace" and grace_is_mapped:
            continue
        if amount == 0:
            continue
        out[name] = amount
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
