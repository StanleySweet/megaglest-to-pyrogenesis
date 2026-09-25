"""Runtime configuration for the converter."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Converter-wide settings, overridable via env vars (``MG2OAD_*``).

    CLI flags take precedence; this class supplies defaults and the
    environment/config-file layer.
    """

    model_config = SettingsConfigDict(
        env_prefix="MG2OAD_",
        env_file=".env",
        extra="ignore",
    )

    log_level: str = "INFO"
    # The installed engine's public mod is 0.28.0; a mod whose dependency
    # says 0.29.0 fails to load ("wrong mod version"). Override via
    # MG2OAD_TARGET_VERSION when targeting a newer engine.
    target_version: str = "0.28.0"
    mod_version: str = "1.0.0"
    oad_public_dir: Path | None = None
    skip_media: bool = False
    rig_bones: int = 32
