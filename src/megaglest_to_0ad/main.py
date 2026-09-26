"""Click CLI entry point."""

from __future__ import annotations

import json
from pathlib import Path

import click

from .core.config import Settings
from .core.converter import convert_pack
from .core.errors import ConversionError
from .core.logger import configure_logging, get_logger
from .megaglest.parser import discover_pack

LOGGER = get_logger("cli")

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


@click.group()
def cli() -> None:
    """MegaGlest to 0 A.D. mod converter."""


@cli.command("list-factions")
@click.argument(
    "megaglest_data",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--log-level",
    type=click.Choice(LOG_LEVELS, case_sensitive=False),
    default="INFO",
    show_default=True,
)
def list_factions(megaglest_data: Path, log_level: str) -> None:
    """List factions available in a MegaGlest pack."""
    configure_logging(log_level)
    try:
        pack = discover_pack(megaglest_data)
    except ConversionError as exc:
        raise click.ClickException(str(exc)) from exc
    names = [
        p.name
        for p in sorted(pack.factions_dir.iterdir())
        if p.is_dir() and not p.name.startswith(".")
    ]
    click.echo(f"Pack: {pack.name} (layout {pack.layout.value})")
    for name in names:
        click.echo(f"  {name}")


@cli.command()
@click.option(
    "--megaglest-data",
    "megaglest_data",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Path to the MegaGlest pack root.",
)
@click.option(
    "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("output"),
    show_default=True,
    help="Directory that receives the generated mod folder.",
)
@click.option(
    "--factions",
    multiple=True,
    default=("all",),
    help="Faction(s) to convert; repeat the flag or pass 'all'.",
)
@click.option(
    "--skip-media",
    is_flag=True,
    default=False,
    help="Skip mesh/texture/audio conversion (mod skeleton + game data only).",
)
@click.option(
    "--rig-bones",
    type=click.IntRange(min=2, max=64),
    default=32,
    show_default=True,
    help="Maximum joints (incl. root) of the synthesized rig per model.",
)
@click.option(
    "--log-level",
    type=click.Choice(LOG_LEVELS, case_sensitive=False),
    default="INFO",
    show_default=True,
)
def convert(
    megaglest_data: Path,
    output: Path,
    factions: tuple[str, ...],
    skip_media: bool,
    rig_bones: int,
    log_level: str,
) -> None:
    """Convert a MegaGlest pack into a 0 A.D. mod."""
    configure_logging(log_level)
    settings = Settings(skip_media=skip_media, rig_bones=rig_bones)
    try:
        report = convert_pack(megaglest_data, output, factions, settings)
    except ConversionError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Converted {report.pack_name} (layout {report.layout.value})")
    click.echo(f"  mod:      {report.mod_dir}")
    click.echo(f"  factions: {', '.join(report.factions)}")
    click.echo(
        f"  units: {report.units}  buildings: {report.buildings}  upgrades: {report.upgrades}"
    )
    click.echo(
        f"  meshes: {report.meshes}  animations: {report.animations}  "
        f"textures: {report.textures}  sounds: {report.sounds}  "
        f"music: {report.music}  particles: {report.particles}  maps: {report.maps}"
    )
    click.echo(f"  generated: {', '.join(report.generated_files)}")


@cli.command()
@click.argument("mod_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--meshes",
    is_flag=True,
    default=False,
    help="Also validate every DAE against the 0 A.D. importer contract.",
)
def validate(mod_dir: Path, meshes: bool) -> None:
    """Check a generated mod against the 0 A.D. acceptance checklist.

    Covers the local, offline part of the checklist: mod.json presence and
    fields, name/folder match, no wrapper directory, and the
    ``ignoreInCompatibilityChecks`` rule. With ``--meshes``, every DAE under
    ``art/`` is additionally checked against the engine's COLLADA importer
    contract (single instanced object, POSITION/NORMAL/TEXCOORD inputs,
    in-range indices).
    """
    failures: list[str] = []
    mod_json = mod_dir / "mod.json"
    if not mod_json.is_file():
        failures.append(f"mod.json missing at mod root: {mod_json}")
    else:
        try:
            metadata = json.loads(mod_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"mod.json is not valid JSON: {exc}")
        else:
            for field in ("name", "version", "label", "description", "dependencies"):
                if field not in metadata:
                    failures.append(f"mod.json missing required field {field!r}")
            if metadata.get("name") != mod_dir.name:
                failures.append(
                    f"mod.json name {metadata.get('name')!r} does not match folder "
                    f"name {mod_dir.name!r}"
                )
            has_simulation = (mod_dir / "simulation").is_dir()
            if has_simulation and metadata.get("ignoreInCompatibilityChecks"):
                failures.append(
                    "ignoreInCompatibilityChecks is set on a mod with simulation/ content"
                )
    if failures:
        for failure in failures:
            click.echo(f"[FAIL] {failure}")
        raise click.ClickException("mod validation failed")
    click.echo(f"OK: {mod_dir.name} is a valid mod root (mod.json and layout checks passed)")
    if meshes:
        from .oad.dae_validator import validate_mod_meshes

        audit = validate_mod_meshes(mod_dir)
        click.echo(f"meshes: {audit.passed}/{audit.checked} importable")
        for failure in audit.failures:
            click.echo(f"[FAIL] {failure}")
        if not audit.ok:
            raise click.ClickException("mesh validation failed")


if __name__ == "__main__":
    cli()
