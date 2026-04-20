from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from benchstone.cli import main


def _setup_with_baseline(runner: CliRunner, project_path: Path) -> None:
    runner.invoke(main, ["register", str(project_path)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "fresh", "--meta-seed", "42"],
    )


def test_history_empty(fake_project_git: Path, isolated_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    r = runner.invoke(main, ["history", "FakeProject", "fake_quality"])
    assert r.exit_code == 0
    assert "no runs match" in r.output


def test_history_shows_all_runs(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    _setup_with_baseline(runner, fake_project_git)

    r = runner.invoke(main, ["history", "FakeProject", "fake_quality"])
    assert r.exit_code == 0, r.output
    lines = [ln for ln in r.output.splitlines() if ln.startswith("20")]
    assert len(lines) == 6  # 3 baseline + 3 fresh
    baseline_lines = [ln for ln in lines if "baseline" in ln]
    fresh_lines = [ln for ln in lines if "meta=42" in ln]
    assert len(baseline_lines) == 3
    assert len(fresh_lines) == 3


def test_history_baseline_marker(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    _setup_with_baseline(runner, fake_project_git)
    r = runner.invoke(main, ["history", "FakeProject", "fake_quality"])
    # A header line names the baseline SHA.
    assert "# baseline @" in r.output
    # Every run in the timeline is at the baseline SHA in this setup, so they
    # all carry the '*' marker.
    assert "  *run=" in r.output


def test_history_limit(fake_project_git: Path, isolated_home: Path) -> None:
    runner = CliRunner()
    _setup_with_baseline(runner, fake_project_git)
    r = runner.invoke(
        main, ["history", "FakeProject", "fake_quality", "--limit", "2"],
    )
    assert r.exit_code == 0
    lines = [ln for ln in r.output.splitlines() if ln.startswith("20")]
    assert len(lines) == 2


def test_history_git_sha_filter(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    _setup_with_baseline(runner, fake_project_git)
    # All runs happen at the same SHA in this setup; a prefix filter that
    # matches nothing returns the empty case.
    r = runner.invoke(
        main,
        ["history", "FakeProject", "fake_quality", "--git-sha", "deadbeef"],
    )
    assert "no runs match" in r.output


def test_history_since_filter(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    _setup_with_baseline(runner, fake_project_git)
    r = runner.invoke(
        main,
        ["history", "FakeProject", "fake_quality", "--since", "2099-01-01"],
    )
    assert "no runs match" in r.output


def test_history_across_projects_does_not_collide(
    fake_project_git: Path, shell_project_git: Path, isolated_home: Path,
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["register", str(shell_project_git)])
    runner.invoke(
        main, ["run", "FakeProject", "fake_quality", "--seed-set", "baseline"],
    )
    runner.invoke(
        main, ["run", "ShellProject", "shell_quality", "--seed-set", "baseline"],
    )

    fake_rows = [
        ln for ln in runner.invoke(
            main, ["history", "FakeProject", "fake_quality"]
        ).output.splitlines() if ln.startswith("20")
    ]
    shell_rows = [
        ln for ln in runner.invoke(
            main, ["history", "ShellProject", "shell_quality"]
        ).output.splitlines() if ln.startswith("20")
    ]
    assert len(fake_rows) == 3
    assert len(shell_rows) == 3
    # Each history shows its own baseline seed set, not the other's.
    assert all(any(f"seed={s} " in row for s in (1, 2, 3)) for row in fake_rows)
    assert all(any(f"seed={s} " in row for s in (10, 20, 30)) for row in shell_rows)
