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