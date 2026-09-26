"""The ``MG2OAD_*`` environment layer.

Settings used to come from pydantic-settings, which read ``.env`` and coerced
types for free. That is now a hand-rolled parser, so it gets pinned here: a
setting that silently keeps its default because a coercion branch is wrong is
the kind of bug nobody notices until a pack converts wrongly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from megaglest_to_0ad.core.config import Settings

_PREFIX = "MG2OAD_"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No inherited MG2OAD_* vars, and a cwd with no .env to find."""
    for key in list(os.environ):
        if key.startswith(_PREFIX):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def test_defaults_apply_with_no_environment() -> None:
    settings = Settings.from_env()
    assert settings.target_version == "0.28.0"
    assert settings.mod_version == "1.0.0"
    assert settings.skip_media is False
    assert settings.rig_bones == 32


def test_environment_overrides_and_coerces_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_PREFIX + "TARGET_VERSION", "0.29.0")
    monkeypatch.setenv(_PREFIX + "RIG_BONES", "8")
    monkeypatch.setenv(_PREFIX + "SKIP_MEDIA", "true")

    settings = Settings.from_env()
    assert settings.target_version == "0.29.0"
    assert settings.rig_bones == 8, "must be an int, not the string '8'"
    assert settings.skip_media is True, "must be a bool, not the string 'true'"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_spellings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(_PREFIX + "SKIP_MEDIA", raw)
    assert Settings.from_env().skip_media is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_falsy_spellings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(_PREFIX + "SKIP_MEDIA", raw)
    assert Settings.from_env().skip_media is False


def test_env_file_is_read_and_real_env_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The autouse fixture put us in an empty tmp_path.
    (Path.cwd() / ".env").write_text(
        "# a comment\n"
        "\n"
        f"{_PREFIX}RIG_BONES=12\n"
        f"{_PREFIX}TARGET_VERSION='0.30.0'\n"
        "UNPREFIXED=ignored\n",
        encoding="utf-8",
    )
    settings = Settings.from_env()
    assert settings.rig_bones == 12
    assert settings.target_version == "0.30.0", "quotes should be stripped"

    monkeypatch.setenv(_PREFIX + "RIG_BONES", "5")
    assert Settings.from_env().rig_bones == 5, "the real environment must win"


def test_unknown_keys_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_PREFIX + "NOT_A_SETTING", "x")
    assert Settings.from_env() == Settings()


def test_bad_int_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_PREFIX + "RIG_BONES", "many")
    with pytest.raises(ValueError):
        Settings.from_env()
