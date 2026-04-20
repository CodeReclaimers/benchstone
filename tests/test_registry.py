from __future__ import annotations

from pathlib import Path

import pytest

from benchstone.registry import Registry, RegistryError


def test_register_and_list(fake_project_path: Path, isolated_home: Path) -> None:
    reg = Registry()
    rp = reg.register(fake_project_path)
    assert rp.name == "FakeProject"
    assert rp.path == fake_project_path.resolve()
    assert rp.manifest_hash.startswith("sha256:")

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
