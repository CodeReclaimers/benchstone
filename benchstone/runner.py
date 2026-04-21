from __future__ import annotations

import hashlib
import os
import random
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .manifest import Benchmark, Project
from .protocol import InvocationConfig, ProjectResult, ProtocolError
from .provenance import GitState
from .store import Store


class RunnerError(Exception):
    """Raised when a benchmark cannot be dispatched (dirty tree, bad template, etc.)."""


@dataclass(frozen=True)
class RunPlan:
    """The parameters of one evaluation: seed set + provenance + dirty-tree gate.

    A baseline plan uses the manifest's explicit `baseline_seeds` and has
    ``meta_seed=None``. An evaluation plan derives its seeds from a single
    meta-seed, so the whole N-rep run is reproducible from that integer.
    """
    seeds: tuple[int, ...]
    meta_seed: int | None
    git_state: GitState
    allow_dirty: bool = False


def plan_baseline(
    benchmark: Benchmark,
    git_state: GitState,
    allow_dirty: bool,
    repetitions: int | None = None,
) -> RunPlan:
    if not benchmark.baseline_seeds:
        raise RunnerError(
            f"benchmark {benchmark.name!r} has no baseline_seeds in the manifest"
        )
    seeds: tuple[int, ...] = tuple(benchmark.baseline_seeds)
    if repetitions is not None:
        if repetitions < 1:
            raise RunnerError("--repetitions must be >= 1")
        if repetitions > len(seeds):
            raise RunnerError(
                f"--repetitions={repetitions} exceeds baseline_seeds length "
                f"({len(seeds)}); extend baseline_seeds in the manifest or lower --repetitions"
            )
        seeds = seeds[:repetitions]
    return RunPlan(
        seeds=seeds,
        meta_seed=None,
        git_state=git_state,
        allow_dirty=allow_dirty,
    )


def plan_evaluation(
    benchmark: Benchmark,
    git_state: GitState,
    allow_dirty: bool,
    meta_seed: int | None = None,
    repetitions: int | None = None,
) -> RunPlan:
    n = repetitions if repetitions is not None else benchmark.repetitions
    if n < 1:
        raise RunnerError("--repetitions must be >= 1")
    if meta_seed is None:
        meta_seed = int.from_bytes(os.urandom(8), "big") & 0x7FFFFFFFFFFFFFFF
    rng = random.Random(meta_seed)
    seeds = tuple(rng.randrange(0, 2**31) for _ in range(n))
    return RunPlan(
        seeds=seeds,
        meta_seed=meta_seed,
        git_state=git_state,
        allow_dirty=allow_dirty,
    )


def execute(
    project: Project,
    project_path: Path,
    benchmark: Benchmark,
    plan: RunPlan,
    store: Store,
    host: str,
    logs_root: Path,
    artifacts_root: Path | None = None,
) -> list[int]:
    """Run the benchmark per `plan` and persist one row per repetition.

    Returns the list of inserted run IDs in rep order. Errors encountered while
    running a single rep (non-zero exit, missing output, schema violation) are
    captured as ``status="error"`` rows so the store remains a faithful log.
    """
    if plan.git_state.dirty and not plan.allow_dirty:
        raise RunnerError(
            "refusing to run against a dirty git tree; pass --allow-dirty to override"
        )

    diff_path: str | None = None
    if plan.git_state.dirty and plan.git_state.diff:
        diff_path = _persist_diff(
            logs_root, project.name, benchmark.name, plan.git_state.diff
        )

    inserted: list[int] = []
    for rep_idx, seed in enumerate(plan.seeds):
        rid = _run_one_rep(
            project=project,
            project_path=project_path,
            benchmark=benchmark,
            plan=plan,
            seed=seed,
            rep_idx=rep_idx,
            rep_total=len(plan.seeds),
            store=store,
            host=host,
            logs_root=logs_root,
            artifacts_root=artifacts_root,
            diff_path=diff_path,
        )
        inserted.append(rid)
    return inserted


