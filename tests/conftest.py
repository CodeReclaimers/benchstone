from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_project_path() -> Path:
    return FIXTURES / "fake_project"


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "benchstone-home"
    monkeypatch.setenv("BENCHSTONE_HOME", str(home))
    return home
