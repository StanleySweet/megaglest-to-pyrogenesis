"""Runtime configuration for the converter."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, get_type_hints

_ENV_PREFIX = "MG2OAD_"


def _read_env_file() -> dict[str, str]:
    """Parse a ``.env`` file when one is present.

    The environment layer reads the working directory, so a converter run
    from a build checkout can be configured without exporting variables in the
    calling shell.
    """
    path = Path(".env")
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _coerce(raw: str, kind: Any) -> Any:
    """Convert an environment string to a field's declared type."""
    if kind is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if kind is int:
        return int(raw)
    return raw


@dataclass(frozen=True)
class Settings:
    """Converter-wide settings, overridable via env vars (``MG2OAD_*``).

    CLI flags take precedence; this class supplies defaults and the
    environment/config-file layer.
    """

    # The installed engine's public mod is 0.28.0; a mod whose dependency
    # says 0.29.0 fails to load ("wrong mod version"). Override via
    # MG2OAD_TARGET_VERSION when targeting a newer engine.
    target_version: str = "0.28.0"
    mod_version: str = "1.0.0"
    skip_media: bool = False
    rig_bones: int = 32

    @classmethod
    def from_env(cls) -> Settings:
        """Defaults, overridden by ``.env`` and then the real environment.

        Field types drive the coercion, so a new field is configurable
        without touching this method.
        """
        found = {**_read_env_file(), **os.environ}
        kinds = get_type_hints(cls)
        values: dict[str, Any] = {}
        for field in fields(cls):
            raw = found.get(_ENV_PREFIX + field.name.upper())
            if raw is not None:
                values[field.name] = _coerce(raw, kinds[field.name])
        return cls(**values)
