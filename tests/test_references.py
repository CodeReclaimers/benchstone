from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from benchstone import paths, references
from benchstone.references import ReferenceError
from benchstone.store import Run


def _artifact(tmp_path: Path, content: bytes = b"hello reference") -> tuple[Path, str]:
    import hashlib

    path = tmp_path / "artifact.bin"
    path.write_bytes(content)
    return path, "sha256:" + hashlib.sha256(content).hexdigest()


def _run(artifact_path: Path, artifact_hash: str, run_id: int = 1) -> Run:
    return Run(
        id=run_id, project="FakeProject", benchmark="fake_correctness",
        git_sha="deadbeef1234567890", git_dirty=False, dirty_diff_path=None,
        timestamp="2026-04-19T14:22:11Z", harness_version="0.1.0",
        host="testhost", seed=0, meta_seed=1, repetition_index=0,
        status="ok", metric=None, metric_components=None,
        wall_clock_seconds=0.1, project_metadata=None, stderr_log_path=None,
        artifact_hash=artifact_hash, artifact_path=str(artifact_path),
    )


def test_freeze_writes_reference_and_history(
    tmp_path: Path, isolated_home: Path
) -> None:
    art_path, art_hash = _artifact(tmp_path)
    ref = references.freeze(
        "FakeProject", "fake_correctness",
        _run(art_path, art_hash),
        notes="initial capture",
    )
    assert ref.content_hash == art_hash
    assert ref.content_path == str(art_path)
    assert ref.notes == "initial capture"

    # Reference file and history file written.
    ref_file = paths.references_dir() / "FakeProject" / "fake_correctness.json"
    assert ref_file.exists()
    history = references.history("FakeProject", "fake_correctness")
    assert len(history) == 1
    assert history[0]["event"] == "frozen"


def test_freeze_refuses_when_reference_already_exists(
    tmp_path: Path, isolated_home: Path
) -> None:
    art_path, art_hash = _artifact(tmp_path)
    references.freeze(
        "FakeProject", "fake_correctness", _run(art_path, art_hash),
    )
    with pytest.raises(ReferenceError, match="already exists"):
        references.freeze(
            "FakeProject", "fake_correctness", _run(art_path, art_hash),
        )


def test_replace_requires_reason(tmp_path: Path, isolated_home: Path) -> None:
    art_path, art_hash = _artifact(tmp_path)
    references.freeze(
        "FakeProject", "fake_correctness", _run(art_path, art_hash),
    )
    new_art, new_hash = _artifact(tmp_path, content=b"updated reference")
    with pytest.raises(ReferenceError, match="reason"):
        references.replace(
            "FakeProject", "fake_correctness",
            _run(new_art, new_hash), reason="",
        )
    with pytest.raises(ReferenceError, match="reason"):
        references.replace(
            "FakeProject", "fake_correctness",
            _run(new_art, new_hash), reason="   ",
        )


def test_replace_without_prior_refuses(tmp_path: Path, isolated_home: Path) -> None:
    art_path, art_hash = _artifact(tmp_path)
    with pytest.raises(ReferenceError, match="no existing reference"):
        references.replace(
            "FakeProject", "fake_correctness",
            _run(art_path, art_hash), reason="new code",
        )


def test_replace_writes_new_and_logs_history(
    tmp_path: Path, isolated_home: Path
) -> None:
    art_path, art_hash = _artifact(tmp_path)
    references.freeze(
        "FakeProject", "fake_correctness", _run(art_path, art_hash),
        notes="v1",
    )
    new_art, new_hash = _artifact(tmp_path, content=b"updated reference")
    ref = references.replace(
        "FakeProject", "fake_correctness",
        _run(new_art, new_hash, run_id=2),
        reason="behavior intentionally changed by commit abc123",
        notes="v2",
    )
    assert ref.content_hash == new_hash
    assert references.get("FakeProject", "fake_correctness").content_hash == new_hash

    history = references.history("FakeProject", "fake_correctness")
    assert [e["event"] for e in history] == ["frozen", "replaced"]
    assert history[1]["reason"] == "behavior intentionally changed by commit abc123"
    assert history[1]["prior"]["content_hash"] == art_hash
    assert history[1]["new"]["content_hash"] == new_hash


def test_freeze_rejects_run_without_artifact(
    tmp_path: Path, isolated_home: Path
) -> None:
    art_path, art_hash = _artifact(tmp_path)
    run_no_art = _run(art_path, art_hash).__class__(
        id=1, project="P", benchmark="B", git_sha="sha",
        git_dirty=False, dirty_diff_path=None,
        timestamp="t", harness_version="0.1.0", host="h",
        seed=0, meta_seed=0, repetition_index=0,
        status="ok", metric=None, metric_components=None,
        wall_clock_seconds=0.0, project_metadata=None, stderr_log_path=None,
        artifact_hash=None, artifact_path=None,
    )
    with pytest.raises(ReferenceError, match="no artifact"):
        references.freeze("P", "B", run_no_art)


def test_freeze_rejects_missing_artifact_file(
    tmp_path: Path, isolated_home: Path
) -> None:
    art_path, art_hash = _artifact(tmp_path)
    art_path.unlink()
    with pytest.raises(ReferenceError, match="missing"):
        references.freeze(
            "P", "B", _run(art_path, art_hash),
        )


def test_get_returns_none_when_absent(isolated_home: Path) -> None:
    assert references.get("Nope", "nothing") is None


def test_exists(tmp_path: Path, isolated_home: Path) -> None:
    art_path, art_hash = _artifact(tmp_path)
    assert not references.exists("P", "B")
    references.freeze("P", "B", _run(art_path, art_hash))
    assert references.exists("P", "B")
