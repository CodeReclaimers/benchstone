from __future__ import annotations

import time
from pathlib import Path

import pytest

from benchstone import background, jobs, paths
from benchstone.manifest import load as load_manifest
from benchstone.provenance import git_state
from benchstone.runner import plan_baseline
from benchstone.store import Store


def _wait_for_terminal(job_id: str, timeout_s: float = 10.0) -> jobs.Job:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        j = jobs.load(job_id)
        if j.status in jobs.TERMINAL_STATUSES:
            return j
        time.sleep(0.05)
    pytest.fail(
        f"background job {job_id} did not reach a terminal status within {timeout_s}s; "
        f"last status={jobs.load(job_id).status}"
    )


def test_spawn_runs_baseline_to_completion(
    fake_project_git: Path, isolated_home: Path
) -> None:
    manifest = load_manifest(fake_project_git)
    benchmark = manifest.benchmark("fake_quality")
    plan = plan_baseline(benchmark, git_state(fake_project_git), allow_dirty=False)

    job = background.spawn(
        project=manifest.project,
        project_path=fake_project_git,
        benchmark=benchmark,
        plan=plan,
        host="testhost",
    )
    assert job.status == "running"
    assert job.pid > 0

    final = _wait_for_terminal(job.job_id)
    assert final.status == "done", f"{final.status=} message={final.message!r}"
    assert len(final.inserted_run_ids) == 3

    with Store(paths.store_path()) as store:
        runs = store.fetch_runs("FakeProject", "fake_quality")
        assert len(runs) == 3
        assert all(r.status == "ok" for r in runs)
        assert all(r.meta_seed is None for r in runs)


def test_spawn_with_set_baseline_updates_pointer(
    fake_project_git: Path, isolated_home: Path
) -> None:
    manifest = load_manifest(fake_project_git)
    benchmark = manifest.benchmark("fake_quality")
    plan = plan_baseline(benchmark, git_state(fake_project_git), allow_dirty=False)

    job = background.spawn(
        project=manifest.project,
        project_path=fake_project_git,
        benchmark=benchmark,
        plan=plan,
        host="testhost",
        set_baseline=True,
        baseline_notes="established in background",
    )
    final = _wait_for_terminal(job.job_id)
    assert final.status == "done"

    with Store(paths.store_path()) as store:
        base = store.get_baseline("FakeProject", "fake_quality")
    assert base is not None
    assert base.notes == "established in background"
    assert base.git_sha == git_state(fake_project_git).sha


def test_worker_marks_job_failed_on_missing_manifest(
    isolated_home: Path,
) -> None:
    """Directly invoke the worker entry point with a spec pointing at a missing
    project path — no subprocess, no race. The worker must persist
    status='failed' with a diagnostic message."""
    import json

    from benchstone._background_worker import main as worker_main

    job_id = jobs.new_job_id()
    paths.jobs_dir().mkdir(parents=True, exist_ok=True)
    initial = jobs.Job(
        job_id=job_id, pid=0, project="X", benchmark="Y",
        threads=1, gpu="none", status="running",
        started_at="2026-04-19T14:00:00Z", ended_at=None,
        host="h", worker_log_path="/tmp/x.log",
    )
    jobs.save(initial)

    spec = {
        "job_id": job_id,
        "project_name": "X",
        "project_path": "/nonexistent/benchstone-fake-path",
        "benchmark_name": "Y",
        "plan": {
            "seeds": [1], "meta_seed": None,
            "git_sha": "deadbeef", "git_dirty": False, "git_diff": "",
            "allow_dirty": False,
        },
        "host": "h",
        "benchstone_home": str(isolated_home),
        "set_baseline": False,
        "baseline_notes": None,
    }
    spec_path = jobs.spec_file(job_id)
    spec_path.write_text(json.dumps(spec))

    rc = worker_main(["--spec", str(spec_path)])
    assert rc == 1

    j = jobs.load(job_id)
    assert j.status == "failed"
    assert j.message
    assert j.ended_at is not None
