# MegaGlest to 0 A.D. Converter

Converts MegaGlest mega packs into 0 A.D. mod directories: models, textures, audio, particle systems, actors, unit and structure templates, technologies, and civ definitions.

Coverage has gaps. The tool skips maps, drops MegaGlest resources that have no 0 A.D. equivalent, and logs the assets that need a manual fix. Read the conversion log before shipping a mod.

## Requirements

- Python 3.11 or newer
- `ffmpeg` on PATH for audio conversion. The Python dependencies do not include it.
- 0 A.D., for the optional archive build step only

## Install

```bash
git clone https://github.com/StanleySweet/megaglest-to-pyrogenesis.git
cd megaglest-to-pyrogenesis
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

This installs the `megaglest-to-pyrogenesis` command. `python -m megaglest_to_0ad` runs the same CLI.

## Usage

### List the factions in a pack

```bash
megaglest-to-pyrogenesis list-factions /path/to/megaglest-pack
```

### Convert every faction

```bash
megaglest-to-pyrogenesis convert \
  --megaglest-data /path/to/megaglest-pack \
  --output output
```

The command writes one mod folder under `--output` and prints the generated file list.

### Convert selected factions

`--factions` takes one value per flag:

```bash
megaglest-to-pyrogenesis convert \
  --megaglest-data /path/to/megaglest-pack \
  --output output \
  --factions demo \
  --factions romans
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--megaglest-data` | *(required)* | Path to the MegaGlest pack root |
| `--output` | `output` | Directory that receives the generated mod folder |
| `--factions` | `all` | Faction to convert; repeat the flag per faction, or pass `all` |
| `--skip-media` | off | Skip mesh, texture, and audio conversion, and generate game data only |
| `--rig-bones` | `32` | Maximum joints, root included, of the synthesized rig per model (2-64) |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

### Validate the result

`validate` runs offline. It checks `mod.json` presence, required fields, the name against the folder name, and the `ignoreInCompatibilityChecks` rule:

```bash
megaglest-to-pyrogenesis validate output/demo_tech
```

Add `--meshes` to check every DAE against the 0 A.D. COLLADA importer contract:

```bash
megaglest-to-pyrogenesis validate output/demo_tech --meshes
```

## Build a distributable mod

The archive build needs a local 0 A.D. install and runs after `convert`. Set `MOD` to the folder name that `convert` produced, because 0 A.D. rejects a mod whose `mod.json` name and folder name differ:

```bash
MOD=demo_tech
"/path/to/0ad/binaries/system/pyrogenesis" \
  -mod=mod \
  -mod=public \
  -mod="$MOD" \
  -archivebuild="output/$MOD" \
  -archivebuild-output="$MOD.pyromod" \
  -archivebuild-compress
```

The unarchived folder loads in 0 A.D. on its own. Build the archive for distribution and load times.

## Project structure

```
.
├── src/megaglest_to_0ad/
│   ├── __main__.py
│   ├── main.py          # CLI: list-factions, convert, validate
│   ├── converters/      # G3D to DAE, textures, audio, rig synthesis
│   ├── core/            # orchestration, config, logging, media dispatch
│   ├── megaglest/       # pack discovery, parsing, civ loading, asset inventory
│   ├── oad/             # 0 A.D. XML and JSON generation, mesh validation
│   └── utils/
├── tests/               # test suite and fixtures
├── tools/               # one-off inspection scripts
├── vendor/g3d/          # G3D reader and format notes, GPLv3, from MegaGlest
├── pyproject.toml
└── README.md
```

## Development

```bash
python -m pytest
python -m ruff check src/ tests/
```

Regenerate the test fixtures after editing the generator:

```bash
python tools/make_test_fixtures.py
```

## License

Copyright (C) 2026 Stanislas Daniel Claude Dolcini.

GPLv3. The license text sits in `LICENSE`, and `vendor/g3d/` keeps its own copy alongside the G3D reader and format notes taken from MegaGlest.

MegaGlest packs and 0 A.D. content ship under their own licenses. This repository grants no rights to them. Confirm the license of a pack before redistributing a converted mod.

The repository ships no third-party game assets. `tools/make_test_fixtures.py` generates every binary under `tests/fixtures/` from code, and `tests/test_fixture_assets.py` fails if a committed fixture stops matching the generator.

## Credits

- MegaGlest: https://megaglest.org
- 0 A.D.: https://play0ad.com
