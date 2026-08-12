- [x] Add to agents.md to always build in output, that includes the pyromod that will be installed
- [x] Some of the actors have no textures. (ministrel sanctuary for ex)
- [x] All actors should use a placeholder norm and spec texture.
- [x] Output folder seems outdated, and should be ignored removed if useless
- [x] Use pycollada if we don't use it and other libraries to reduce boilerplate.
- [x] Some of the anims are distorted, To check for consistency one should compare vertex positions frame by frame to make sure.
- [x] More files/folders should be ignored
- [x] The original input/megaglest-data/ has a different layout that does not work

- [x] Rename materials:
    basic_spec → no_trans_spec
    blend_spec → basic_trans_spec
    objectcolor_spec → objectcolor_specmap
    playercolor_spec → player_trans_spec
- [x] Upgrade materials to _norm_spec variants.
- [x] Add missing normTex → default_norm.png.
- [x] Add missing specTex → null_black.dds.
- [x] Handle parallax materials based on normTex PNG alpha.
- [x] Warn on unknown/missing/incompatible assets needing manual fixes.
- [x] Save modified XML files and report fixes.
- [x] Save changed XML files.
- [x] Log number of fixed files.

- [x] .gitignore is dead
- [x] simulation/templates footprint are not computed correctly they are quite big
- [x] numbers should be _01 and not _1 etc this is not a new rule, but if it is should be enforced.
- [x] techs are missing.
- [x] validate --meshes was unusably slow (quadratic skin-weight parsing); fixed, 225/225 DAEs importable in seconds

- [x] Researcher <Technologies> referenced display names, not canonical upgrade ids (forge noldor_armour -> noldor_armour_crafting, wood_hall train_and_equip_dryads -> dryad_weaponry, lore_house gather_wisdom -> wisdom); parse <produced-upgrade> and use it, 0 broken tech refs

- [x] Units were silent: the converter wrote SoundGroup XMLs (audio/groups/*.xml) but no template referenced them. Wire <Sound><SoundGroups> per unit: selection-sounds -> select, skill sounds -> the engine animation names the actor wires (die -> death, harvest -> gather_*, attack -> attack_melee/ranged, move -> walk/run); 63 keys wired, 0 missing files

- [x] Foundation actors skipped the earliest construction stage (heavydamage -> cons_02 for 5-stage academy, cons_01 never shown; a placed foundation looked 20% built because Foundation.js starts hitpoints at 1 and the placement selection is heavydamage). Map heavydamage to stage 0.

- [x] Workers could not build anything: _add_builder was a guard-only stub, so no template had a <Builder> component and the engine never offered the construct command. Emit Builder/Rate 1.0 + Entities listing every faction structure (the pack's build-skill speed has no 0 A.D. rate equivalent; build times stay in Cost/BuildTime).

- [x] Workers could not gather: no template had a <ResourceGatherer> and the worker's fallback parent template_unit_support carries none, so every harvest skill was dead. Emit ResourceGatherer with public per-subtype rates (missing rate = ungatherable) + 10-unit carries for units with a harvest skill; inserted in engine registration order.

- [x] Minstrel (and magol_hedir) misclassified as buildings: _classify_building treated any produce skill as a static producer, but MG summons are produce skills on field units (minstrel summons dryads/ents; both walk). Add a mobility check (move skill -> unit) after the structure markers; mobile summoners keep their produce commands as a unit Trainer (engine Trainer is entity-generic). minstrel: units/ + wood_hall trains it + still summons; magol_hedir: trained at academy as a size-1 melee unit per the pack.

- [x] Projectile attacks were instant-hit: _add_attack emitted only AttackName/Damage/MaxRange/RepeatTime, so Attack.js applied damage at the attack event with no flight time. Emit a Projectile block (Speed 100 / Spread 0 / Gravity 50 / FriendlyFire false - public arrow defaults) for every skill with MG attack-projectile=true; 8 units (archers, dryad, minstrel, reaver, prince, forest_guardian, ent siege rock) now fire flying projectiles, melee-only units stay instant.