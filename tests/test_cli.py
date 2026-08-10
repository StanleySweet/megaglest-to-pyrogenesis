"""CLI end-to-end tests (list-factions, convert, validate)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from megaglest_to_0ad.main import cli


def test_list_factions(layout_b_pack: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["list-factions", str(layout_b_pack)])
    assert result.exit_code == 0, result.output
    assert "layout flat" in result.output
    assert "elves" in result.output


def test_convert_end_to_end(layout_b_pack: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "convert",
            "--megaglest-data",
            str(layout_b_pack),
            "--output",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    mod_dir = tmp_path / "layout_b"
    assert (mod_dir / "mod.json").is_file()
    report = json.loads((mod_dir / "conversion_report.json").read_text(encoding="utf-8"))
    assert report["pack"]["layout"] == "flat"
    assert report["pack"]["name"] == "layout_b"
    assert report["factions"]["elves"]["units"] == ["barracks", "elf"]
    assert report["factions"]["elves"]["buildings"] == ["barracks"]
    assert report["factions"]["elves"]["upgrades"] == ["weaponry"]
    assert report["assets"]["meshes"] == 3
    assert "units: 1  buildings: 1  upgrades: 1" in result.output


def test_convert_selected_faction(layout_b_pack: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "convert",
            "--megaglest-data",
            str(layout_b_pack),
            "--output",
            str(tmp_path),
            "--factions",
            "elves",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "factions: elves" in result.output


def test_convert_unknown_faction_fails(layout_b_pack: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "convert",
            "--megaglest-data",
            str(layout_b_pack),
            "--output",
            str(tmp_path),
            "--factions",
            "atlanteans",
        ],
    )
    assert result.exit_code != 0
    assert "atlanteans" in result.output


def test_validate_passes(layout_b_pack: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    mod_dir = tmp_path / "layout_b"
    (mod_dir / "simulation").mkdir(parents=True)
    (mod_dir / "mod.json").write_text(
        json.dumps(
            {
                "name": "layout_b",
                "version": "1.0.0",
                "label": "x",
                "description": "y",
                "dependencies": ["0ad=0.29.0"],
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(cli, ["validate", str(mod_dir)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_validate_fails_on_name_mismatch(tmp_path: Path) -> None:
    runner = CliRunner()
    mod_dir = tmp_path / "wrong_name"
    mod_dir.mkdir()
    (mod_dir / "mod.json").write_text(
        json.dumps(
            {
                "name": "right_name",
                "version": "1.0.0",
                "label": "x",
                "description": "y",
                "dependencies": ["0ad=0.29.0"],
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(cli, ["validate", str(mod_dir)])
    assert result.exit_code != 0
    assert "does not match folder" in result.output
