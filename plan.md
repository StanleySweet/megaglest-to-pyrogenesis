# MegaGlest to 0 A.D. Mod Converter — Plan and Engineering Notes

The conversion spec, plus the reasoning behind behaviour that is not obvious
from the code. Open work is tracked as GitHub issues (see **Backlog**); the
checklists below are the delivered record.


## Project Overview

Build a Python 3.11+ CLI tool that converts MegaGlest mega pack data into 0 A.D. mod packages with 1:1 fidelity. The tool must handle asset conversion (3D models, textures, audio), generate game configuration files (civ.json, simulation templates, tech trees), and maintain structural integrity across both game formats.

**Core Goal**: Enable MegaGlest civilization packs to be playable in 0 A.D. with minimal manual intervention.

### Local-First Policy

- **No network access at runtime.** Conversion, validation, and reference lookup operate only on local files.
- The only network step is the initial `pip install -e ".[dev]"` (or pre-downloaded wheels). Everything after that runs offline.
- Format schemas are derived from local ground truth: the installed 0 A.D. `public` mod and the input MegaGlest pack itself. No online docs or tools are fetched during development or conversion.
- Written reference knowledge (mod structure, acceptance checklist, file naming) is integrated directly into this plan; no external documentation is required.
- Reference mods (e.g. a local clone of the Millennium A.D. mod, used as an example of a hand-authored standalone mod) are copied once and kept on disk; they are optional and never downloaded at runtime.

---

## Project Architecture

### Directory Structure
```
.
├── src/megaglest_to_0ad/           # import package; the distribution is megaglest-to-pyrogenesis
│   ├── __main__.py                 # python -m entry point
│   ├── main.py                     # CLI entry point
│   ├── core/
│   │   ├── converter.py            # Main orchestrator
│   │   ├── config.py               # Configuration & paths
│   │   ├── errors.py
│   │   ├── logger.py               # Centralized logging
│   │   └── media_conversion.py     # Per-faction media dispatch
│   ├── megaglest/
│   │   ├── parser.py               # Parse MegaGlest structures (auto-detect layout)
│   │   ├── civ_loader.py           # Load civilization data
│   │   └── asset_inventory.py      # Index assets
│   ├── oad/
│   │   ├── mod_builder.py          # Build 0 A.D. mod structure
│   │   ├── civ_generator.py        # Generate simulation/data/civs/*.json
│   │   ├── actor_generator.py      # Generate art/actors XML
│   │   ├── template_generator.py   # Generate simulation templates
│   │   ├── tech_generator.py       # Generate simulation/data/technologies/*.json
│   │   ├── particle_converter.py   # MegaGlest particle XML → 0 A.D. art/particles
│   │   ├── dae_validator.py        # COLLADA contract checks (validate --meshes)
│   │   └── common.py               # Shared scaling/helpers for generators
│   ├── converters/
│   │   ├── mesh_converter.py       # G3D → DAE (native parser + hand-written COLLADA)
│   │   ├── texture_converter.py    # TGA/BMP/JPG → PNG
│   │   ├── audio_converter.py      # WAV → OGG (ffmpeg via subprocess)
│   │   └── rig.py                  # Morph → skeletal rig synthesis (k-means + Kabsch SVD)
│   └── utils/
├── vendor/g3d/
│   ├── g3d_format.md               # G3D binary format notes (from MegaGlest source)
│   ├── g3dlib.py                   # Reference G3D v4 reader/writer
│   └── LICENSE                     # GPLv3 (format spec derived from MegaGlest)
├── tests/
│   ├── test_converters.py
│   ├── test_generators.py
│   ├── test_fixture_assets.py      # fixtures must match the generator byte for byte
│   └── fixtures/                   # Generated test data (kept local, committed)
├── tools/
│   ├── make_test_fixtures.py       # Generates every binary under tests/fixtures/
│   └── validate_*.py, verify_*.py  # One-off inspection scripts
├── pyproject.toml                  # Single source of dependency truth
├── uv.lock
├── LICENSE                         # GPLv3
└── README.md
```

