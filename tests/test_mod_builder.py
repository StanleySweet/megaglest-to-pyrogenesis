"""Mod skeleton, mod.json and name sanitization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from megaglest_to_0ad.oad.mod_builder import (
    build_mod_skeleton,
    default_metadata,
    sanitize_mod_name,
    write_mod_json,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Demo_A10", "demo_a10"),
        ("Demo A10!", "demo_a10"),
        ("Mega Pack", "mega_pack"),
        ("  my_mod-2  ", "my_mod-2"),
    ],
)
def test_sanitize_mod_name(raw: str, expected: str) -> None:
    assert sanitize_mod_name(raw) == expected


def test_sanitize_rejects_empty() -> None:
    with pytest.raises(ValueError):
        sanitize_mod_name("!!!  ")


def test_default_metadata() -> None:
    metadata = default_metadata("Demo_A10")
    assert metadata.name == "demo_a10"
    assert metadata.dependencies == ["0ad=0.28.0"]
    assert metadata.url is None


def test_mod_skeleton_and_json(tmp_path: Path) -> None:
    metadata = default_metadata("Demo_A10")
    mod_dir = build_mod_skeleton(tmp_path, metadata.name)
    assert mod_dir == tmp_path / "demo_a10"
    for rel in (
        "simulation/data/civs",
        "simulation/data/technologies",
        "simulation/templates/units",
        "art/actors/units",
        "art/meshes",
        "art/textures/ui/session/portraits/units",
        "audio/sfx",
    ):
        assert (mod_dir / rel).is_dir(), rel

    mod_json = write_mod_json(mod_dir, metadata)
    payload = json.loads(mod_json.read_text(encoding="utf-8"))
    assert payload["name"] == "demo_a10"
    assert payload["version"] == "1.0.0"
    assert set(payload) == {"name", "version", "label", "description", "dependencies"}
