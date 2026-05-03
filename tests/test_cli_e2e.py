from __future__ import annotations

from pathlib import Path

import pytest
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


def test_run_background_dispatches_job(
    fake_project_git: Path, isolated_home: Path
) -> None:
    import time as _time

    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    r = runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "baseline", "--background"],
    )
    assert r.exit_code == 0, r.output
    assert "dispatched background job" in r.output

    # Poll until the spawned job reports a terminal status so we don't leave
    # a detached child running against torn-down test state.
    from benchstone import jobs as _jobs
    deadline = _time.monotonic() + 10.0
    job_list = _jobs.list_all()
    assert len(job_list) == 1
    job_id = job_list[0].job_id
    while _time.monotonic() < deadline:
        j = _jobs.load(job_id)
        if j.status in _jobs.TERMINAL_STATUSES:
            break
        _time.sleep(0.05)
    assert _jobs.load(job_id).status == "done"

    with Store(bs_paths.store_path()) as store:
        runs = store.fetch_runs("FakeProject", "fake_quality")
        assert len(runs) == 3
        assert all(r.status == "ok" for r in runs)


def test_status_lists_jobs(fake_project_git: Path, isolated_home: Path) -> None:
    import time as _time

    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    # No jobs yet.
    r = runner.invoke(main, ["status"])
    assert r.exit_code == 0, r.output
    assert "no active jobs" in r.output

    runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "baseline", "--background"],
    )

    # Poll for completion before asserting terminal-status output.
    from benchstone import jobs as _jobs
    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        listed = _jobs.list_all()
        if listed and listed[0].status in _jobs.TERMINAL_STATUSES:
            break
        _time.sleep(0.05)

    r = runner.invoke(main, ["status", "--all"])
    assert r.exit_code == 0, r.output
    assert "FakeProject/fake_quality" in r.output
    assert "done" in r.output


