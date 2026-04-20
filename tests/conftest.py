from __future__ import annotations

import shutil
import subprocess
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


@pytest.fixture
def fake_project_git(tmp_path: Path, fake_project_path: Path) -> Path:
    """Copy the fake-project fixture into a tmp directory and make it a clean git repo.

    Returns the path to the committed working tree so provenance.git_state() can
    produce a concrete SHA and clean-tree verdict.
    """
    dest = tmp_path / "FakeProjectRepo"
    shutil.copytree(fake_project_path, dest)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "test@example.com")
    _git(dest, "config", "user.name", "benchstone tests")
    _git(dest, "config", "commit.gpgsign", "false")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "initial fixture commit")
    return dest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
