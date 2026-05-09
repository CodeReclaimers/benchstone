from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

from benchstone.registry import Registry, RegistryError


def test_register_and_list(fake_project_path: Path, isolated_home: Path) -> None:
    reg = Registry()
    result = reg.register(fake_project_path)
    rp = result.project
    assert rp.name == "FakeProject"
    assert rp.path == fake_project_path.resolve()
    assert rp.manifest_hash.startswith("sha256:")
    assert result.prior_path is None

    listed = reg.list_projects()
    assert [p.name for p in listed] == ["FakeProject"]
    assert listed[0].manifest_hash == rp.manifest_hash


def test_resolve_unknown_raises(isolated_home: Path) -> None:
    reg = Registry()
    with pytest.raises(RegistryError, match="not registered"):
        reg.resolve("nope")


def test_register_persists_to_file(fake_project_path: Path, isolated_home: Path) -> None:
    Registry().register(fake_project_path)
    # A fresh Registry() instance should see the previously registered project.
    assert Registry().resolve("FakeProject").name == "FakeProject"


def test_register_rejects_non_directory(tmp_path: Path, isolated_home: Path) -> None:
    f = tmp_path / "not-a-dir.txt"
    f.write_text("x")
    with pytest.raises(RegistryError, match="not a directory"):
        Registry().register(f)


def test_register_is_idempotent(fake_project_path: Path, isolated_home: Path) -> None:
    reg = Registry()
    reg.register(fake_project_path)
    reg.register(fake_project_path)
    assert len(reg.list_projects()) == 1


def test_register_reports_prior_path_on_move(
    fake_project_path: Path, tmp_path: Path, isolated_home: Path
) -> None:
    reg = Registry()
    first = reg.register(fake_project_path)
    assert first.prior_path is None

    # Copy the project to a new location and re-register — the pointer moves
    # and the caller is given the old path so it can warn.
    moved = tmp_path / "moved"
    shutil.copytree(fake_project_path, moved)
    second = reg.register(moved)
    assert second.prior_path == fake_project_path.resolve()
    assert second.project.path == moved.resolve()


def test_register_warns_on_corpus_drift(
    fake_project_path: Path, tmp_path: Path, isolated_home: Path
) -> None:
    """Corpus drift caught at register time saves the user from finding out
    only after a wasted run-time wall-clock budget."""
    target = tmp_path / "DriftedProject"
    shutil.copytree(fake_project_path, target)
    # Mutate the corpus so its hash no longer matches the manifest's
    # corpus_hash for fake_quality.
    corpus_file = target / "bench" / "corpus" / "fake"
    corpus_file.write_bytes(b"definitely not the original corpus")

    with pytest.warns(UserWarning, match="corpus drift"):
        Registry().register(target)


def test_register_warns_on_missing_corpus(
    fake_project_path: Path, tmp_path: Path, isolated_home: Path
) -> None:
    """Missing corpus surfaces at register time too — the run would otherwise
    error after admission and dispatch."""
    target = tmp_path / "MissingCorpusProject"
    shutil.copytree(fake_project_path, target)
    (target / "bench" / "corpus" / "fake").unlink()

    with pytest.warns(UserWarning, match="corpus is missing"):
        Registry().register(target)


def test_register_does_not_warn_on_clean_corpus(
    fake_project_path: Path, isolated_home: Path
) -> None:
    """A pristine fixture must not trip either corpus warning — that would
    make the warnings useless noise."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Registry().register(fake_project_path)


def test_unregister_removes_entry(
    fake_project_path: Path, isolated_home: Path
) -> None:
    reg = Registry()
    reg.register(fake_project_path)
    prior = reg.unregister("FakeProject")
    assert prior == fake_project_path.resolve()
    with pytest.raises(RegistryError, match="not registered"):
        reg.resolve("FakeProject")


def test_unregister_unknown_raises(isolated_home: Path) -> None:
    with pytest.raises(RegistryError, match="not registered"):
        Registry().unregister("nope")
