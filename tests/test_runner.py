from __future__ import annotations

import math
from pathlib import Path

import pytest

from benchstone.manifest import load as load_manifest
from benchstone.provenance import git_state
from benchstone.runner import (
    RunnerError,
    execute,
    plan_baseline,
    plan_evaluation,
)
from benchstone.store import Store


def _expected_metric(seed: int) -> float:
    """Mirror of fake_quality's metric function in the fixture."""
    base = 1.0 + (seed % 1000) / 10000.0
    return base + 0.0005 * math.sin(seed)


def test_plan_baseline_uses_manifest_seeds(fake_project_git: Path) -> None:
    manifest = load_manifest(fake_project_git)
    b = manifest.benchmark("fake_quality")
    plan = plan_baseline(b, git_state(fake_project_git), allow_dirty=False)
    assert plan.seeds == (1, 2, 3)
    assert plan.meta_seed is None


def test_plan_evaluation_derives_stable_seeds(fake_project_git: Path) -> None:
    manifest = load_manifest(fake_project_git)
    b = manifest.benchmark("fake_quality")
    gstate = git_state(fake_project_git)
    p1 = plan_evaluation(b, gstate, allow_dirty=False, meta_seed=42)
    p2 = plan_evaluation(b, gstate, allow_dirty=False, meta_seed=42)
    assert p1.seeds == p2.seeds
    assert p1.meta_seed == 42 and len(p1.seeds) == 3
    p3 = plan_evaluation(b, gstate, allow_dirty=False, meta_seed=43)
    assert p3.seeds != p1.seeds


def test_plan_baseline_honors_repetitions_override(fake_project_git: Path) -> None:
    manifest = load_manifest(fake_project_git)
    b = manifest.benchmark("fake_quality")  # baseline_seeds=[1,2,3]
    gstate = git_state(fake_project_git)
    plan = plan_baseline(b, gstate, allow_dirty=False, repetitions=1)
    assert plan.seeds == (1,)


def test_plan_baseline_rejects_repetitions_gt_seed_count(
    fake_project_git: Path,
) -> None:
    from benchstone.runner import RunnerError

    manifest = load_manifest(fake_project_git)
    b = manifest.benchmark("fake_quality")  # baseline_seeds=[1,2,3]
    with pytest.raises(RunnerError, match="exceeds baseline_seeds length"):
        plan_baseline(b, git_state(fake_project_git), allow_dirty=False, repetitions=5)


def test_plan_evaluation_honors_repetitions_override(
    fake_project_git: Path,
) -> None:
    manifest = load_manifest(fake_project_git)
    b = manifest.benchmark("fake_quality")  # manifest says repetitions=3
    plan = plan_evaluation(
        b, git_state(fake_project_git), allow_dirty=False,
        meta_seed=42, repetitions=1,
    )
    assert len(plan.seeds) == 1


def test_execute_baseline_against_fake_project(
    fake_project_git: Path, tmp_path: Path
) -> None:
    manifest = load_manifest(fake_project_git)
    bench = manifest.benchmark("fake_quality")
    gstate = git_state(fake_project_git)
    plan = plan_baseline(bench, gstate, allow_dirty=False)

    logs = tmp_path / "logs"
    with Store(tmp_path / "store.db") as store:
        ids = execute(
            project=manifest.project,
            project_path=fake_project_git,
            benchmark=bench,
            plan=plan,
            store=store,
            host="testhost",
            logs_root=logs,
        )
        assert len(ids) == 3
        runs = store.fetch_runs("FakeProject", "fake_quality")
        assert [r.status for r in runs] == ["ok", "ok", "ok"]
        assert [r.seed for r in runs] == [1, 2, 3]
        assert all(r.meta_seed is None for r in runs)
        assert all(r.git_sha == gstate.sha for r in runs)
        assert all(r.git_dirty is False for r in runs)
        for r in runs:
            assert r.metric is not None
            assert r.metric == pytest.approx(_expected_metric(r.seed))
            assert Path(r.stderr_log_path).exists()


def test_execute_rejects_dirty_tree_without_override(
    fake_project_git: Path, tmp_path: Path
) -> None:
    (fake_project_git / "scratch.txt").write_text("untracked")
    manifest = load_manifest(fake_project_git)
    bench = manifest.benchmark("fake_quality")
    gstate = git_state(fake_project_git)
    assert gstate.dirty
    plan = plan_baseline(bench, gstate, allow_dirty=False)
    with Store(tmp_path / "store.db") as store:
        with pytest.raises(RunnerError, match="dirty git tree"):
            execute(
                project=manifest.project, project_path=fake_project_git,
                benchmark=bench, plan=plan, store=store,
                host="testhost", logs_root=tmp_path / "logs",
            )


