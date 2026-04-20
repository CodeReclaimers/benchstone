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
    # Give the scheduler enough headroom that every manifest benchmark admits
    # by default; individual tests can override either variable.
    monkeypatch.setenv("BENCHSTONE_MAX_THREADS", "64")
    monkeypatch.setenv("BENCHSTONE_GPU_COUNT", "0")
    return home


@pytest.fixture
def shell_project_path() -> Path:
    return FIXTURES / "shell_project"


@pytest.fixture
def fake_project_git(tmp_path: Path, fake_project_path: Path) -> Path:
    """Copy the fake-project fixture into a tmp directory and make it a clean git repo.

    Returns the path to the committed working tree so provenance.git_state() can
    produce a concrete SHA and clean-tree verdict.
    """
    return _materialize_git_project(tmp_path / "FakeProjectRepo", fake_project_path)


@pytest.fixture
def shell_project_git(tmp_path: Path, shell_project_path: Path) -> Path:
    """Bash-based second-project fixture for Phase 4 contract stress."""
    return _materialize_git_project(tmp_path / "ShellProjectRepo", shell_project_path)


def _materialize_git_project(dest: Path, src: Path) -> Path:
    shutil.copytree(src, dest)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "test@example.com")
    _git(dest, "config", "user.name", "benchstone tests")
    _git(dest, "config", "commit.gpgsign", "false")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "initial fixture commit")
    return dest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
