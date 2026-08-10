"""Shared pytest fixtures: pack paths."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "packs"


@pytest.fixture
def layout_b_pack() -> Path:
    return FIXTURES / "layout_b"


@pytest.fixture
def layout_a_pack() -> Path:
    return FIXTURES / "layout_a"


@pytest.fixture
def broken_pack() -> Path:
    return FIXTURES / "broken"
