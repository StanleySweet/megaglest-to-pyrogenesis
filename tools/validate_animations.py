#!/usr/bin/env python3
"""Lint actor animation wiring for the skinned-prop sync contract.

The 0 A.D. engine syncs the frame timing of a unit's separately-skinned
parts through the actor <animation id=...> attribute (``CObjectEntry::Anim``
-> ``CSkeletonAnim::m_ID``) combined with ``CUnitAnimation::PickAnimationID``:

- Every model part (the root actor and each of its <prop> actors that
  carries animations) exposes its animations keyed by ``name`` (idle, walk,
  run, attack_*, death, ...).
- ``PickAnimationID`` picks an ``id`` from the root's chosen animation, then
  every other part plays the animation *whose id matches* that chosen id
  (``UnitAnimation.cpp``: ``GetRandomAnimation(m_State, m_AnimationID)``).
  If a part has no matching id, ``GetAnimations`` falls back to the idle
  animations -- so a missing/mismatched id makes props desync and play the
  wrong clip.

This linter checks that contract:
 1. every <animation> in the mod carries a non-empty ``id``;
 2. for each unit actor, every animation ``name`` resolves to the SAME id
    across the root and all of its (animated) prop actors, so the parts
    select the same clip and stay in phase;
 3. every referenced animation DAE and prop actor exists in the mod.

A prop may legitimately lack a ``name`` its root has (e.g. a whole-mesh
death crumble on a split building): the engine falls back to that prop's
idle clips, so this is reported as a WARNING, not an error. Missing or
mismatched ``id``, missing animation files and dead prop refs are errors.

Usage::

    python tools/validate_animations.py --mod-dir /tmp/demo_a12/demo_a12
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from lxml import etree

_ANIMATION_DIR = "art/animation"
_ACTOR_DIR = "art/actors"


def _iter_elements(parent: etree._Element):
    for child in parent:
        if isinstance(child.tag, str):
            yield child


def _actors(mod_dir: Path) -> list[Path]:
    actors_dir = mod_dir / _ACTOR_DIR
    if not actors_dir.exists():
        return []
    return sorted(p for p in actors_dir.rglob("*.xml"))


def _animations(variant: etree._Element) -> list[dict[str, str]]:
    """Animation entries (file/name/id/...) within a variant."""
    out: list[dict[str, str]] = []
    for child in _iter_elements(variant):
        if child.tag != "animations":
            continue
        for anim in _iter_elements(child):
            if anim.tag != "animation":
                continue
            out.append(
                {
                    "file": anim.get("file", ""),
                    "name": anim.get("name", ""),
                    "id": anim.get("id", ""),
                }
            )
    return out


def _props(variant: etree._Element) -> list[str]:
    out: list[str] = []
    for child in _iter_elements(variant):
        if child.tag != "props":
            continue
        for prop in _iter_elements(child):
            if prop.tag == "prop" and prop.get("actor"):
                out.append(prop.get("actor"))
    return out


class LintResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def add_errors(self, errors: list[str]) -> None:
        self.errors.extend(errors)


def lint_mod(mod_dir: Path) -> LintResult:
    result = LintResult()
    actors = _actors(mod_dir)
    if not actors:
        result.add_errors([f"{mod_dir / _ACTOR_DIR}: no actor XMLs found to lint"])
        return result

    # index of actors by relative path (without the modifiers for props)
    by_rel: dict[str, Path] = {}
    for actor in actors:
        rel = actor.relative_to(mod_dir / _ACTOR_DIR).as_posix()
        by_rel[rel] = actor

    anim_dir = mod_dir / _ANIMATION_DIR

    # First pass: every animation entry must carry a non-empty id.
    for actor in actors:
        root = etree.parse(actor).getroot()
        for variant in root.iter("variant"):
            for anim in _animations(variant):
                if not anim["id"]:
                    result.errors.append(f"{actor}: animation '{anim['name']}' has no id")
                if not anim["file"]:
                    result.errors.append(f"{actor}: animation '{anim['name']}' has no file")
                elif not (anim_dir / anim["file"]).exists():
                    result.errors.append(f"{actor}: animation file '{anim['file']}' does not exist")
                if not anim["name"]:
                    result.errors.append(f"{actor}: animation entry has no name")

    # Second pass: sync check. For unit actors (those with at least one
    # animation in any variant), ensure each animation name maps to one id
    # and that id is shared with every animated prop.
    for actor in actors:
        root = etree.parse(actor).getroot()
        main_anims = _collect(actor)
        if not main_anims:
            continue

        # name -> id for the root
        root_name_to_ids = {name: ids for name, ids in main_anims.items()}
        for name in sorted(root_name_to_ids):
            ids = root_name_to_ids[name]
            if len(ids) > 1:
                result.errors.append(
                    f"{actor}: animation '{name}' has multiple distinct ids {sorted(ids)}"
                )

        # Follow props
        visited: set[Path] = set()
        stack: list[Path] = [actor]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            croot = etree.parse(current).getroot()
            prop_refs: set[Path] = set()
            for variant in croot.iter("variant"):
                for ref in _props(variant):
                    ref_path = _resolve_actor(mod_dir, by_rel, ref)
                    if ref_path is None:
                        result.errors.append(f"{current}: prop actor '{ref}' does not exist")
                        continue
                    prop_refs.add(ref_path)
                    stack.append(ref_path)
            for prop in sorted(prop_refs, key=lambda p: p.as_posix()):
                prop_anims = _collect(prop)
                if not prop_anims:
                    continue
                for name in sorted(main_anims):
                    ids = prop_anims.get(name)
                    if ids is None:
                        result.warnings.append(
                            f"{actor}: prop {prop.relative_to(mod_dir)} lacks animation "
                            f"'{name}' (falls back to idle in the engine)"
                        )
                        continue
                    root_id = next(iter(main_anims[name]))
                    if root_id not in ids:
                        result.errors.append(
                            f"{actor}: animation '{name}' id mismatch "
                            f"(root {root_id}, prop {sorted(ids)})"
                        )
    return result


def _collect(actor: Path) -> dict[str, set[str]]:
    """name -> set of ids (all variants merged)."""
    root = etree.parse(actor).getroot()
    out: dict[str, set[str]] = defaultdict(set)
    for variant in root.iter("variant"):
        for anim in _animations(variant):
            if anim["name"]:
                out[anim["name"]].add(anim["id"])
    return dict(out)


def _resolve_actor(mod_dir: Path, by_rel: dict[str, Path], ref: str) -> Path | None:
    """Resolve a prop actor reference (relative to art/actors/)."""
    ref = ref.strip("/")
    if ref in by_rel:
        return by_rel[ref]
    # try without the civ/actors prefix if it was absolute under actors
    candidates = [ref]
    stripped = ref.split("/", 1)
    if len(stripped) == 2:
        candidates.append(stripped[1])
    for cand in candidates:
        if cand in by_rel:
            return by_rel[cand]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mod-dir",
        required=True,
        type=Path,
        help="Path to the generated mod folder (the one containing art/).",
    )
    args = parser.parse_args()

    if not args.mod_dir.exists():
        print(f"error: {args.mod_dir} does not exist", file=sys.stderr)
        return 2

    result = lint_mod(args.mod_dir)
    for error in result.errors:
        print(f"ERROR: {error}")
    for warning in result.warnings:
        print(f"WARN:  {warning}")
    print(f"{len(result.errors)} error(s), {len(result.warnings)} warning(s)")
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
