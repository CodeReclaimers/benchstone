from __future__ import annotations

import dataclasses
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from ._timefmt import utc_now

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
    from ._timefmt import utc_stamp_tag
    # Strip the trailing 'Z' so the id keeps its original shape.
    return f"{utc_stamp_tag()[:-1]}-{secrets.token_hex(3)}"


def job_dir(job_id: str) -> Path:
    """Per-job subdirectory holding job.json, spec.json, and worker.log.

    Each job owns its own directory so list_all can enumerate jobs by looking
    at top-level entries in $BENCHSTONE_HOME/jobs/ without needing to filter
    filename suffixes.
    """
    return paths.jobs_dir() / job_id


def job_file(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def spec_file(job_id: str) -> Path:
    return job_dir(job_id) / "spec.json"


def worker_log_file(job_id: str) -> Path:
    return job_dir(job_id) / "worker.log"


def save(job: Job) -> None:
    """Atomically persist the job descriptor to $BENCHSTONE_HOME/jobs/<id>/job.json."""
    job_dir(job.job_id).mkdir(parents=True, exist_ok=True)
    final = job_file(job.job_id)
    tmp = final.with_suffix(".json.tmp")
    _write_private(tmp, json.dumps(asdict(job), indent=2, sort_keys=True))
    tmp.replace(final)


def _write_private(path: Path, content: str) -> None:
    """Write `content` to `path` with 0o600 permissions (owner-only)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)


def load(job_id: str) -> Job:
    data = json.loads(job_file(job_id).read_text())
    return _job_from_dict(data)


def list_all() -> list[Job]:
    d = paths.jobs_dir()
    if not d.exists():
        return []
    out: list[Job] = []
    for entry in sorted(d.iterdir()):
        if not entry.is_dir():
            continue
        jf = entry / "job.json"
        if not jf.exists():
            continue
        out.append(_job_from_dict(json.loads(jf.read_text())))
    return out


def discard_spec(job_id: str) -> None:
    """Best-effort deletion of the spec file once the worker is done with it.

    The spec file carries plan data (including any git diff bytes) and is only
    needed while the worker is running. Removing it on terminal status keeps
    $BENCHSTONE_HOME/jobs/ from growing without bound.
    """
    try:
        spec_file(job_id).unlink()
    except FileNotFoundError:
        pass


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
                ended_at=utc_now(),
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
