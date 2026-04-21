from __future__ import annotations

from typing import Any

import pytest

from benchstone.jobs import Job
from benchstone.manifest import Benchmark
from benchstone.scheduler import HostCapacity, SchedulerError, admit


def _bench(**overrides: Any) -> Benchmark:
    base: dict[str, Any] = dict(
        name="B", entry_point="B", tier="quality",
        deterministic=False, metric_direction="minimize",
        expected_runtime_seconds=None, threads=1, gpu="none",
        background_required=False, repetitions=3, baseline_seeds=(1, 2, 3),
        promotion_sigma=2.0, corpus_path=None, corpus_hash=None,
        corpus_type=None, reference_policy=None, gate_policy="sigma",
    )
    base.update(overrides)
    return Benchmark(**base)


def _job(**overrides: Any) -> Job:
    base: dict[str, Any] = dict(
        job_id="j", pid=1, project="P", benchmark="B",
        threads=1, gpu="none", status="running",
        started_at="t", ended_at=None, host="h",
        worker_log_path="/tmp/x",
    )
    base.update(overrides)
    return Job(**base)


def test_admit_empty_host() -> None:
    admit(_bench(), [], HostCapacity(threads=8, gpu_count=0))


def test_admit_within_thread_budget() -> None:
    active = [_job(threads=4)]
    admit(_bench(threads=4), active, HostCapacity(threads=8, gpu_count=0))


def test_admit_refuses_thread_overcommit() -> None:
    active = [_job(threads=5)]
    with pytest.raises(SchedulerError, match="thread budget"):
        admit(_bench(threads=4), active, HostCapacity(threads=8, gpu_count=0))


def test_admit_refuses_direct_gpu_on_zero_gpu_host() -> None:
    with pytest.raises(SchedulerError, match="direct GPU"):
        admit(_bench(gpu="direct"), [], HostCapacity(threads=8, gpu_count=0))


def test_admit_refuses_second_direct_gpu() -> None:
    active = [_job(gpu="direct", threads=1)]
    with pytest.raises(SchedulerError, match="GPU"):
        admit(_bench(gpu="direct"), active, HostCapacity(threads=8, gpu_count=1))


def test_admit_allows_direct_gpu_when_none_active() -> None:
    admit(_bench(gpu="direct"), [], HostCapacity(threads=8, gpu_count=1))


def test_admit_ollama_gpu_does_not_hit_gpu_check() -> None:
    # An ollama-GPU benchmark should admit on a host with gpu_count=0 since the
    # shared Ollama process is handled outside the scheduler's budget.
    admit(_bench(gpu="ollama"), [], HostCapacity(threads=8, gpu_count=0))


def test_admit_ignores_terminal_jobs() -> None:
    terminal = [_job(threads=8, status="done"), _job(threads=8, status="failed")]
    admit(_bench(threads=4), terminal, HostCapacity(threads=8, gpu_count=0))


def test_host_capacity_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCHSTONE_MAX_THREADS", "16")
    monkeypatch.setenv("BENCHSTONE_GPU_COUNT", "2")
    cap = HostCapacity.from_env()
    assert cap.threads == 16 and cap.gpu_count == 2


def test_host_capacity_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BENCHSTONE_MAX_THREADS", raising=False)
    monkeypatch.delenv("BENCHSTONE_GPU_COUNT", raising=False)
    cap = HostCapacity.from_env()
    assert cap.threads >= 1
    assert cap.gpu_count == 0