### Python Environment
```bash
# Setup (the ONLY step that touches the network; afterwards everything is local)
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

---

## Requirements & Dependencies

### Core Requirements (`pyproject.toml`)

`pyproject.toml` is the only place dependencies are declared; do not add a
second list here. Runtime: `click`, `pydantic`, `pydantic-settings`,
`python-json-logger`, `Pillow`, `lxml`, `numpy`. Dev extra: `ruff`, `pytest`,
`pytest-cov`, `pycollada` (used to cross-check the hand-written COLLADA output).

Two deliberate omissions: there is no `pydub`, because it is uninstallable on
Python 3.13+ (its `audioop` dependency left the stdlib) — ffmpeg is driven
through `subprocess` instead. There is no `trimesh`: the G3D parser and the
COLLADA writer are both hand-written against the format spec, so nothing else
is needed for 3D.

### Local Binaries (installed once via system package manager; no runtime downloads)
- **ffmpeg**: invoked directly through `subprocess` for WAV → OGG encoding; must be on `PATH`.
- **0 A.D. (pyrogenesis)**: used only for the optional `validate` step (launch the mod locally) and to reference the `public` mod files.
- **Blender**: optional, for manual mesh repair only. NOT part of the conversion pipeline (see Mesh Conversion below).

### Vendored Material (kept in-repo, no fetching)
- **G3D format spec**: `vendor/g3d/g3d_format.md`, derived from the G3D reader in the MegaGlest source (`source/shared_lib/sources/graphics/model.cpp`, GPLv3 — license retained in `vendor/g3d/LICENSE`). MegaGlest `.g3d` is a bespoke binary format, unrelated to the libgdx "g3dj/g3db" formats.
- **0 A.D. written references**: the relevant 0 A.D. wiki knowledge (mod structure, mod.json rules, install & launch, archive builder, acceptance checklist, file naming conventions, licensing) is written into this plan itself — no external docs required.
- **Local reference mod**: optional one-time copy of a standalone 0 A.D. mod (e.g. Millennium A.D.) used as an example of hand-authored civ/template/actor files. Never fetched at runtime.

---

## Input: MegaGlest Pack Structure

The converter must auto-detect the pack layout. Two layouts exist in the wild; both must be supported.

### Layout A: Classic Glest/MegaGlest pack (per the official mod-pack layout)
```
megaglest-pack/
├── techs/
│   └── {tech_name}/
│       ├── {tech_name}.xml            # tech-tree definition
│       ├── factions/
│       │   └── {faction_name}/
│       │       ├── {faction_name}.xml # civilization metadata
│       │       ├── loading_screen.*   # jpg/png/tga
│       │       ├── units/
│       │       │   └── {unit_name}/
│       │       │       ├── {unit_name}.xml
│       │       │       ├── models/    # *.g3d (+ textures)
│       │       │       ├── skills/    # attack/move/die/etc. XML
│       │       │       ├── sounds/    # *.wav
│       │       │       └── images/    # *.bmp/*.png/*.tga
│       │       ├── buildings/
│       │       │   └── {building_name}/
│       │       │       ├── {building_name}.xml
│       │       │       ├── models/
│       │       │       ├── skills/
│       │       │       └── sounds/
│       │       ├── upgrades/
│       │       │   └── {upgrade_name}/
│       │       └── resources/
│       │           └── {resource_name}/
│       └── resources/
│           └── {resource_name}/
├── tilesets/
│   └── {tileset_name}/
│       ├── {tileset_name}.xml
│       └── terrain/                    # *.tga/*.png
├── maps/                               # *.mgm
└── scenarios/                          # *.gbm
```

### Layout B: Flat layout (community packs)
```
megaglest-pack/
├── factions/
│   └── {faction_name}/
│       ├── {faction_name}.xml          # some packs name this tech.xml
│       ├── loading_screen.*
│       ├── units/{unit_name}/…         # same subfolders as Layout A
│       ├── buildings/{building_name}/…
│       └── upgrades/{upgrade_name}/…
└── tilesets/…
```

### Notes
- Faction/unit/building/tech XML schemas vary between packs. The parser must be tolerant: read the XML generically (element names per file, not a hardcoded schema) and map known fields (`skills`, `resources`, `sounds`, `model` references, HP/armor/damage) defensively, logging unknown fields.
- `tileset/` (singular) does not exist in real packs — the directory is `tilesets/`.
- Asset paths inside MegaGlest XML are relative to the pack root; resolve and validate them during inventory (broken references are conversion errors).

---

## Output: 0 A.D. Mod Structure

Target: 0 A.D. **0.29.x** (verify every schema against the locally installed `public` mod — fields change between versions).

### Target Directory Layout
```
0ad-megaglest-mod/
├── mod.json                           # REQUIRED; name must match folder name
├── simulation/
│   ├── data/
│   │   ├── civs/
│   │   │   ├── {civ_code}.json        # Civilization definition
│   │   │   └── ...
│   │   └── technologies/
│   │       ├── {tech_name}.json
│   │       └── ...
│   └── templates/
│       ├── units/
│       │   └── {civ_code}/
│       │       ├── {unit_name}.xml
│       │       └── ...
│       ├── structures/
│       │   └── {civ_code}/
│       │       ├── {building_name}.xml
│       │       └── ...
│       ├── special/
│       │   └── players/
│       │       └── {civ_code}.xml     # Player template (parent template_player; required to be playable)
├── art/
│   ├── actors/
│   │   ├── units/
│   │   │   └── {civ_code}/
│   │   │       ├── {unit_name}.xml    # Actor definition
│   │   │       └── ...
│   │   └── structures/
│   │       └── {civ_code}/
│   │           └── ...
│   ├── meshes/
│   │   └── {civ_code}/
│   │       ├── {unit_name}.dae        # Mesh (texture-merged; armature + skin controller when rigged)
│   │       └── ...
│   ├── animation/
│   │   └── {civ_code}/
│   │       ├── {mesh}_{anim}.dae      # One skeleton-animation DAE per skill animation
│   │       └── ...
│   ├── skeletons/
│   │   └── {civ_code}.xml             # Bone-name mapping (PSA slot ↔ standard bone)
│   ├── textures/
│   │   ├── units/{civ_code}/…
│   │   ├── terrain/{terrain_name}.png
│   │   └── ui/session/portraits/
│   │       ├── units/{civ_code}/{unit_name}.png
│   │       └── technologies/{tech_name}.png
│   └── variants/                      # shared variant files (biped, etc.)
│       └── {civ_code}/…
└── audio/                             # NOTE: not "sounds/"
    ├── sfx/
    │   └── {civ_code}/
    │       ├── {unit_name}_attack.ogg (+ optional group XML next to files)
    │       └── ...
    └── music/
