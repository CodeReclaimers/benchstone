from __future__ import annotations

from pathlib import Path

import pytest

from benchstone.provenance import ProvenanceError, git_state


def test_clean_repo_reports_clean(fake_project_git: Path) -> None:
    state = git_state(fake_project_git)
    assert len(state.sha) == 40
    assert state.dirty is False
    assert state.diff == ""


def test_modified_tracked_file_is_dirty_with_diff(fake_project_git: Path) -> None:
    (fake_project_git / "bench" / "manifest.toml").write_text(
        (fake_project_git / "bench" / "manifest.toml").read_text() + "\n# drift\n"
    )
    state = git_state(fake_project_git)
    assert state.dirty is True
    assert "# drift" in state.diff


def test_untracked_file_counts_as_dirty(fake_project_git: Path) -> None:
    (fake_project_git / "scratch.txt").write_text("untracked")
    state = git_state(fake_project_git)
    assert state.dirty is True
    # untracked files don't show up in `git diff HEAD`, only in status
    assert state.diff == ""


def test_non_repo_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ProvenanceError):
        git_state(tmp_path)


def test_nonexistent_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ProvenanceError, match="not a directory"):
        git_state(tmp_path / "does-not-exist")
