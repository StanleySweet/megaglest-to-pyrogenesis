"""CLI end-to-end tests (list-factions, convert, validate)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from megaglest_to_0ad.main import cli


def test_list_factions(layout_b_pack: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["list-factions", str(layout_b_pack)])
    assert result.exit_code == 0, result.output
    assert "layout flat" in result.output
    assert "demo" in result.output


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
    report_text = (mod_dir / "conversion_report.json").read_text(encoding="utf-8")
    # The report ships inside the mod archive, so it must not leak local paths.
    assert str(layout_b_pack) not in report_text
    report = json.loads(report_text)
    assert report["pack"]["layout"] == "flat"
    assert report["pack"]["name"] == "layout_b"
    assert report["factions"]["demo"]["units"] == ["barracks", "grunt"]
    assert report["factions"]["demo"]["buildings"] == ["barracks"]
    assert report["factions"]["demo"]["upgrades"] == ["weaponry"]
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
            "demo",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "factions: demo" in result.output


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


def _logged_records(level: str = "INFO") -> list[dict]:
    """Run ``level`` logging through the CLI's formatter and parse the result."""
    import io
    import logging

    from megaglest_to_0ad.main import _JsonFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_JsonFormatter())
    logger = logging.getLogger("test.cli")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(level)
    return stream, logger


def test_log_lines_are_json_with_extras() -> None:
    """The summary lines carry their numbers in extra, not in the message.

    "Conversion complete" says nothing on its own, so if the formatter stops
    passing extras through, the converter's only progress output goes blank
    and nothing fails.
    """
    import json

    stream, logger = _logged_records()
    logger.info("Conversion complete", extra={"pack": "demo", "units": 12})
    record = json.loads(stream.getvalue().strip())
    assert record["message"] == "Conversion complete"
    assert record["units"] == 12
    assert record["pack"] == "demo"
    assert record["level"] == "INFO"
    assert "logger" in record and "time" in record


def test_log_line_keeps_the_traceback() -> None:
    import json

    stream, logger = _logged_records()
    try:
        raise ValueError("kaboom")
    except ValueError:
        logger.exception("failed")
    record = json.loads(stream.getvalue().strip())
    assert record["message"] == "failed"
    assert "ValueError: kaboom" in record["exception"]


def test_unserialisable_extra_does_not_break_logging() -> None:
    """Extras are arbitrary values -- sets, paths -- so fall back to str()."""
    import json

    stream, logger = _logged_records()
    logger.warning("Unmapped tags", extra={"unit": "archer", "tags": {"a", "b"}})
    record = json.loads(stream.getvalue().strip())
    assert record["unit"] == "archer"
    assert isinstance(record["tags"], str), "a set must be stringified, not dropped"
    assert "a" in record["tags"] and "b" in record["tags"]


def test_configure_logging_is_idempotent() -> None:
    import logging

    from megaglest_to_0ad.main import configure_logging

    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging("INFO")
        configure_logging("DEBUG")
        assert root.handlers == before, "a second call must not add a handler"
    finally:
        root.handlers = before


def test_convert_ships_game_data_licence(layout_b_pack: Path, tmp_path: Path) -> None:
    """A pack under ``techs/`` gets the game data's ``docs/`` licence files.

    MegaGlest's data is CC-BY-SA 3.0, which requires attribution and the
    licence terms to travel with a derivative, so the mod must carry them.
    """
    data = tmp_path / "glest_game"
    pack = data / "techs" / "layout_b"
    shutil.copytree(layout_b_pack, pack)
    (data / "docs").mkdir()
    (data / "docs" / "LICENSE.data.txt").write_text("CC-BY-SA 3.0")
    (data / "docs" / "AUTHORS.data.txt").write_text("authors")
    (data / "docs" / "README.txt").write_text("not a licence")

    result = CliRunner().invoke(
        cli, ["convert", "--megaglest-data", str(pack), "--output", str(tmp_path / "out")]
    )
    assert result.exit_code == 0, result.output
    shipped = sorted(p.name for p in (tmp_path / "out" / "layout_b" / "licenses").iterdir())
    assert shipped == ["AUTHORS.data.txt", "LICENSE.data.txt"]


def test_convert_warns_when_no_licence_found(
    layout_b_pack: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            cli,
            ["convert", "--megaglest-data", str(layout_b_pack), "--output", str(tmp_path)],
        )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "layout_b" / "licenses").exists()
    assert "No licence or attribution files" in caplog.text
