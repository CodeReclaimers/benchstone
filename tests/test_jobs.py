from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from benchstone import jobs


def _job(**overrides) -> jobs.Job:
    base = dict(
        job_id="20260419T140000-abc123",
        pid=os.getpid(),
        project="FakeProject",
        benchmark="fake_quality",
        threads=1,
        gpu="none",
        status="running",
        started_at="2026-04-19T14:00:00Z",
        ended_at=None,
        host="testhost",
        worker_log_path="/tmp/fake.log",
        inserted_run_ids=[],
        message=None,
    )
    base.update(overrides)
    return jobs.Job(**base)


def test_save_and_load_roundtrip(isolated_home: Path) -> None:
    j = _job()
    jobs.save(j)
    back = jobs.load(j.job_id)
    assert back == j


def test_list_all_returns_sorted(isolated_home: Path) -> None:
    j1 = _job(job_id="20260419T140000-aaa")
    j2 = _job(job_id="20260419T140100-bbb")
    jobs.save(j2)
    jobs.save(j1)
    listed = jobs.list_all()
    assert [x.job_id for x in listed] == [j1.job_id, j2.job_id]


def test_list_all_ignores_spec_and_tmp_files(isolated_home: Path) -> None:
    j = _job()
    jobs.save(j)
    # Drop sibling files that must not be picked up.
    (isolated_home / "jobs" / f"{j.job_id}.spec.json").write_text("{}")
    (isolated_home / "jobs" / "orphan.json.tmp").write_text("{}")
    listed = jobs.list_all()
    assert [x.job_id for x in listed] == [j.job_id]


def test_is_pid_alive_self() -> None:
    assert jobs.is_pid_alive(os.getpid())


def test_is_pid_alive_zero_is_false() -> None:
    assert not jobs.is_pid_alive(0)


def test_is_pid_alive_nonexistent() -> None:
    # 999999 is highly unlikely to be a live process; os.kill raises ProcessLookupError.
    assert not jobs.is_pid_alive(999999)


def test_refresh_staleness_marks_dead_running_jobs(isolated_home: Path) -> None:
    alive = _job(job_id="alive", pid=os.getpid(), status="running")
    dead = _job(job_id="dead", pid=999999, status="running")
    done = _job(job_id="done", pid=999999, status="done", ended_at="2026-04-19T14:01:00Z")
    for j in (alive, dead, done):
        jobs.save(j)

    refreshed = jobs.refresh_staleness(jobs.list_all())
    by_id = {j.job_id: j for j in refreshed}
    assert by_id["alive"].status == "running"
    assert by_id["dead"].status == "stale"
    assert by_id["dead"].message is not None
    assert by_id["done"].status == "done"  # terminal unchanged

    # Persistence: a fresh read should see the stale transition.
    assert jobs.load("dead").status == "stale"
