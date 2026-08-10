- [x] Add to agents.md to always build in output, that includes the pyromod that will be installed
- Some of the actors have no textures. (ministrel sanctuary for ex)
- All actors should use a placeholder norm and spec texture.
- Output folder seems outdated, and should be ignored removed if useless
- Use pycollada if we don't use it and other libraries to reduce boilerplate.
- Some of the anims are distorted, To check for consistency one should compare vertex positions frame by frame to make sure.
- More files/folders should be ignored
- The original input/megaglest-data/ has a different layout that does not work

- Rename materials:
    basic_spec → no_trans_spec
    blend_spec → basic_trans_spec
    objectcolor_spec → objectcolor_specmap
    playercolor_spec → player_trans_spec
- Upgrade materials to _norm_spec variants.
- Add missing normTex → default_norm.png.
- Add missing specTex → null_black.dds.
- Handle parallax materials based on normTex PNG alpha.
- Warn on unknown/missing/incompatible assets needing manual fixes.
- Save modified XML files and report fixes.
- Save changed XML files.
- Log number of fixed files.

- .gitignore is dead
- simulation/templates footprint are not computed correctly they are quite big 
- numbers should be _D2 and not _1 etc this is not a new rule, but if it is should be enforced.
- techs are missing.