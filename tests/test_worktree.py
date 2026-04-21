from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from benchstone.worktree import WorktreeError, with_git_worktree


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def test_worktree_of_current_head(fake_project_git: Path) -> None:
    head = _git(fake_project_git, "rev-parse", "HEAD")
    with with_git_worktree(fake_project_git, "HEAD") as wt:
        assert wt.is_dir()
        # Manifest is present in the worktree, same SHA as the source.
        assert (wt / "bench" / "manifest.toml").is_file()
        assert _git(wt, "rev-parse", "HEAD") == head
    # Worktree path removed on exit.
    assert not wt.exists()


def test_worktree_of_past_sha(fake_project_git: Path) -> None:
    # Make a second commit so HEAD^ is a real past SHA.
    (fake_project_git / "NEW.txt").write_text("new")
    _git(fake_project_git, "add", "NEW.txt")
    _git(fake_project_git, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "second")

    past_sha = _git(fake_project_git, "rev-parse", "HEAD^")
    with with_git_worktree(fake_project_git, past_sha) as wt:
        # NEW.txt shouldn't exist in the worktree at the older SHA.
        assert not (wt / "NEW.txt").exists()
        assert _git(wt, "rev-parse", "HEAD") == past_sha


def test_worktree_unknown_sha_raises(fake_project_git: Path) -> None:
    with pytest.raises(WorktreeError, match="not found"):
        with with_git_worktree(fake_project_git, "deadbeefdeadbeef0000"):
            pass


def test_worktree_non_directory_raises(tmp_path: Path) -> None:
    f = tmp_path / "not-a-dir.txt"
    f.write_text("x")
    with pytest.raises(WorktreeError, match="not a directory"):
        with with_git_worktree(f, "HEAD"):
            pass


def test_worktree_cleans_up_after_exception(fake_project_git: Path) -> None:
    captured: list[Path] = []
    with pytest.raises(RuntimeError, match="boom"):
        with with_git_worktree(fake_project_git, "HEAD") as wt:
            captured.append(wt)
            raise RuntimeError("boom")
    assert captured and not captured[0].exists()
    # git should not have a dangling worktree registration.
    out = _git(fake_project_git, "worktree", "list", "--porcelain")
    assert str(captured[0]) not in out
