"""Filesystem helpers: macro expansion and pack-path resolution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

_MACRO_RE = re.compile(r"\$([A-Z0-9_]+)")


def expand_macros(ref: str, macros: Mapping[str, Path]) -> str:
    """Replace ``$NAME`` tokens (e.g. ``$COMMONDATAPATH``) with paths.

    Unknown macros are left untouched so they surface in validation.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in macros:
            return str(macros[name])
        return match.group(0)

    return _MACRO_RE.sub(_sub, ref)


def resolve_pack_path(
    base: Path,
    ref: str,
    macros: Mapping[str, Path] | None = None,
) -> Path:
    """Resolve a MegaGlest asset reference to an absolute path.

    ``ref`` is relative to ``base`` (usually the unit/faction directory) and
    may contain macros such as ``$COMMONDATAPATH``. A leading ``/`` is a
    MegaGlest quirk found in some packs (e.g. ``/sounds/x.wav``): the slash is
    stripped and the reference resolves relative to ``base``, where the pack
    ships the file.
    """
    if ref.startswith("/"):
        ref = ref.lstrip("/")
    expanded = expand_macros(ref, macros or {})
    candidate = Path(expanded)
    if candidate.is_absolute():
        return candidate
    return (base / candidate).resolve()
