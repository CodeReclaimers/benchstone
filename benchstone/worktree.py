"""Temporary git-worktree context manager for time-travel baseline establishment.

Lets `bench baseline establish --at-sha <sha>` run a benchmark at a historical
SHA without disturbing the user's working tree. The worktree is removed on
exit even if the benchmark fails; the parent tempdir is rmtree'd regardless.
"""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path


class WorktreeError(Exception):
    """Raised when creating or cleaning up a git worktree fails."""


@contextlib.contextmanager
def with_git_worktree(project_path: Path, sha: str) -> Iterator[Path]:
    """Create a detached worktree of ``project_path`` at ``sha`` and yield its path.

    On exit the worktree is removed via ``git worktree remove --force`` and the
    parent tempdir is rmtree'd. Errors during cleanup are swallowed (the user
    will have already received the primary error) but a warning line is emitted
    to stderr so stale worktrees are detectable.
    """
    if not project_path.is_dir():
        raise WorktreeError(f"not a directory: {project_path}")

    # Pre-flight: make sure the SHA exists in the repo so the failure mode is
    # a clear error rather than a half-created worktree.
    resolved = _run_git(
        project_path, ["rev-parse", "--verify", f"{sha}^{{commit}}"],
        error=f"SHA {sha!r} not found in {project_path}",
    ).strip()

    parent = Path(tempfile.mkdtemp(prefix="benchstone-worktree-"))
    wt_path = parent / "tree"
    try:
        _run_git(
            project_path,
            ["worktree", "add", "--detach", str(wt_path), resolved],
            error=f"git worktree add failed at {resolved}",
        )
        yield wt_path
    finally:
        # Best-effort cleanup; swallow errors so the user sees the primary one,
        # but surface a warning so stale worktrees are detectable.
        try:
            _run_git(
                project_path,
                ["worktree", "remove", "--force", str(wt_path)],
                error="worktree remove failed",
            )
        except WorktreeError as exc:
            print(
                f"benchstone: warning: failed to remove worktree at {wt_path}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        shutil.rmtree(parent, ignore_errors=True)


def _run_git(cwd: Path, args: list[str], error: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise WorktreeError(f"{error}: {exc.stderr.strip() or exc}") from exc
    return out.stdout
