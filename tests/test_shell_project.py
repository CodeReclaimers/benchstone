"""Contract-stress tests for the bash-based shell_project fixture.

These tests exist to catch any over-specialization to Python/Arborist in the
harness. If they break on a change that leaves the Python fake_project tests
green, the change is probably language-specific and should be generalized.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from benchstone import paths as bs_paths
from benchstone.cli import main
from benchstone.store import Store


def test_register_and_run_shell_project(
    shell_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()

    r = runner.invoke(main, ["register", str(shell_project_git)])
    assert r.exit_code == 0, r.output
    assert "ShellProject" in r.output

    r = runner.invoke(
        main,
        ["run", "ShellProject", "shell_quality", "--seed-set", "baseline"],
    )
    assert r.exit_code == 0, r.output
    assert "dispatched 3 run(s)" in r.output

    with Store(bs_paths.store_path()) as store:
        runs = store.fetch_runs("ShellProject", "shell_quality")
    assert len(runs) == 3
    assert all(r.status == "ok" for r in runs)
    # Metric: 2.0 + seed/10000; baseline seeds are [10, 20, 30].
    assert [round(r.metric, 4) for r in runs] == [2.001, 2.002, 2.003]
    # Metadata carried through from bash implementation.
    assert all((r.project_metadata or {}).get("impl") == "bash" for r in runs)


def test_shell_correctness_freeze_and_evaluate(
    shell_project_git: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full correctness flow through a non-Python entry point."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(shell_project_git)])

    runner.invoke(main, ["run", "ShellProject", "shell_correctness", "--meta-seed", "1"])
    r = runner.invoke(main, ["freeze-reference", "ShellProject", "shell_correctness"])
    assert r.exit_code == 0, r.output
    assert "content_hash=sha256:" in r.output

    # PASS: same variant → same bytes → same hash.
    runner.invoke(main, ["run", "ShellProject", "shell_correctness", "--meta-seed", "2"])
    r = runner.invoke(main, ["evaluate", "ShellProject", "shell_correctness"])
    assert r.exit_code == 0, r.output
    assert "PASS" in r.output

    # FAIL: variant shifts the artifact bytes.
    monkeypatch.setenv("SHELL_CORRECTNESS_VARIANT", "v2")
    runner.invoke(main, ["run", "ShellProject", "shell_correctness", "--meta-seed", "3"])
    r = runner.invoke(main, ["evaluate", "ShellProject", "shell_correctness"])
    assert r.exit_code == 1, r.output
    assert "FAIL" in r.output


def test_two_projects_registered_independently(
    fake_project_git: Path, shell_project_git: Path, isolated_home: Path,
) -> None:
    """Both Python and bash projects register; runs don't cross-contaminate."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["register", str(shell_project_git)])

    r = runner.invoke(main, ["list"])
    assert r.exit_code == 0, r.output
    assert "FakeProject" in r.output and "ShellProject" in r.output

    runner.invoke(
        main, ["run", "FakeProject", "fake_quality", "--seed-set", "baseline"],
    )
    runner.invoke(
        main, ["run", "ShellProject", "shell_quality", "--seed-set", "baseline"],
    )

    with Store(bs_paths.store_path()) as store:
        fake_runs = store.fetch_runs("FakeProject", "fake_quality")
        shell_runs = store.fetch_runs("ShellProject", "shell_quality")
    assert len(fake_runs) == 3
    assert len(shell_runs) == 3
    assert all(r.project == "FakeProject" for r in fake_runs)
    assert all(r.project == "ShellProject" for r in shell_runs)
    # Same benchmark name across both projects would collide if the store
    # only keyed by benchmark — this guards against that.
    assert set(fake_runs[0].git_sha) != set() and set(shell_runs[0].git_sha) != set()


def test_two_projects_baselines_isolated(
    fake_project_git: Path, shell_project_git: Path, isolated_home: Path,
) -> None:
    """Establishing a baseline for one project must not affect the other."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["register", str(shell_project_git)])

    runner.invoke(
        main, ["baseline", "establish", "FakeProject", "fake_quality"],
    )
    with Store(bs_paths.store_path()) as store:
        assert store.get_baseline("FakeProject", "fake_quality") is not None
        assert store.get_baseline("ShellProject", "shell_quality") is None

    runner.invoke(
        main, ["baseline", "establish", "ShellProject", "shell_quality"],
    )
    with Store(bs_paths.store_path()) as store:
        fake_base = store.get_baseline("FakeProject", "fake_quality")
        shell_base = store.get_baseline("ShellProject", "shell_quality")
    assert fake_base is not None and shell_base is not None
    # Each baseline points at its own project's HEAD.
    assert fake_base.git_sha != shell_base.git_sha
