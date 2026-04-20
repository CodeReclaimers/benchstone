from __future__ import annotations

import dataclasses
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths

JobStatus = str  # "pending" | "running" | "done" | "failed" | "stale"

ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "running"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "stale"})


@dataclass(frozen=True)
class Job:
    """A background dispatch descriptor, persisted as a per-job JSON file.

    ``inserted_run_ids`` is populated by the worker on completion. ``message``
    carries an exception summary when ``status=="failed"``.
    """
    job_id: str
    pid: int
    project: str
    benchmark: str
    threads: int
    gpu: str
    status: JobStatus
    started_at: str
    ended_at: str | None
    host: str
    worker_log_path: str
    inserted_run_ids: list[int] = field(default_factory=list)
    message: str | None = None


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def job_file(job_id: str) -> Path:
    return paths.jobs_dir() / f"{job_id}.json"


def spec_file(job_id: str) -> Path:
    return paths.jobs_dir() / f"{job_id}.spec.json"


def save(job: Job) -> None:
    """Atomically persist the job descriptor to $BENCHSTONE_HOME/jobs/<id>.json."""
    paths.jobs_dir().mkdir(parents=True, exist_ok=True)
    final = job_file(job.job_id)
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(job), indent=2, sort_keys=True))
    tmp.replace(final)


def load(job_id: str) -> Job:
    data = json.loads(job_file(job_id).read_text())
    return _job_from_dict(data)


def list_all() -> list[Job]:
    d = paths.jobs_dir()
    if not d.exists():
        return []
    return [
        _job_from_dict(json.loads(f.read_text()))
        for f in sorted(d.glob("*.json"))
        if not f.name.endswith(".spec.json") and not f.name.endswith(".tmp")
    ]


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it.
        return True


def refresh_staleness(jobs: list[Job]) -> list[Job]:
    """Mark running jobs whose PID is gone as 'stale'. Persists the transition."""
    out: list[Job] = []
    for j in jobs:
        if j.status == "running" and not is_pid_alive(j.pid):
            j = dataclasses.replace(
                j,
                status="stale",
                ended_at=_utc_now(),
                message="worker PID not alive when status refreshed",
            )
            save(j)
        out.append(j)
    return out


def _job_from_dict(data: dict) -> Job:
    return Job(
        job_id=data["job_id"],
        pid=int(data["pid"]),
        project=data["project"],
        benchmark=data["benchmark"],
        threads=int(data["threads"]),
        gpu=data["gpu"],
        status=data["status"],
        started_at=data["started_at"],
        ended_at=data.get("ended_at"),
        host=data["host"],
        worker_log_path=data["worker_log_path"],
        inserted_run_ids=list(data.get("inserted_run_ids", [])),
        message=data.get("message"),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