def test_execute_allows_dirty_with_override_and_saves_diff(
    fake_project_git: Path, tmp_path: Path
) -> None:
    mf_path = fake_project_git / "bench" / "manifest.toml"
    mf_path.write_text(mf_path.read_text() + "\n# drift marker\n")
    manifest = load_manifest(fake_project_git)
    bench = manifest.benchmark("fake_quality")
    gstate = git_state(fake_project_git)
    assert gstate.dirty and "drift marker" in gstate.diff
    plan = plan_baseline(bench, gstate, allow_dirty=True)
    with Store(tmp_path / "store.db") as store:
        execute(
            project=manifest.project, project_path=fake_project_git,
            benchmark=bench, plan=plan, store=store,
            host="testhost", logs_root=tmp_path / "logs",
        )
        runs = store.fetch_runs("FakeProject", "fake_quality")
        assert all(r.git_dirty is True for r in runs)
        diff_paths = {r.dirty_diff_path for r in runs}
        assert len(diff_paths) == 1
        diff_file = Path(next(iter(diff_paths)))
        assert diff_file.exists()
        assert "drift marker" in diff_file.read_text()


def test_execute_correctness_archives_artifact(
    fake_project_git: Path, tmp_path: Path
) -> None:
    """A correctness-tier run produces a byte artifact that the runner archives
    content-addressed under artifacts_root, and the hash is written to the row."""
    from benchstone.runner import plan_evaluation

    manifest = load_manifest(fake_project_git)
    bench = manifest.benchmark("fake_correctness")
    gstate = git_state(fake_project_git)
    plan = plan_evaluation(bench, gstate, allow_dirty=False, meta_seed=7)

    artifacts_root = tmp_path / "artifacts"
    with Store(tmp_path / "store.db") as store:
        ids = execute(
            project=manifest.project,
            project_path=fake_project_git,
            benchmark=bench,
            plan=plan,
            store=store,
            host="testhost",
            logs_root=tmp_path / "logs",
            artifacts_root=artifacts_root,
        )
        assert len(ids) == 1
        run = store.get_run(ids[0])
        assert run is not None
        assert run.status == "ok"
        assert run.artifact_hash is not None
        assert run.artifact_hash.startswith("sha256:")
        assert run.artifact_path is not None
        archived = Path(run.artifact_path)
        assert archived.exists()
        assert archived.read_bytes() == b"fake correctness artifact v1\n"
        # Archive path is content-addressed.
        assert archived.name == run.artifact_hash.split(":", 1)[1] + ".bin"
        # And lives under the project/benchmark subtree of artifacts_root.
        assert artifacts_root in archived.parents


def test_execute_correctness_missing_artifact_records_error(
    fake_project_git: Path, tmp_path: Path
) -> None:
    """Break the entry point so it does not write the artifact file; the runner
    must record status='error' with a helpful message."""
    from benchstone.runner import plan_evaluation

    bench_py = fake_project_git / "bench" / "benchmarks.py"
    patched = bench_py.read_text().replace(
        'Path(artifact_path).write_bytes(content)',
        'pass  # deliberately fail to write artifact',
    )
    bench_py.write_text(patched)

    manifest = load_manifest(fake_project_git)
    bench = manifest.benchmark("fake_correctness")
    gstate = git_state(fake_project_git)
    plan = plan_evaluation(bench, gstate, allow_dirty=True, meta_seed=9)

    with Store(tmp_path / "store.db") as store:
        ids = execute(
            project=manifest.project,
            project_path=fake_project_git,
            benchmark=bench,
            plan=plan,
            store=store,
            host="testhost",
            logs_root=tmp_path / "logs",
            artifacts_root=tmp_path / "artifacts",
        )
        run = store.get_run(ids[0])
        assert run is not None
        assert run.status == "error"
        assert run.artifact_hash is None
        meta = run.project_metadata or {}
        assert "artifact" in (meta.get("error") or "").lower()


def test_execute_captures_subprocess_failure(
    fake_project_git: Path, tmp_path: Path
) -> None:
    """A benchmark whose entry point does not exist yields status='error' rows."""
    mf_path = fake_project_git / "bench" / "manifest.toml"
    content = mf_path.read_text().replace(
        'entry_point = "fake_quality"', 'entry_point = "nonexistent_entry"'
    )
    mf_path.write_text(content)
    manifest = load_manifest(fake_project_git)
    bench = manifest.benchmark("fake_quality")
    plan = plan_baseline(bench, git_state(fake_project_git), allow_dirty=True)
    with Store(tmp_path / "store.db") as store:
        execute(
            project=manifest.project, project_path=fake_project_git,
            benchmark=bench, plan=plan, store=store,
            host="testhost", logs_root=tmp_path / "logs",
        )
        runs = store.fetch_runs("FakeProject", "fake_quality")
        assert len(runs) == 3
        assert all(r.status == "error" for r in runs)
