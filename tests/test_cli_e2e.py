from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from benchstone.cli import main


def test_list_when_empty(isolated_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    assert "no projects registered" in result.output


def test_register_then_list(fake_project_path: Path, isolated_home: Path) -> None:
    runner = CliRunner()
    r1 = runner.invoke(main, ["register", str(fake_project_path)])
    assert r1.exit_code == 0, r1.output
    assert "registered FakeProject" in r1.output

    r2 = runner.invoke(main, ["list"])
    assert r2.exit_code == 0, r2.output
    assert "FakeProject" in r2.output
    assert "fake_quality" in r2.output
    assert "tier=quality" in r2.output
    assert "fake_correctness" in r2.output
    assert "tier=correctness" in r2.output


def test_list_filter_by_project(fake_project_path: Path, isolated_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_path)])

    r = runner.invoke(main, ["list", "--project", "FakeProject"])
    assert r.exit_code == 0, r.output
    assert "FakeProject" in r.output

    r = runner.invoke(main, ["list", "--project", "Nonexistent"])
    assert r.exit_code != 0
    assert "Nonexistent" in r.output


def test_register_bad_manifest(tmp_path: Path, isolated_home: Path) -> None:
    bad = tmp_path / "bad_project"
    (bad / "bench").mkdir(parents=True)
    (bad / "bench" / "manifest.toml").write_text("this is not valid toml = = =")

    runner = CliRunner()
    r = runner.invoke(main, ["register", str(bad)])
    assert r.exit_code != 0
    assert "invalid TOML" in r.output or "Error" in r.output
