from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

from . import jobs, paths
from ._timefmt import utc_now
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
    jobs.job_dir(job_id).mkdir(parents=True, exist_ok=True)
    worker_log_path = jobs.worker_log_file(job_id)

    spec = {
        "job_id": job_id,
        "project_name": project.name,
        "project_path": str(project_path),
        "benchmark_name": benchmark.name,
        "plan": plan.to_dict(),
        "host": host,
        "benchstone_home": str(paths.benchstone_home()),
        "set_baseline": set_baseline,
        "baseline_notes": baseline_notes,
    }
    spec_path = jobs.spec_file(job_id)
    # Spec carries plan.git_state.diff which may contain pre-commit secrets;
    # restrict to the owner.
    jobs._write_private(spec_path, json.dumps(spec, indent=2, sort_keys=True))

    started_at = utc_now()
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
