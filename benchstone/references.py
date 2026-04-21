from __future__ import annotations

import dataclasses
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths
from ._timefmt import utc_now
from .store import Run, Store


class ReferenceError(Exception):
    """Raised when a reference operation fails (missing artifact, already exists, etc.)."""


@dataclass(frozen=True)
class Reference:
    project: str
    benchmark: str
    frozen_at: str
    frozen_git_sha: str
    content_hash: str
    content_path: str
    from_run_id: int | None
    notes: str | None


def freeze(
    project: str,
    benchmark: str,
    run: Run,
    notes: str | None = None,
) -> Reference:
    """Create the initial frozen reference for (project, benchmark).

    Raises ReferenceError if a reference already exists — use ``replace`` with
    an explicit reason instead, per the immutability principle in guide §1.
    """
    if exists(project, benchmark):
        raise ReferenceError(
            f"reference already exists for {project}/{benchmark}; "
            f"use replace with --reason to override"
        )
    _require_artifact(run)
    ref = _build(project, benchmark, run, notes)
    _write(ref)
    _append_history(project, benchmark, {
        "event": "frozen",
        "at": ref.frozen_at,
        "new": asdict(ref),
    })
    return ref


def replace(
    project: str,
    benchmark: str,
    run: Run,
    reason: str,
    notes: str | None = None,
) -> Reference:
    """Overwrite an existing reference, appending a replacement event to history.

    The reason string is required; an empty or whitespace-only reason is rejected
    so the history log stays meaningful.
    """
    if not reason or not reason.strip():
        raise ReferenceError("replace requires a non-empty --reason")
    prior = get(project, benchmark)
    if prior is None:
        raise ReferenceError(
            f"no existing reference for {project}/{benchmark}; "
            f"use freeze-reference instead"
        )
    _require_artifact(run)
    new = _build(project, benchmark, run, notes)
    _write(new)
    _append_history(project, benchmark, {
        "event": "replaced",
        "at": new.frozen_at,
        "reason": reason,
        "prior": asdict(prior),
        "new": asdict(new),
    })
    return new


def get(project: str, benchmark: str) -> Reference | None:
    path = _ref_file(project, benchmark)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return Reference(**data)


def exists(project: str, benchmark: str) -> bool:
    return _ref_file(project, benchmark).exists()


def history(project: str, benchmark: str) -> list[dict]:
    path = _history_file(project, benchmark)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- internals ---------------------------------------------------------------


def _require_artifact(run: Run) -> None:
    if run.artifact_hash is None or run.artifact_path is None:
        raise ReferenceError(
            f"run id={run.id} has no artifact (is this a correctness benchmark?)"
        )
    if not Path(run.artifact_path).exists():
        raise ReferenceError(
            f"run id={run.id} artifact file is missing at {run.artifact_path!r}"
        )


def _build(project: str, benchmark: str, run: Run, notes: str | None) -> Reference:
    assert run.artifact_hash is not None and run.artifact_path is not None
    return Reference(
        project=project,
        benchmark=benchmark,
        frozen_at=utc_now(),
        frozen_git_sha=run.git_sha,
        content_hash=run.artifact_hash,
        content_path=run.artifact_path,
        from_run_id=run.id,
        notes=notes,
    )


def _write(ref: Reference) -> None:
    path = _ref_file(ref.project, ref.benchmark)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(ref), indent=2, sort_keys=True))
    tmp.chmod(0o600)
    tmp.replace(path)


def _append_history(project: str, benchmark: str, event: dict) -> None:
    path = _history_file(project, benchmark)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def _ref_file(project: str, benchmark: str) -> Path:
    return paths.references_dir() / project / f"{benchmark}.json"


def _history_file(project: str, benchmark: str) -> Path:
    return paths.references_dir() / project / f"{benchmark}.history.jsonl"