def test_scheduler_refuses_overcommit(
    fake_project_git: Path,
    isolated_home: Path,
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    monkeypatch.setenv("BENCHSTONE_MAX_THREADS", "2")
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    # fake_heavy declares threads=4; with MAX_THREADS=2 the scheduler must refuse.
    r = runner.invoke(
        main,
        ["run", "FakeProject", "fake_heavy",
         "--seed-set", "baseline", "--foreground"],
    )
    assert r.exit_code != 0
    assert "thread" in r.output.lower() or "capacity" in r.output.lower()


def test_background_required_auto_dispatches(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """fake_heavy has background_required=true in the manifest; the CLI should
    default to background dispatch even without an explicit --background flag."""
    import time as _time

    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    r = runner.invoke(
        main,
        ["run", "FakeProject", "fake_heavy", "--seed-set", "baseline"],
    )
    assert r.exit_code == 0, r.output
    assert "dispatched background job" in r.output

    from benchstone import jobs as _jobs
    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        listed = _jobs.list_all()
        if listed and listed[0].status in _jobs.TERMINAL_STATUSES:
            break
        _time.sleep(0.05)
    assert _jobs.list_all()[0].status == "done"


def test_baseline_establish_background_sets_pointer_on_completion(
    fake_project_git: Path, isolated_home: Path
) -> None:
    import time as _time

    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    r = runner.invoke(
        main,
        ["baseline", "establish", "FakeProject", "fake_quality",
         "--background", "--notes", "bg-set"],
    )
    assert r.exit_code == 0, r.output
    assert "dispatched background job" in r.output

    from benchstone import jobs as _jobs
    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        listed = _jobs.list_all()
        if listed and listed[0].status in _jobs.TERMINAL_STATUSES:
            break
        _time.sleep(0.05)
    assert _jobs.list_all()[0].status == "done"

    with Store(bs_paths.store_path()) as store:
        base = store.get_baseline("FakeProject", "fake_quality")
    assert base is not None
    assert base.notes == "bg-set"


def test_baseline_establish_at_sha(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """Establishing at a past SHA uses a worktree and points the baseline there."""
    import subprocess

    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(fake_project_git), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    # Make a second commit so HEAD != HEAD^.
    (fake_project_git / "CHANGE.txt").write_text("noop")
    _git("add", "CHANGE.txt")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "second")

    head = _git("rev-parse", "HEAD")
    parent = _git("rev-parse", "HEAD^")
    assert head != parent

    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    r = runner.invoke(
        main,
        ["baseline", "establish", "FakeProject", "fake_quality",
         "--at-sha", parent, "--notes", "retro-baseline"],
    )
    assert r.exit_code == 0, r.output
    assert "establishing baseline in worktree" in r.output
    assert "baseline set" in r.output

    with Store(bs_paths.store_path()) as store:
        base = store.get_baseline("FakeProject", "fake_quality")
        runs = store.fetch_baseline_runs(
            "FakeProject", "fake_quality", parent
        )
    assert base is not None
    assert base.git_sha == parent   # baseline points at the past SHA, not HEAD
    assert "at-sha" in (base.notes or "")
    assert len(runs) == 3
    assert all(r.git_sha == parent for r in runs)


def test_baseline_establish_at_sha_refuses_background(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    r = runner.invoke(
        main,
        ["baseline", "establish", "FakeProject", "fake_quality",
         "--at-sha", "HEAD", "--background"],
    )
    assert r.exit_code != 0
    assert "--at-sha is incompatible with --background" in r.output


def test_baseline_establish_at_sha_rejects_unknown_benchmark(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    r = runner.invoke(
        main,
        ["baseline", "establish", "FakeProject", "no_such_benchmark",
         "--at-sha", "HEAD"],
    )
    assert r.exit_code != 0


def test_run_repetitions_override_shrinks_to_one(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """Smoke-test path: --repetitions 1 with --seed-set baseline uses the first seed only."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    r = runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "baseline", "--repetitions", "1"],
    )
    assert r.exit_code == 0, r.output
    assert "dispatched 1 run(s)" in r.output

    with Store(bs_paths.store_path()) as store:
        runs = store.fetch_runs("FakeProject", "fake_quality")
    assert len(runs) == 1
    assert runs[0].seed == 1  # baseline_seeds[0]


def test_run_repetitions_rejects_excessive_baseline(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    r = runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "baseline", "--repetitions", "5"],
    )
    assert r.exit_code != 0
    assert "exceeds baseline_seeds length" in r.output


def test_run_repetitions_fresh_scales_freely(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    # Fresh seeds come from the meta-seed PRNG; any count goes.
    r = runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "fresh", "--meta-seed", "7", "--repetitions", "8"],
    )
    assert r.exit_code == 0, r.output
    assert "dispatched 8 run(s)" in r.output


def test_baseline_establish_with_repetitions_override(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    r = runner.invoke(
        main,
        ["baseline", "establish", "FakeProject", "fake_quality",
         "--repetitions", "2"],
    )
    assert r.exit_code == 0, r.output
    with Store(bs_paths.store_path()) as store:
        baseline_runs = store.fetch_baseline_runs(
            "FakeProject", "fake_quality", store.get_baseline(
                "FakeProject", "fake_quality"
            ).git_sha,
        )
    assert len(baseline_runs) == 2
    assert [r.seed for r in baseline_runs] == [1, 2]


def test_evaluate_expect_match_exits_zero(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "fresh", "--meta-seed", "42"],
    )
    # Known REJECT case from earlier tests.
    r = runner.invoke(
        main,
        ["evaluate", "FakeProject", "fake_quality", "--expect", "REJECT"],
    )
    assert r.exit_code == 0, r.output
    assert "expected:    REJECT  (match)" in r.output


def test_evaluate_expect_mismatch_exits_four(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "fresh", "--meta-seed", "42"],
    )
    r = runner.invoke(
        main,
        ["evaluate", "FakeProject", "fake_quality", "--expect", "PROMOTE"],
    )
    assert r.exit_code == 4, r.output
    assert "MISMATCH" in r.output


def test_evaluate_expect_rejects_invalid_kind(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    r = runner.invoke(
        main,
        ["evaluate", "FakeProject", "fake_quality", "--expect", "nope"],
    )
    assert r.exit_code != 0
    assert "--expect" in r.output


def test_evaluate_baseline_cv_warning(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """Synthesize a baseline with high CV by inserting hand-crafted rows."""
    import socket
    from datetime import datetime, timezone

    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    from benchstone.provenance import git_state as _gs
    sha = _gs(fake_project_git).sha

    def _row(seed: int, metric: float, meta_seed: int | None) -> dict:
        return dict(
            project="FakeProject", benchmark="fake_quality",
            git_sha=sha, git_dirty=False,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            harness_version="0.1.0", host=socket.gethostname(),
            seed=seed, meta_seed=meta_seed, repetition_index=0,
            status="ok", metric=metric, wall_clock_seconds=0.001,
        )

    with Store(bs_paths.store_path()) as store:
        # Baseline set with a strong outlier (mirrors the Arborist pathology).
        for seed, metric in [(1, 1.25), (2, 1.26), (3, 1.27), (4, 1.83), (5, 1.28)]:
            store.insert_run(**_row(seed, metric, None))
        # Candidate set — similar center, no outlier.
        for seed, metric in [(100, 1.24), (101, 1.25), (102, 1.26), (103, 1.27), (104, 1.28)]:
            store.insert_run(**_row(seed, metric, 42))
        store.set_baseline(
            "FakeProject", "fake_quality", sha,
            "2026-04-20T00:00:00Z", notes="synthetic",
        )

    r = runner.invoke(main, ["evaluate", "FakeProject", "fake_quality"])
    assert "cv=" in r.output
    assert "baseline cv=" in r.output and "> 0.05" in r.output


def test_correctness_freeze_then_evaluate_passes(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """End-to-end correctness flow: run → freeze-reference → re-run → evaluate → PASS."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    r = runner.invoke(main, ["run", "FakeProject", "fake_correctness",
                             "--meta-seed", "1"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(main, ["freeze-reference", "FakeProject", "fake_correctness",
                             "--notes", "v1 reference"])
    assert r.exit_code == 0, r.output
    assert "frozen reference" in r.output
    assert "content_hash=sha256:" in r.output

    # Second run produces an identical artifact (fake_correctness is bytewise
    # deterministic in its default configuration).
    runner.invoke(main, ["run", "FakeProject", "fake_correctness",
                         "--meta-seed", "2"])

    r = runner.invoke(main, ["evaluate", "FakeProject", "fake_correctness"])
    assert r.exit_code == 0, r.output
    assert "PASS" in r.output
    assert "tier:        correctness" in r.output


def test_correctness_evaluate_fails_after_variant_shift(
    fake_project_git: Path, isolated_home: Path, monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """After freezing a v1 reference, a v2 run (via FAKE_CORRECTNESS_VARIANT)
    produces a different artifact hash — the gate must return FAIL with exit 1."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    # Freeze the v1 reference.
    runner.invoke(main, ["run", "FakeProject", "fake_correctness", "--meta-seed", "1"])
    runner.invoke(main, ["freeze-reference", "FakeProject", "fake_correctness"])

    # Run again under v2 so the artifact bytes diverge.
    monkeypatch.setenv("FAKE_CORRECTNESS_VARIANT", "v2")
    runner.invoke(main, ["run", "FakeProject", "fake_correctness", "--meta-seed", "2"])

    r = runner.invoke(main, ["evaluate", "FakeProject", "fake_correctness"])
    assert r.exit_code == 1, r.output
    assert "FAIL" in r.output
    assert "differs from reference" in r.output


def test_evaluate_no_reference_exits_2(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["run", "FakeProject", "fake_correctness", "--meta-seed", "1"])
    r = runner.invoke(main, ["evaluate", "FakeProject", "fake_correctness"])
    assert r.exit_code == 2, r.output
    assert "NO_REFERENCE" in r.output


def test_freeze_reference_refuses_on_non_correctness(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    r = runner.invoke(main, ["freeze-reference", "FakeProject", "fake_quality"])
    assert r.exit_code != 0
    assert "correctness-tier" in r.output


def test_replace_reference_requires_reason_via_cli(
    fake_project_git: Path, isolated_home: Path, monkeypatch: "pytest.MonkeyPatch",
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["run", "FakeProject", "fake_correctness", "--meta-seed", "1"])
    runner.invoke(main, ["freeze-reference", "FakeProject", "fake_correctness"])

    monkeypatch.setenv("FAKE_CORRECTNESS_VARIANT", "v2")
    runner.invoke(main, ["run", "FakeProject", "fake_correctness", "--meta-seed", "2"])

    # Missing --reason triggers click's required-option error before our checks.
    r = runner.invoke(main, ["replace-reference", "FakeProject", "fake_correctness"])
    assert r.exit_code != 0

    r = runner.invoke(
        main,
        ["replace-reference", "FakeProject", "fake_correctness",
         "--reason", "intentional behavior change"],
    )
    assert r.exit_code == 0, r.output
    assert "replaced reference" in r.output
    assert "intentional behavior change" in r.output


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
    # Pre-promote pointer was set by `baseline establish` → meta_seed=NULL.
    assert base_before.meta_seed is None

    r = runner.invoke(
        main,
        ["promote", "FakeProject", "fake_quality", "--force", "--notes", "forced"],
    )
    assert r.exit_code == 0, r.output
    assert "baseline promoted" in r.output
    assert "baseline meta_seed=42" in r.output

    with Store(bs_paths.store_path()) as store:
        base_after = store.get_baseline("FakeProject", "fake_quality")
    assert base_after is not None
    assert base_after.notes == "forced"
    # Same SHA (no commit moved) but timestamp/notes updated, and the pointer
    # now records the candidate run set's meta_seed so the next `bench evaluate`
    # sees the promoted runs as baseline.
    assert base_after.established_at >= base_before.established_at
    assert base_after.meta_seed == 42


def _git_commit_whitespace(repo: Path, marker: str) -> str:
    """Make a trivial commit in ``repo`` and return the resulting HEAD SHA."""
    import subprocess

    (repo / "WHITESPACE").write_text(marker + "\n")
    subprocess.run(["git", "add", "WHITESPACE"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", f"whitespace {marker}"],
                   cwd=repo, check=True, capture_output=True)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def test_promote_then_evaluate_does_not_need_more_data(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """Regression: pre-fix, `bench promote` moved the pointer to a SHA with no
    meta_seed=NULL runs, so the next `bench evaluate` returned NEEDS_MORE_DATA
    until the user re-ran `baseline establish`. With the meta_seed-on-pointer
    fix, the promoted candidate runs become the baseline at the new SHA and
    evaluate returns a real verdict immediately."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])

    # Move HEAD to a new SHA, then run a candidate set there.
    new_sha = _git_commit_whitespace(fake_project_git, "promote-roundtrip")
    runner.invoke(
        main, ["run", "FakeProject", "fake_quality",
               "--seed-set", "fresh", "--meta-seed", "77"],
    )

    # Force-promote (the candidate metrics are worse than baseline, so the
    # gate would say REJECT — but that's irrelevant; we want to test the
    # *pointer* behavior, not the gate decision).
    rp = runner.invoke(
        main,
        ["promote", "FakeProject", "fake_quality", "--force", "--notes", "rp"],
    )
    assert rp.exit_code == 0, rp.output
    assert f"-> {new_sha[:10]}" in rp.output
    assert "baseline meta_seed=77" in rp.output

    # Next evaluate must NOT return NEEDS_MORE_DATA: the promoted candidate
    # group is now visible as the baseline set at the new SHA.
    re = runner.invoke(main, ["evaluate", "FakeProject", "fake_quality"])
    assert "NEEDS_MORE_DATA" not in re.output, re.output
    # Baseline is the promoted set (meta_seed=77, 3 runs) and the candidate
    # set at this SHA is the same group, so the gate runs to a verdict
    # (sigma == 0 → REJECT). The point is it produced a verdict, not which.
    assert re.exit_code in (0, 1), re.output


def test_promote_refuses_on_multiple_candidate_meta_seeds(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """Two `bench evaluate` invocations at the same SHA produce two distinct
    meta_seed groups. promote refuses to silently choose between them."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main, ["run", "FakeProject", "fake_quality",
               "--seed-set", "fresh", "--meta-seed", "100"],
    )
    runner.invoke(
        main, ["run", "FakeProject", "fake_quality",
               "--seed-set", "fresh", "--meta-seed", "200"],
    )

    r = runner.invoke(
        main,
        ["promote", "FakeProject", "fake_quality", "--force", "--notes", "ambig"],
    )
    assert r.exit_code != 0
    assert "multiple meta_seeds" in r.output
    assert "[100, 200]" in r.output


def test_promote_with_explicit_meta_seed_chooses_group(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """When the user disambiguates with --meta-seed, that group becomes baseline."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main, ["run", "FakeProject", "fake_quality",
               "--seed-set", "fresh", "--meta-seed", "100"],
    )
    runner.invoke(
        main, ["run", "FakeProject", "fake_quality",
               "--seed-set", "fresh", "--meta-seed", "200"],
    )

    r = runner.invoke(
        main,
        ["promote", "FakeProject", "fake_quality",
         "--force", "--meta-seed", "100", "--notes", "explicit"],
    )
    assert r.exit_code == 0, r.output
    assert "baseline meta_seed=100" in r.output

    with Store(bs_paths.store_path()) as store:
        base = store.get_baseline("FakeProject", "fake_quality")
    assert base is not None
    assert base.meta_seed == 100


def test_promote_with_unknown_meta_seed_fails(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main, ["run", "FakeProject", "fake_quality",
               "--seed-set", "fresh", "--meta-seed", "100"],
    )
    r = runner.invoke(
        main,
        ["promote", "FakeProject", "fake_quality",
         "--force", "--meta-seed", "999"],
    )
    assert r.exit_code != 0
    assert "--meta-seed 999 not found" in r.output
