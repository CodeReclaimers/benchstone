from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import jobs, paths
from .manifest import Benchmark, Project
from .runner import RunPlan


def spawn(
    project: Project,
    project_path: Path,
    benchmark: Benchmark,
    plan: RunPlan,
    host: str,
    set_baseline: bool = False,
    baseline_notes: str | None = None,
) -> jobs.Job:
    """Fork-and-detach a worker process that will execute the plan and update the job file.

    Returns the Job descriptor with ``status="running"`` and the worker's PID.
    The parent is decoupled from the worker via ``start_new_session=True``, so
    ``bench run --background`` can exit while the worker keeps going.
    """
    paths.jobs_dir().mkdir(parents=True, exist_ok=True)
    job_id = jobs.new_job_id()
    worker_log_path = paths.jobs_dir() / f"{job_id}.worker.log"

    spec = {
        "job_id": job_id,
        "project_name": project.name,
        "project_path": str(project_path),
        "benchmark_name": benchmark.name,
        "plan": {
            "seeds": list(plan.seeds),
            "meta_seed": plan.meta_seed,
            "git_sha": plan.git_state.sha,
            "git_dirty": plan.git_state.dirty,
            "git_diff": plan.git_state.diff,
            "allow_dirty": plan.allow_dirty,
        },
        "host": host,
        "benchstone_home": str(paths.benchstone_home()),
        "set_baseline": set_baseline,
        "baseline_notes": baseline_notes,
    }
    spec_path = jobs.spec_file(job_id)
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True))

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    initial = jobs.Job(
        job_id=job_id,
        pid=0,
        project=project.name,
        benchmark=benchmark.name,
        threads=benchmark.threads,
        gpu=benchmark.gpu,
        status="pending",
        started_at=started_at,
        ended_at=None,
        host=host,
        worker_log_path=str(worker_log_path),
        inserted_run_ids=[],
        message=None,
    )
    jobs.save(initial)

    log_handle = open(worker_log_path, "wb")
    try:
        popen = subprocess.Popen(
            [sys.executable, "-m", "benchstone._background_worker",
             "--spec", str(spec_path)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # Parent doesn't need to keep the handle; child inherits the fd.
        log_handle.close()

    running = dataclasses.replace(initial, pid=popen.pid, status="running")
    jobs.save(running)
    return running
