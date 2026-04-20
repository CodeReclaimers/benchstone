from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from .jobs import ACTIVE_STATUSES, Job
from .manifest import Benchmark


class SchedulerError(Exception):
    """Raised when admitting a benchmark would overcommit host capacity."""


@dataclass(frozen=True)
class HostCapacity:
    threads: int
    gpu_count: int

    @classmethod
    def from_env(cls) -> "HostCapacity":
        """Read $BENCHSTONE_MAX_THREADS (default: os.cpu_count() or 1) and
        $BENCHSTONE_GPU_COUNT (default: 0). Keep GPU default conservative so
        direct-GPU benchmarks fail loudly on hosts that don't advertise one."""
        raw_threads = os.environ.get("BENCHSTONE_MAX_THREADS")
        threads = int(raw_threads) if raw_threads else (os.cpu_count() or 1)
        gpu_count = int(os.environ.get("BENCHSTONE_GPU_COUNT", "0"))
        return cls(threads=threads, gpu_count=gpu_count)


def admit(
    benchmark: Benchmark,
    active_jobs: Iterable[Job],
    capacity: HostCapacity,
) -> None:
    """Raise SchedulerError if dispatching `benchmark` would overcommit.

    - direct-GPU benchmarks cannot run concurrently on hosts with gpu_count=1,
      and are refused on gpu_count=0.
    - ollama-GPU benchmarks currently have no admission constraint: the shared
      Ollama instance serializes internally. Revisit if that assumption breaks.
    - thread budget is the sum of manifest-declared threads across active jobs;
      dispatching a benchmark that would push the sum above capacity.threads is
      refused.
    """
    active = [j for j in active_jobs if j.status in ACTIVE_STATUSES]

    if benchmark.gpu == "direct":
        if capacity.gpu_count < 1:
            raise SchedulerError(
                f"benchmark {benchmark.name!r} requires direct GPU but "
                f"host has gpu_count={capacity.gpu_count}"
            )
        active_direct = sum(1 for j in active if j.gpu == "direct")
        if active_direct >= capacity.gpu_count:
            raise SchedulerError(
                f"benchmark {benchmark.name!r} requires direct GPU but all "
                f"{capacity.gpu_count} are in use by active job(s)"
            )

    used_threads = sum(j.threads for j in active)
    if used_threads + benchmark.threads > capacity.threads:
        raise SchedulerError(
            f"thread budget exceeded: benchmark {benchmark.name!r} requests "
            f"{benchmark.threads} thread(s), {used_threads} already in use, "
            f"host capacity is {capacity.threads}"
        )
