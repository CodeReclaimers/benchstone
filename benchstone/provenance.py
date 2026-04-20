from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProvenanceError(Exception):
    """Raised when git state cannot be determined."""


@dataclass(frozen=True)
class GitState:
    sha: str
    dirty: bool
    diff: str  # content of `git diff HEAD` when dirty; empty string otherwise


def git_state(path: str | Path) -> GitState:
    """Return the git SHA, dirty flag, and diff for the repository containing `path`.

    Untracked files count as dirty: the working tree is not reproducible from the
    recorded SHA when they are present. `git diff HEAD` only captures changes to
    tracked files, so an untracked-only dirty state yields ``dirty=True`` with
    ``diff=""`` — the presence of the flag is still enough to flag the run.
    """
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise ProvenanceError(f"not a directory: {path}")
    sha = _run(["git", "-C", str(path), "rev-parse", "HEAD"])
    status = _run(["git", "-C", str(path), "status", "--porcelain=v1"])
    dirty = status.strip() != ""
    diff = _run(["git", "-C", str(path), "diff", "HEAD"], strip=False) if dirty else ""
    return GitState(sha=sha, dirty=dirty, diff=diff)


def _run(cmd: list[str], strip: bool = True) -> str:
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise ProvenanceError(
            f"command failed: {' '.join(cmd)}\n{exc.stderr.strip()}"
        ) from exc
    except FileNotFoundError as exc:
        raise ProvenanceError("git executable not found on PATH") from exc
    return out.stdout.strip() if strip else out.stdout
