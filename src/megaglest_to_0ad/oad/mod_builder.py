"""0 A.D. mod skeleton and mod.json generation.

Follows the 0 A.D. mod layout (verified against the 0.29 public mod and the
in-repo plan): civs under ``simulation/data/civs/``, techs under
``simulation/data/technologies/``, templates under ``simulation/templates/``,
actors under ``art/actors/``, meshes under ``art/meshes/``, portraits under
``art/textures/ui/session/portraits/``, sounds under ``audio/``.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

MOD_SUBDIRS = (
    "simulation/data/civs",
    "simulation/data/technologies",
    "simulation/templates/units",
    "simulation/templates/structures",
    "art/actors/units",
    "art/actors/structures",
    "art/actors/props",
    "art/particles",
    "art/meshes",
    "art/animation",
    "art/skeletons",
    "art/textures/skins/units",
    "art/textures/particles",
    "art/textures/terrain",
    "art/textures/ui/session/portraits/units",
    "art/textures/ui/session/portraits/technologies",
    "art/variants",
    "audio/sfx",
    "audio/music",
    "audio/groups",
)

_NAME_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass(frozen=True)
class ModMetadata:
    """Content of the output ``mod.json`` (fields per 0 A.D. rules)."""

    name: str
    version: str
    label: str
    description: str
    dependencies: list[str]
    url: str | None = None


def sanitize_mod_name(raw: str) -> str:
    """Derive a valid 0 A.D. mod name (lowercase alnum, underscore, dash)."""
    cleaned = _NAME_RE.sub("_", raw.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        raise ValueError(f"Cannot derive a valid mod name from {raw!r}")
    return cleaned


def default_metadata(
    pack_name: str,
    target_version: str = "0.28.0",
    mod_version: str = "1.0.0",
) -> ModMetadata:
    """Default metadata derived from the pack name."""
    return ModMetadata(
        name=sanitize_mod_name(pack_name),
        version=mod_version,
        label=f"{pack_name} (converted from MegaGlest)",
        description=f"0 A.D. conversion of the MegaGlest pack {pack_name}.",
        dependencies=[f"0ad={target_version}"],
    )


def build_mod_skeleton(output_root: Path, name: str) -> Path:
    """Create the mod directory and standard subdirectories.

    A previous build of the same mod is removed first: conversion output is
    deterministic, and stale files (e.g. older mesh names) would otherwise
    accumulate in the live mod and the packaged pyromod.
    """
    mod_dir = output_root / name
    if mod_dir.exists():
        shutil.rmtree(mod_dir)
    for rel in MOD_SUBDIRS:
        (mod_dir / rel).mkdir(parents=True, exist_ok=True)
    return mod_dir


def write_mod_json(mod_dir: Path, metadata: ModMetadata) -> Path:
    """Write ``mod.json`` with the required fields."""
    payload: dict[str, object] = {
        "name": metadata.name,
        "version": metadata.version,
        "label": metadata.label,
        "description": metadata.description,
        "dependencies": list(metadata.dependencies),
    }
    if metadata.url is not None:
        payload["url"] = metadata.url
    path = mod_dir / "mod.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