def _run_one_rep(
    project: Project,
    project_path: Path,
    benchmark: Benchmark,
    plan: RunPlan,
    seed: int,
    rep_idx: int,
    rep_total: int,
    store: Store,
    host: str,
    logs_root: Path,
    artifacts_root: Path | None,
    diff_path: str | None,
) -> int:
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_tag = now.strftime("%Y%m%dT%H%M%SZ")

    log_dir = logs_root / project.name / benchmark.name
    log_dir.mkdir(parents=True, exist_ok=True)
    stderr_log_path = log_dir / f"{ts_tag}-rep{rep_idx:02d}.stderr"

    base: dict[str, object] = dict(
        project=project.name,
        benchmark=benchmark.name,
        git_sha=plan.git_state.sha,
        git_dirty=plan.git_state.dirty,
        dirty_diff_path=diff_path,
        timestamp=timestamp,
        harness_version=__version__,
        host=host,
        seed=seed,
        meta_seed=plan.meta_seed,
        repetition_index=rep_idx,
        stderr_log_path=str(stderr_log_path),
    )

    is_correctness = benchmark.tier == "correctness"
    artifact_hash: str | None = None
    archived_artifact: str | None = None

    try:
        with tempfile.TemporaryDirectory(prefix="benchstone-") as tmp:
            tmp_dir = Path(tmp)
            config_path = tmp_dir / "config.json"
            output_path = tmp_dir / "result.json"
            artifact_path = (tmp_dir / "artifact.bin") if is_correctness else None
            corpus_path = (
                str((project_path / benchmark.corpus_path).resolve())
                if benchmark.corpus_path else ""
            )
            InvocationConfig(
                benchmark=benchmark.name,
                seed=seed,
                corpus_path=corpus_path,
                repetition_index=rep_idx,
                repetition_total=rep_total,
                artifact_path=str(artifact_path) if artifact_path else None,
            ).write(config_path)

            cmd = _format_invocation(
                project.invocation,
                entry_point=benchmark.entry_point,
                config_path=str(config_path),
                output_path=str(output_path),
            )

            start = time.monotonic()
            with open(stderr_log_path, "wb") as errfile:
                completed = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(project_path),
                    stdout=subprocess.DEVNULL,
                    stderr=errfile,
                )
            elapsed = time.monotonic() - start

            if completed.returncode != 0:
                return store.insert_run(
                    **base,
                    status="error",
                    wall_clock_seconds=elapsed,
                    project_metadata={"exit_code": completed.returncode},
                )

            result = ProjectResult.read(output_path)

            if is_correctness:
                if artifact_path is None or not artifact_path.exists():
                    return store.insert_run(
                        **base,
                        status="error",
                        wall_clock_seconds=elapsed,
                        project_metadata={
                            "error": "correctness benchmark did not produce an artifact",
                        },
                    )
                artifact_hash, archived_artifact = _archive_artifact(
                    artifact_path, project.name, benchmark.name, artifacts_root
                )
    except (ProtocolError, FileNotFoundError, OSError) as exc:
        return store.insert_run(
            **base,
            status="error",
            wall_clock_seconds=0.0,
            project_metadata={"error": f"{type(exc).__name__}: {exc}"},
        )

    return store.insert_run(
        **base,
        status=result.status,
        metric=result.metric,
        metric_components=result.metric_components,
        wall_clock_seconds=(
            result.wall_clock_seconds if result.wall_clock_seconds is not None else elapsed
        ),
        project_metadata=result.metadata or None,
        artifact_hash=artifact_hash,
        artifact_path=archived_artifact,
    )


def _format_invocation(
    template: str, *, entry_point: str, config_path: str, output_path: str
) -> str:
    try:
        return template.format(
            entry_point=entry_point,
            config_path=config_path,
            output_path=output_path,
        )
    except KeyError as exc:
        raise RunnerError(
            f"manifest invocation template references unknown placeholder: {exc}"
        ) from exc


def _persist_diff(
    logs_root: Path, project_name: str, benchmark_name: str, diff: str
) -> str:
    log_dir = logs_root / project_name / benchmark_name
    log_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = log_dir / f"{ts_tag}.diff"
    path.write_text(diff)
    return str(path)


def _archive_artifact(
    src: Path,
    project_name: str,
    benchmark_name: str,
    artifacts_root: Path | None,
) -> tuple[str, str]:
    """Content-address the run's artifact under ``artifacts_root`` and return
    ``(sha256:<hex>, absolute_archived_path)``. Existing entries at the same
    hash are left in place — correctness artifacts are deduplicated across
    runs that produce identical bytes."""
    if artifacts_root is None:
        raise RunnerError(
            "correctness benchmark produced an artifact but no artifacts_root "
            "was configured for this run"
        )
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    dest_dir = artifacts_root / project_name / benchmark_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{digest}.bin"
    if not dest.exists():
        shutil.copy2(src, dest)
    return f"sha256:{digest}", str(dest)
