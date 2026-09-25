"""Every binary fixture is generated from code and reproducible byte for byte.

tools/make_test_fixtures.py owns tests/fixtures. These tests fail if a committed
fixture stops matching the generator, or if a binary appears under tests/fixtures
that the generator does not produce.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _generator() -> ModuleType:
    path = ROOT / "tools" / "make_test_fixtures.py"
    spec = importlib.util.spec_from_file_location("make_test_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_fixtures_match_generator(tmp_path: Path) -> None:
    module = _generator()
    module.write_all(tmp_path)
    for relative, payload in module.build().items():
        committed = FIXTURES / relative
        assert committed.is_file(), f"missing fixture: {relative}"
        assert committed.read_bytes() == payload, f"stale fixture: {relative}"


def test_no_binary_fixture_outside_the_generator() -> None:
    module = _generator()
    expected = {(FIXTURES / relative).resolve() for relative in module.build()}
    actual = {
        path.resolve()
        for path in FIXTURES.rglob("*")
        if path.is_file() and path.suffix != ".xml" and "__pycache__" not in path.parts
    }
    assert actual == expected
