from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from benchstone import paths as bs_paths
from benchstone.cli import main
from benchstone.store import Store


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


def test_run_command_against_git_fixture(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    assert runner.invoke(main, ["register", str(fake_project_git)]).exit_code == 0

    r = runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "baseline"],
    )
    assert r.exit_code == 0, r.output
    assert "dispatched 3 run(s)" in r.output

    with Store(bs_paths.store_path()) as store:
        runs = store.fetch_runs("FakeProject", "fake_quality")
        assert len(runs) == 3
        assert all(r.status == "ok" for r in runs)


def test_baseline_establish_sets_pointer(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    r = runner.invoke(
        main,
        ["baseline", "establish", "FakeProject", "fake_quality",
         "--notes", "initial baseline"],
    )
    assert r.exit_code == 0, r.output
    assert "baseline set" in r.output

    with Store(bs_paths.store_path()) as store:
        base = store.get_baseline("FakeProject", "fake_quality")
        assert base is not None
        assert base.notes == "initial baseline"
        assert base.git_sha  # non-empty sha


def test_evaluate_at_same_sha_rejects(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """Baseline seeds [1,2,3] produce metrics ~1.000 in the fake project;
    fresh seeds drawn from meta_seed=42 produce much larger metrics, so direction
    'minimize' sees a regression and the verdict is REJECT even at the same SHA.
    """
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "fresh", "--meta-seed", "42"],
    )

    r = runner.invoke(main, ["evaluate", "FakeProject", "fake_quality"])
    assert r.exit_code == 1, r.output
    assert "REJECT" in r.output
    # Confirm the gate is actually distinguishing baseline from candidate
    # (would be identical if the meta_seed split weren't applied).
    for line in r.output.splitlines():
        if line.strip().startswith("baseline:"):
            baseline_line = line
        if line.strip().startswith("candidate:"):
            candidate_line = line
    assert baseline_line != candidate_line
    assert "mean=1.000" in baseline_line  # ~1.0005 from seeds 1,2,3
    assert "mean=1.0" in candidate_line and "mean=1.000" not in candidate_line


def test_evaluate_no_baseline_exits_2(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    r = runner.invoke(main, ["evaluate", "FakeProject", "fake_quality"])
    assert r.exit_code == 2, r.output
    assert "NO_BASELINE" in r.output


def test_promote_refuses_without_force_on_reject(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main, ["run", "FakeProject", "fake_quality",
               "--seed-set", "fresh", "--meta-seed", "42"]
    )

    r = runner.invoke(main, ["promote", "FakeProject", "fake_quality"])
    assert r.exit_code != 0
    assert "refusing to promote" in r.output


def test_promote_force_moves_pointer(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main, ["run", "FakeProject", "fake_quality",
               "--seed-set", "fresh", "--meta-seed", "42"]
    )

    with Store(bs_paths.store_path()) as store:
        base_before = store.get_baseline("FakeProject", "fake_quality")
    assert base_before is not None

    r = runner.invoke(
        main,
        ["promote", "FakeProject", "fake_quality", "--force", "--notes", "forced"],
    )
    assert r.exit_code == 0, r.output
    assert "baseline promoted" in r.output

    with Store(bs_paths.store_path()) as store:
        base_after = store.get_baseline("FakeProject", "fake_quality")
    assert base_after is not None
    assert base_after.notes == "forced"
    # Same SHA (no commit moved) but timestamp/notes updated.
    assert base_after.established_at >= base_before.established_at