```

### Layout Rules (verified against 0 A.D. 0.29 public mod)
- Top-level dirs of a mod are a subset of: `art`, `audio`, `autostart`, `campaigns`, `gamesettings`, `globalscripts`, `gui`, `l10n`, `maps`, `shaders`, `simulation` (+ `mod.json`). There is **no** top-level `civs/`, `textures/`, or `sounds/`.
- Civ definitions: `simulation/data/civs/{civ}.json`.
- Technologies: `simulation/data/technologies/{tech}.json` (JSON, lowercase keys — see below).
- Templates: `simulation/templates/{units|structures|...}/{civ}/{name}.xml`, root element `<Entity parent="...">`.
- Actors: `art/actors/{units|structures}/{civ}/{name}.xml`. Paths inside actors are relative: `mesh` → `art/meshes/`, `texture file` → `art/textures/`, `variant file` → `art/variants/`, `material` → `art/materials/`, prop actors → `art/actors/`.
- Animations: `art/animation/{civ}/{mesh}_{anim}.dae` (one per wired skill animation) plus `art/skeletons/{civ}.xml` (bone-name mapping; the engine loads every skeleton XML in the mod stack). Actor variants carry an `<animations>` block before `<mesh>`: `file` relative to `art/animation/`, `speed="100"` (integer percent — ObjectBase.cpp), `event` on attack animations (MegaGlest `attack-start-time`, both progress fractions).
- Textures: `art/textures/` (mesh textures, terrain). Portrait icons: `art/textures/ui/session/portraits/units/…` and `…/technologies/…` (template `<Icon>` values are relative to that portraits dir).
- Audio: `audio/` with OGG Vorbis files; volume/pitch grouping uses small XML files placed next to the sounds (see public `audio/groups/`).
- Terrain: `art/terrains/*.xml` definitions referencing textures in `art/textures/terrain/`.

### Installing & Launching
- User mods live in a per-OS folder; the mod directory name must equal `mod.json` `name`:
  - macOS: `~/Library/Application Support/0ad/mods/`
  - Linux: `~/.local/share/0ad/mods/`
- Launch with the mod loaded: `pyrogenesis -mod=public -mod=megaglest_egyptian …` (or enable it in **Tools & Options > Mod Selection**).

Optional distribution step (local 0 A.D. binary): build a `.pyromod` with the archive builder — it also converts `png/tga → dds`, `dae → psa/pmd`, and `xml → xmb`:
```sh
pyrogenesis -mod=mod -mod=public -mod=mymod \
  -archivebuild=binaries/data/mods/mymod \
  -archivebuild-output=mymod.pyromod -archivebuild-compress
```
The unarchived folder mod loads in 0 A.D. without this step; the archive is only for distribution/performance. To publish on mod.io, rename the built `.pyromod` to `{modname}.zip` (the archive already has the correct layout). Publishing itself is an external, online step — out of the converter's scope.

### File Naming Conventions
All generated filenames must follow the 0 A.D. conventions:
- Lowercase only; underscores (not spaces/hyphens); no parentheses/brackets or other special characters.
- Two-digit numbers (`_01`, not `_1`).
- Name order: `<general>_<civ>_<type>_<extra>_<variation>`; omit any portion already implied by the folder path (e.g. drop `units/` from the template file name).
- Standard abbreviation tables: civs (athen, brit, celt, gaul, hele, iber, kart, kush, mace, pers, ptol, rome, sele, spart); units — `a`=advanced, `b`=basic, `e`=elite, `c`=champion, `r`=rider; types — `isw`=infantry swordsman, `cc`=civic center, etc. The converter generates codes from MegaGlest faction/unit names — enforce the general rules and uniqueness; the tables are guidance for naming, not a required vocabulary.

---

## Key Output Formats

### 1. mod.json (REQUIRED at mod root; `name` must equal the folder name)
```json
{
  "name": "megaglest_egyptian",
  "version": "1.0.0",
  "label": "MegaGlest Egyptian Pack",
  "description": "Converted from a MegaGlest faction pack.",
  "dependencies": ["0ad=0.28.0"]
}
```
Rules:
- Required: `name`, `version`, `label`, `description`, `dependencies`. `url` is optional.
- `name` is a lowercase identifier (usually matches the mod.io URL, e.g. "ja-lang"); `label` is the human-readable title (e.g. "Japanese Language Pack"). Do not confuse them.
- `name`: alphanumeric + underscore + dash only; the folder name (and any zip base name) must match it exactly.
- `version`: digits and at most two periods.
- `dependencies`: array of mod names and/or comparisons (`"0ad=0.29.0"`, `"mymod>=2.0"`, …).
- `ignoreInCompatibilityChecks` (optional, 0 A.D. A25+): boolean; allowed ONLY for mods that do not touch `simulation/` (GUI/art/translations). A conversion mod with templates/civs/techs MUST NOT set it.

### 2. civ.json (simulation/data/civs/{civ}.json)
Format verified against 0 A.D. 0.29 `athen.json` and Millennium A.D. 0.28 `anglo.json`:
```json
{
  "Code": "mg_egyptian",
  "Culture": "mg_egyptian",
  "Music": [],
  "CivBonuses": [],
  "WallSets": ["structures/wallset_palisade"],
  "StartEntities": [
    { "Template": "structures/mg_egyptian/civil_centre" },
    { "Template": "units/mg_egyptian/support_civilian", "Count": 4 }
  ],
  "AINames": [],
  "SkirmishReplacements": {},
  "SelectableInGameSetup": true
}
```
- Template references include the civ folder: `structures/{civ}/civil_centre`, `units/{civ}/…`.
- The following fields from older/imagined civ formats do **not** exist in current 0 A.D.: `Name`, `Description`, `Color`, `Emblem`, `Resources`, `DisabledTechs`, `Modules`. Starting resources and colors are handled by game setup / player settings, not by the civ file. (Verify against the local install's civs before relying on any field.)

### 3. Technology JSON (simulation/data/technologies/{tech}.json)
Format verified against 0 A.D. 0.29 technologies (e.g. `cavalry_health.json`) — lowercase keys:
```json
{
  "genericName": "Technology Name",
  "description": "Description from MegaGlest",
  "cost": { "food": 100, "wood": 50, "stone": 0, "metal": 50 },
  "requirements": { "tech": "phase_town" },
  "requirementsTooltip": "Unlocked in Town Phase.",
  "icon": "technologies/{tech_name}.png",
  "researchTime": 35,
  "tooltip": "Effect description.",
  "modifications": [ { "value": "Health/Max", "multiply": 1.1 } ],
  "affects": ["Infantry", "Cavalry"],
  "soundComplete": "interface/alarm/alarm_upgradearmory.xml"
}
```
- `affects` is a list of template-name matchers and/or unit classes, e.g. `["units/{civ}/*"]`, not `"buildings/*"` (structures are under `structures/`).

### 4. Unit Template XML (simulation/templates/units/{civ}/{unit}.xml)
Structure verified against 0 A.D. 0.29 (`units/athen/infantry_spearman_b.xml`):
```xml
<?xml version="1.0" encoding="utf-8"?>
<Entity parent="civ/{civ}|hoplite|template_unit_infantry_melee_spearman">
  <Identity>
    <SelectionGroupName>units/{civ}/{unit_name}</SelectionGroupName>
    <GenericName>Unit Name</GenericName>
    <SpecificName>Unit Name</SpecificName>
    <Icon>units/{civ}/{unit_name}.png</Icon>
  </Identity>
  <VisualActor>
    <Actor>units/{civ}/{unit_name}.xml</Actor>
  </VisualActor>
  <Health>
    <Max>100</Max>
  </Health>
  <Armour>
    <Hack>5</Hack>
    <Pierce>5</Pierce>
    <Crush>5</Crush>
  </Armour>
  <Attack>
    <Melee>
      <Damage type="piercing">10</Damage>
      <MaxRange>4</MaxRange>
    </Melee>
  </Attack>
</Entity>
```
- `parent` chains are version-specific (0.29 uses `civ/{civ}|class|template_*`); copy the chain style from the local reference templates. There is no `<Identity><Civ>` element and no `units/default` parent.

### 5. Actor XML (art/actors/units/{civ}/{unit}.xml)
Structure verified against 0 A.D. 0.29 (`art/actors/units/athenians/infantry_spearman_b.xml`):
```xml
<?xml version="1.0" encoding="utf-8"?>
<actor version="1">
  <castshadow/>
  <group>
    <variant frequency="1" name="Base">
      <mesh>{civ}/{unit_name}.dae</mesh>
      <textures>
        <texture file="units/{civ}/{unit_name}_diffuse.png" name="baseTex"/>
      </textures>
      <props>
        <prop actor="props/units/weapons/{weapon}.xml" attachpoint="weapon_R"/>
      </props>
    </variant>
  </group>
  <material>player_trans_norm_spec.xml</material>
</actor>
```
- The converter emits **no** `<animations>` blocks, no variants beyond the
  single `Base` variant, and no `<props>`: animation is out of scope, and
  MegaGlest models carry no prop/attachpoint data.
- Relative paths: `mesh` from `art/meshes/`, `texture file` from `art/textures/`.

---

## Conversion Specifications

### 1. Mesh Conversion (MegaGlest G3D → DAE)

**Tool**: in-repo, pure-Python. Parse the G3D binary format using the vendored spec (`vendor/g3d/g3d_format.md`, derived from the MegaGlest source reader), then write COLLADA 1.4.1 with a small dedicated writer (no trimesh; `pycollada` is a dev-only cross-check).

Important: MegaGlest `.g3d` is NOT the libgdx `g3dj`/`g3db` format, and existing "G3D exporters" for libgdx do not read it. Do not integrate them.

**Process**:
- Read header (magic `G3D` + version; v3 and v4 differ), vertex format flags,
  vertices (position/normal/UV/bone index+weight), face indices, texture
  references, and animation frames if present.
- **One instanced object max per scene, empties excepted.** 0 A.D.'s
  importer requires exactly ONE instanced object per DAE (`FindSingleInstance`
  errors with "Too many objects" on more). Meshes that share a texture are
  therefore **merged into one DAE per texture group** (base-pose geometry from
  frame 0 + texture references); groups split on texture, not per mesh.
- **Rigged models (the main game): texture-merged geometry + a synthesized
  armature.** G3D is pure morph — no bones exist — so the rig is synthesized:
  displacement-cluster vertices (k-means over rest position + per-frame
  displacement features) → generated joints; morph frames are re-encoded as
  rigid joint transforms (weighted scale-free Kabsch, pure-Python 3x3 Jacobi
  SVD). The armature NODE (named `{civ}_{mesh}_root`) IS the skin's joint 0 —
  unlike hand-authored 0 A.D. models — so `FindSkeleton`'s parent-chain walk
  from `GetJoint(0)` finds it as the identifier root. The root joint stays
  frozen at identity (the engine composes world = root × local; fits are
  absolute per cluster). Frame 0 (the rest pose) emits exact identity so the
  base pose renders as authored. Static models (`frame_count <= 1`) stay
  unrigged.

- **Parallel conversion (process pool).** Rig synthesis (k-means + per-frame
  Kabsch) is the conversion's slowest phase (pure-Python, GIL-bound) — a
  6.8 MB 76-frame model takes ~23 s of k-means alone. The mesh phase and the
  animation phase run in a `ProcessPoolExecutor` (≤ 8 workers), one model /
  one animation DAE per worker, with deterministic (seeded) rig math and
  ordered result collection so output is byte-identical to the sequential
  path (measured: 244 meshes / 93 animations in ~42 s wall vs ~516 s serial
  on demo_A10; `validate --meshes` and archivebuild both accept it).
  Batches of ≤ 2 tasks fall back to sequential to avoid pool-spawn overhead.
- **Standalone tool** — `tools/morph_to_skeletal.py BASE.g3d [ANIM.g3d ...]`
  is the same rig pipeline as a one-shot CLI: skinned mesh DAE + one
  skeletal animation DAE per input (key = skill part of the filename),
  written under `art/meshes/{civ}/` and `art/animation/{civ}/` for direct
  drop-in. Same deterministic math: byte-identical to the pack converter
  for the same inputs (verified on treant: mesh + idle DAE match
  `--civ demo --rig-bones 6 --anim-speed 40 --loop`).

**Validation deltas (millenniumad / engine 0.28 archivebuild, 2026-08-06)** — the
importer contract overrides the literal layout above in these cases:
- **Skin controllers make the morph path work.** The importer REQUIRES a
  skin (`REQUIRE(skin != NULL)` in `PMDConvert.cpp`/`PSAConvert.cpp`) — morph
  data alone is rejected. The synthesized rig provides exactly that: one
  `<controller><skin>` per texture group with identity bind matrices and
  top-4 soft weights per vertex (`JOINT` indices are `bone + 1`: slot 0 is
  the root, which no vertex uses). Animation DAEs reuse the base model's
  geometry + weights (`use_base_weights=True` when the skill file IS the base
  model); keyframe times spread the frames over `100 / anim_speed` seconds
  (MegaGlest `anim-speed` semantics) with a closing rest keyframe for looping
  animations (stop/move). PSA resamples at fixed 30 fps; the engine maps
  names via the skeleton XML: standard bones are slots `root`/`bone_N`, and
  each DAE joint gets a `<target>`; mapped order == standard order keeps
  targetId == realTargetId. The validator now checks skin integrity
  (Name_array/bind/weights consistency, normalization) and animation DAEs
  (sampler sources, LINEAR-only interpolation, channel targets that resolve
  to JOINT/NODE nodes in the scene).
- **Mesh basename collisions dedupe with `_01`** (e.g. `stone.g3d` exists 3×
  with different content in the pack → `stone.dae`, `stone_01.dae`,
  `stone_02.dae`), and the `ConvertedMesh` registry maps each source g3d to
  its exact output DAEs so actors can reference the right file.
- **Same-content textures from different source dirs produce ONE output**
  (content-hash dedup): packs copy one art file into many unit dirs; the
  mod ships a single PNG per unique byte payload. Distinct-content textures
  that share a stem get `_N` suffixes.
- **All output textures are power-of-two** (upscaled in the converter):
  the archive builder otherwise warns and rescales every non-POT file.
- **MegaGlest cancel portraits are dropped**: the shared cancel icon is
  byte-identical for every unit and has no 0 A.D. analog (engine UI renders
  cancel); emitting `{unit}_cancel.png` would duplicate one texture N times.
- **Conversion report counts = converted output files** (DAE/PNG/OGG written
  to the mod, content-deduped), while `conversion_report.json` `assets` keeps
  the source-pack referenced counts.
- **Filenames are underscore-only** (`_N` suffixes, never `-N`): the 0 A.D.
  VFS/archive builder fails with `file_system.cpp(78): Error during IO` on
  hyphenated names.
- **Engine-smoke mods pin the installed engine**: `MG2OAD_TARGET_VERSION=0.28.0`
  (env, no CLI flag) writes `"dependencies": ["0ad=0.28.0"]` so the mod loads
  in the locally installed Pyrogenesis 0.28 and matches millenniumad's
  dependency line. The default target stays 0.29.x.

**Implementation**:
```python
class MeshConverter:
    def convert_g3d_to_dae(self, g3d_path: Path, output_dir: Path,
                           civ: str, stem: str | None = None) -> ConvertedMesh:
        """Convert G3D to one static base-pose COLLADA DAE per mesh."""
        # 1. Parse G3D per vendor/g3d/g3d_format.md
        # 2. Build COLLADA geometry (frame 0) — no armature, no animations
```

**Ruff Compliance**:
- Max line length: 100 characters
- Type hints on all functions
- Docstrings for all public methods

### 2. Texture Conversion

**Specifications**:
- **TGA/BMP/JPG → PNG**: use Pillow; target PNG with max lossless compression (`compress_level=9`). 0 A.D. loads PNG textures directly (the engine converts PNG→DDS only at archive-build time); TGA output is NOT required.
- Preserve alpha when present (`RGBA`); convert grayscale/color to `RGBA` for consistent material binds.
- Rename to lowercase, underscore-separated names per 0 A.D. naming conventions, e.g. `{civ}_{unit}_diffuse.png`.

**Implementation**:
```python
class TextureConverter:
    def convert_to_png(self, source: Path, output: Path) -> None:
        """Convert TGA/BMP/JPG to PNG with max compression."""
        img = Image.open(source)
        if img.mode not in ("RGBA", "LA"):
            img = img.convert("RGBA")
        img.save(output, "PNG", compress_level=9)
```

### 3. Audio Conversion (WAV → OGG)

**Tool**: the local `ffmpeg` binary, driven through `subprocess` (no pydub, see Requirements).

**Specifications**:
- OGG Vorbis, quality ~ -q:a 6 (≈192 kbps VBR); 0 A.D. uses OGG Vorbis for all sounds.
- Sample rate: preserve source, else normalize to 44100 Hz.
- Channels: preserve (mono/stereo).
- Playback grouping: 0 A.D. groups sounds via small XML files placed next to the sounds (volume/pitch/random selection) — see the public `audio/groups/` examples. Emit one group XML per sound set.

**Implementation**:
```python
class AudioConverter:
    def convert_wav_to_ogg(self, source: Path, output: Path) -> None:
        """Convert WAV to OGG with the local ffmpeg binary."""
        audio = AudioSegment.from_wav(source)
        audio.export(output, format="ogg", parameters=["-q:a", "6"])
```

### 4. Animation Extraction & Handling (synthesized rig)

G3D has no bones — only per-vertex morph frames — so the rig is **synthesized**
(`converters/rig.py`): k-means clusters vertices by rest position +
displacement features (deterministic k-means++, 60 iterations, early exit);
each cluster becomes a joint (`bone_1..bone_{K-1}`); the armature root
(`{civ}_{mesh}_root`) is joint 0. Morph frames are re-encoded as per-cluster
weighted rigid fits (scale-free Kabsch via pure-Python 3x3 Jacobi SVD).
Bone count `K = max(2, min(rig_bones, first-group vertex_count // 40))`
(`--rig-bones`, default 6). One `art/skeletons/{civ}.xml` per faction maps
PSA slots (`rig.bone_names`) to standard bones (`root`/`bone_N`) via
`<identifier><root>` + `<target>` (StdSkeletons.cpp contract). One
`art/animation/{civ}/{base}_{key}.dae` per skill animation: engine names
(stop→idle, move→walk+run, die→death, harvest→gather_*, build, repair;
attack→attack_ranged if `range > 4` or a projectile else attack_melee);
produce/upgrade/morph/be_built files are emitted but never wired. Actor
variants wire them with `speed="100"` and `event` = `attack-start-time`.
Fidelity is approximate (morph → skin); measured ≤ 0.07 units on the treant
idle fixture (683 verts / 19 frames / K=6).
### 5. civ.json Generation

**Input**: parsed faction metadata from the pack.

**Mapping**:
- Faction name → `Code` (lowercase, alphanumeric + underscore; must be unique across the mod).
- `units/*/{unit}.xml` → unit templates.
- `buildings/*/{building}.xml` → structure templates.
- Tech prerequisites → technology JSON files and `requirements` fields.

### 6. Simulation Templates

**Input**: parsed `unit.xml`, `building.xml`, and tech data.

**Process**:
- Extract unit stats (HP, armor, damage, speed, cost, train time).
- Extract building stats (HP, production, construction costs).
- Map skill types (attack, move, heal, build) to 0 A.D. template components (`Attack`, `UnitAI`, `ResourceGatherer`, `Builder`, `Cost`, `Health`, `Armour`).
- Emit `simulation/templates/units/{civ}/…` and `structures/{civ}/…`.

**Example Mapping**:
```
MegaGlest {faction}_{unit_type}_{variant} → simulation/templates/units/{civ}/{unit_name}.xml
```

---

## Implementation Phases

### Phase 1: Core Infrastructure (Non-Blocking)
- [x] Logging system (JSON-structured logging)
- [x] Configuration (CLI args, config files)
- [x] File path management
- [x] Error handling & validation

### Phase 2: MegaGlest Parser
- [x] Layout auto-detection (Layout A vs B)
- [x] XML parsing (faction, unit, building, tech) — tolerant, field-mapped
- [x] Asset inventory (scan all media files; resolve relative paths)
- [x] Civilization loader (extract civ data)

### Phase 3: Asset Converters
- [x] Texture converter (→ PNG with Pillow)
- [x] Audio converter (WAV → OGG via ffmpeg)
- [x] Mesh converter (G3D → DAE, native parser + COLLADA writer)

### Phase 4: 0 A.D. Output Generator — DONE (2026-08-06)
- [x] Mod directory structure builder (mod.json, art/, audio/, simulation/)
- [x] civ.json generator (simulation/data/civs/)
- [x] Actor XML generator
- [x] Unit/Building template generator (simulation/templates/)
- [x] Technology JSON generator (simulation/data/technologies/)

Implementation: `oad/civ_generator.py`, `oad/actor_generator.py`,
`oad/template_generator.py`, `oad/tech_generator.py` (+ shared
`oad/common.py`: `humanize_name`, `resource_cost` map, `material_for`,
`town_centre_candidate`), wired into `convert_pack` after per-faction media
conversion; `tests/test_generators.py` (10 tests). Formats follow
millenniumad 0.28.5: civ JSON key order exact (Code/Culture/Music/
CivBonuses/WallSets/StartEntities/AINames/SkirmishReplacements/
SelectableInGameSetup); actors `<actor version="1">` with base variant,
baseTex, version-aware material (`player_trans.xml` for 0.28 targets,
`basic_trans.xml` for 0.29); templates on plain `template_*` parent chains
(exist in both targets; 0.29 civ-chain modifiers skipped) with Cost/Health/
Resistance/Attack/UnitMotion/Vision/Identity/Builder/Promotion/Trainer/
Researcher/VisualActor; techs lowercase-keyed with `{civ}/{name}` cross-
references (millenniumad convention) and synthetic description/tooltip.

Mapping decisions (MegaGlest → 0 A.D.): model = first skill animation that
resolved during media conversion (actors reference `mesh_daes[0]`); HP/
damage/armor ÷ 10; train/build/research time ÷ 10; WalkSpeed = move-speed/30;
footprint/vision/range × 4 m/tile; `grace` dropped, gold→metal; armor → the
`Resistance` component (0.28 and 0.29 both; `Armour` is obsolete); attack
`pierce` → Pierce else Hack; Ranged when range > 4 tiles or projectile;
max-hp upgrade `multiply` = start-percentage × (100 + value) / 10000;
`affects` filtered to faction units; `FoundationActor` omitted (public mod
ships no `fndn_*` simulation templates).

Engine acceptance (2026-08-06, pinned 0.28.0): full demo_A10 convert →
archivebuild exit 0 (24,664,605 B) → `pyrogenesis -mod=public -mod=demo_a10`
exit 0 with 0 error(s) / 0 warning(s); all 49 generated game-data files
(20 actors, 20 templates, 8 techs, 1 civ JSON) parse cleanly.

### Phase 5: Skeleton & Animation — DONE (2026-08-06)

Morph frames re-encoded as a synthesized skeletal rig (see §4): texture-merged
skinned mesh DAEs, one animation DAE per skill animation, per-faction skeleton
XML, actor `<animations>` blocks with `speed`/`event`. Engine acceptance
(2026-08-06, pinned 0.28.0): full demo_A10 reconvert → archivebuild exit 0 →
load smoke exit 0 with 0 error(s) / 0 warning(s), now including PMD+PSA.
Rig math covered by unit tests (Kabsch rotation fit exact to 1e-9, SVD
reconstruction exact to 3e-15, `animation_duration` semantics, K > 5
`use_base_weights` expansion, treant rest-pose identity).

### Phase 6: Testing & Validation
- [x] Unit tests for converters
- [x] Integration tests (full MegaGlest pack conversion from committed fixtures)
- [x] Schema validation against the locally installed 0 A.D. `public` mod
- [ ] Smoke test: launch converted mod in local 0 A.D. (`pyrogenesis -mod=…`)
- [ ] Archive-build smoke test (`.pyromod` via `-archivebuild`) and install via the mod selection screen

---

## Code Quality Standards

### Ruff Compliance (All Files)
```bash
ruff check src/ --select E,W,F,I,N,UP,RUF
ruff format src/
```

**Rules**:
- E: PEP 8 errors
- W: Warnings
- F: PyFlakes
- I: Isort (import sorting)
- N: pep8-naming
- UP: pyupgrade
- RUF: Ruff-specific

### Logging

**Logger Setup** (src/core/logger.py):
```python
import logging
from pythonjsonlogger import jsonlogger

def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Configure JSON-structured logger."""
    logger = logging.getLogger(name)
    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level)
    return logger
```

**Usage**:
```python
logger = setup_logger(__name__)
logger.info("Converting unit", extra={"unit": "warrior", "faction": "egyptian"})
logger.error("Mesh conversion failed", extra={"path": str(g3d_path), "reason": error})
```

### Type Hints
All functions must have complete type hints:
```python
def convert_texture(source: Path, output: Path) -> None:
    """Convert texture to target format."""
    ...
```

### Documentation
- Module docstrings (3-line summary)
- Class docstrings (purpose + attributes)
- Public method docstrings (params, returns, raises)
- Inline comments for complex logic only

---

## CLI Interface

### Command Structure
```bash
# Basic conversion (input and output are local paths)
python -m megaglest_to_0ad convert \
  --megaglest-data /path/to/megaglest-pack \
  --output /path/to/0ad-mod \
  --factions egyptian greek roman

# With options
python -m megaglest_to_0ad convert \
  --megaglest-data /path/to/megaglest-pack \
  --output /path/to/0ad-mod \
  --factions all \
  --skip-media \
  --log-level debug

# Validate output against a local 0 A.D. install
python -m megaglest_to_0ad validate /path/to/0ad-mod --oad-data /path/to/0ad/public

# List available factions
python -m megaglest_to_0ad list-factions /path/to/megaglest-pack
```

### Click Implementation
```python
@click.group()
def cli():
    """MegaGlest to 0 A.D. Converter CLI."""
    pass

@cli.command()
@click.option("--megaglest-data", type=click.Path(exists=True), required=True)
@click.option("--output", type=click.Path(), required=True)
@click.option("--factions", multiple=True, default=["all"])
@click.option("--log-level", type=click.Choice(["debug", "info", "warning", "error"]))
def convert(megaglest_data: str, output: str, factions: tuple, log_level: str):
    """Convert MegaGlest faction(s) to 0 A.D. mod."""
    ...
```

---

## Testing Strategy

### Unit Tests
- Texture converter (fixed TGA/BMP fixtures)
- Audio converter (stub ffmpeg)
- XML parsers (fixed XML samples)
- G3D parser (small committed .g3d fixtures; parse + round-trip)
- Path normalization

### Integration Tests
- Full faction conversion (small test pack committed under `tests/fixtures/`)
- File structure validation against the target layout
- Output JSON/XML schema validation (compare key fields to the local `public` mod samples)

### Test Fixtures
```
tests/fixtures/
├── g3d/            # hand-built G3D v3 + v4 models and textures
└── packs/          # two pack layouts (A: techs/, B: flat factions/)
    ├── layout_a/
    ├── layout_b/
    └── broken/     # a deliberately invalid faction for the error paths
```
No fixture bytes come from a MegaGlest pack. `tools/make_test_fixtures.py`
generates every binary in the tree — models, PNG/TGA/BMP textures, WAV sound
and the OGG music stub — from code, and `tests/test_fixture_assets.py` fails if
a committed file stops matching the generator or if a binary appears that the
generator does not own. Regenerate with:

```bash
python tools/make_test_fixtures.py
```

All fixtures are committed to the repo — no network access in CI.

---

## Error Handling & Validation

### Validation Checklist
- [x] MegaGlest pack structure valid (layout detected, required XML files present)
- [x] All referenced assets exist (relative paths resolved)
- [x] G3D files parseable (magic + version checked)
- [x] Output 0 A.D. JSON/XML schema-compliant (validated against local `public` mod reference files)
- [x] No duplicate civilization codes
- [x] `mod.json` `name` matches the mod folder name
- [x] `mod.json` sits directly at the mod root (no wrapper directory around it — the #1 mod.io rejection cause)
- [x] `ignoreInCompatibilityChecks` absent (or only set for non-`simulation/` mods)
- [ ] Archive build succeeds and the mod shows green in the 0 A.D. mod selection screen

### Error Messages (Structured Logging)
```python
logger.error("Faction not found", extra={
    "faction": faction_name,
    "available": available_factions,
    "searched_paths": searched_paths
})
```

---

## Local References & Vendored Material

All reference material is local. Nothing here is fetched at build or run time.

1. **Written 0 A.D. reference material** — the wiki content on mod structure, installation, mod.json rules, archive building, the acceptance checklist, and file naming conventions is integrated directly into this plan (see "Output: 0 A.D. Mod Structure", "Installing & Launching", "File Naming Conventions", "Key Output Formats", "Error Handling & Validation"). No external wiki access is needed.
2. **0 A.D. `public` mod** — the ground truth for schemas. Path: inside the local 0 A.D. install (`binaries/data/mods/public/`). The converter's `validate` command reads civ/tech/template/actor examples from here. If no 0 A.D. install exists, copy the `public` data folder once next to the tool (`vendor/oad_public/`) and point config at it.
3. **Reference mod** (optional, one-time local clone) — Millennium A.D. is a good example of a standalone, hand-authored mod with its own civs, templates, actors, and audio; its file organization mirrors the public mod. Any locally available mod works; the point is to see how a mod *other than public* lays out its files.
4. **G3D format** — `vendor/g3d/g3d_format.md`, derived from the MegaGlest source reader (`source/shared_lib/sources/graphics/model.cpp`; GPLv3). The MegaGlest tree also contains useful reference tools: `source/g3d_viewer/` and `source/tools/glexemel/g2xml.c` (G3D→XML). Vendored copies live in-repo; no downloads.
5. **MegaGlest pack layouts** — documented in this plan (Layouts A and B). The parser auto-detects; extend from real packs the user supplies.
6. **COLLADA** — the DAE writer targets the widely supported 1.4.1 subset that 0 A.D. ships/reads (see DAE examples under `public/art/meshes/`); no external tooling required.
7. **Licensing of 0 A.D. content** — art is CC-BY-SA-3.0, code GPLv2. Do not copy 0 A.D. assets into converted mods; the mod's own content comes from the MegaGlest pack — check its license and include it in the output mod.

---

### Known Challenges
1. **G3D format is bespoke and versioned**: G3D v3 vs v4 headers differ (v4 adds per-vertex animation frames). Handle both, reject unknown versions with a clear error. Test against the fixtures and real pack files.
2. **0 A.D. version skew**: template `parent` syntax and some JSON fields changed across 0.25–0.29. Pin the target version (0.29.x), and validate output against the local install's own files; the `validate` command is the safety net.
3. **Synthetic-rig fidelity**: G3D has no bones, so the skeleton is an approximation; a 6-joint rig cannot reproduce arbitrary morphs exactly (measured ≤ 0.07 units on treant idle). Skill files with divergent rest poses (differing frame-0 geometry from the base model) are the worst case — weights come from base-model rest proximity. Increasing `--rig-bones` trades conversion time for accuracy. See §Mesh Conversion.
4. **Texture paths**: MegaGlest uses flat relative paths; 0 A.D. is hierarchical (`art/textures/units/{civ}/…`). Map during conversion and validate final paths.

### Extensibility
- Plugin system for custom converters (future)
- User-defined mapping files (JSON) for edge cases
- Dry-run mode (preview changes without writing)

---

## Success Criteria

✅ **Minimal**: Basic faction conversion (units, buildings, techs) produces a mod that loads in the local 0 A.D.
✅ **Complete**: Textures, audio, and synthesized skeletal animation (rigged meshes, per-skill animation DAEs, skeleton XML) working in 0 A.D.
✅ **Robust**: 100% ruff compliance, comprehensive logging, error recovery
✅ **Documented**: README, example conversion, API docs
✅ **Local**: full conversion pipeline runs with no network access after `pip install`

---

## Engineering Notes

Behaviour that is not obvious from reading the code, and why it ended up that
way.

### Template components and content gaps
- [x] Units were silent: the converter wrote `audio/groups/*.xml` but no template referenced them. Wire `<Sound><SoundGroups>` per unit — selection sounds to `select`, and skill sounds to the names the engine's animations use (`die` → `death`, `harvest` → `gather_*`, `attack` → `attack_melee`/`attack_ranged`, `move` → `walk`/`run`). 63 keys wired, 0 missing files.
- [x] Foundation actors skipped their earliest construction stage (`heavydamage` → `cons_02` on a 5-stage building, so `cons_01` was never shown; a placed foundation looked 20% built because `Foundation.js` starts hitpoints at 1 and the placement selection is `heavydamage`). Map `heavydamage` to stage 0.
- [x] Workers could not build anything: `_add_builder` was a guard-only stub, so no template had a `<Builder>` component and the engine never offered the construct command. Emit `Builder`/`Rate 1.0` plus `Entities` listing every faction structure. The pack's build-skill speed has no 0 A.D. rate equivalent, so build times stay in `Cost/BuildTime`.
- [x] Workers could not gather: no template had a `<ResourceGatherer>`, and the worker's fallback parent `template_unit_support` carries none, so every harvest skill was dead. Emit `ResourceGatherer` with public per-subtype rates (a missing rate means ungatherable) and 10-unit carries for units with a harvest skill, inserted in engine registration order.
- [x] Mobile summoners were misclassified as buildings: `_classify_building` treated any `produce` skill as a static producer, but MegaGlest summons are produce skills on field units. Add a mobility check (a `move` skill means unit) after the structure markers; mobile summoners keep their produce commands as a unit `Trainer`, which is entity-generic in the engine.
- [x] Projectile attacks were instant-hit: `_add_attack` emitted only `AttackName`/`Damage`/`MaxRange`/`RepeatTime`, so `Attack.js` applied damage at the attack event with no flight time. Emit a `Projectile` block (Speed 100 / Spread 0 / Gravity 50 / FriendlyFire false — the public arrow defaults) for every skill with `attack-projectile=true`.
- [x] Projectile meshes were converted but no actor referenced them, so flying arrows and rocks were invisible in game. Generate a projectile actor and add `VisualActor` to the `Projectile` block of every ranged and siege attack.
- [x] Flying units must use `unitmotionflying`.
- [x] `<Researcher><Technologies>` referenced display names rather than canonical upgrade ids, which broke every tech reference. Parse `<produced-upgrade>` and use that instead.
- [x] Custom MegaGlest resources were dropped silently by `resource_cost()`, whose `RESOURCE_MAP` covers gold/wood/stone/food only. `grace` maps to population — a positive cost becomes `Cost/Population` slots, a negative one a `Population/Bonus` cap. Every other custom resource now warns per unit/tech/civ instead of vanishing.
- [x] Material naming moved to the modern `_norm_spec` variants; a missing `normTex` falls back to `default_norm.png` and a missing `specTex` to `null_black.dds`.
- [x] Numbers are zero-padded (`_01`, not `_1`) to match the engine's variant convention.

### Performance
`build_rig` k-means in `converters/rig.py` was the bottleneck. Feature vectors are rest position plus per-frame displacements, so dims = 3 + 3×(frames−1); a 2465-vertex, 32-bone, 76-frame model took ~36 s. It now runs on numpy arrays — k-means++, batched Kabsch via 3×3 SVD, vectorised soft weights and nearest-neighbour, with a fresh `default_rng(0)` per `_kmeans` so output stays deterministic. That model builds in 1.3 s and a full pack run in 1–3 min, byte-identical across runs.

`validate --meshes` was unusably slow from quadratic skin-weight parsing. Parsing the weights once made it ~70× faster: 225/225 DAEs importable in seconds.

What remains is the 145 animation DAE writes, each re-reading the base and animation models and re-embedding full geometry, using brute-force nearest-neighbour when the base and animation models differ. Tracked in the backlog.

## Backlog

Open work, tracked as GitHub issues. Story points are the usual Fibonacci guess.

| Issue | Labels | Points |
|---|---|---|
| Convert MegaGlest maps into 0 A.D. maps | `enhancement` | 13 |
| Ship the civ loading screen in the converted mod | `bug` | 2 |
| Cache parsed models and replace brute-force NN with a KD-tree | `performance` | 8 |
| Spike: are compiled hot loops still worth it after the above | `performance`, `spike` | 5 |
| Release gate: a converted mod loads green in 0 A.D. | `verification` | 3 |
| Release gate: the `.pyromod` archive installs from the mod screen | `verification` | 3 |

The two release gates are the only work here that cannot run in CI: both need a
local 0 A.D. install. The remaining unchecked boxes in **Phase 6** and the
**Validation Checklist** above are exactly those two.
