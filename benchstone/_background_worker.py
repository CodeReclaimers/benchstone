"""Worker entry point for background benchmark dispatch.

Invoked as ``python -m benchstone._background_worker --spec <file>`` by
``benchstone.background.spawn``. Re-opens its own Store, runs the plan to
completion, and rewrites the job descriptor with the terminal status.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import traceback
from pathlib import Path

from . import jobs, paths
from ._timefmt import utc_now
from .manifest import load as load_manifest
from .runner import RunPlan, execute
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text())
    job_id = spec["job_id"]

    # Ensure subsequent paths resolve against the same $BENCHSTONE_HOME the parent used,
    # even if the env var was unset in the detached session.
    os.environ["BENCHSTONE_HOME"] = spec["benchstone_home"]

    try:
        project_path = Path(spec["project_path"])
        manifest = load_manifest(project_path)
        benchmark = manifest.benchmark(spec["benchmark_name"])
        plan = RunPlan.from_dict(spec["plan"])

        with Store(paths.store_path()) as store:
            with store.transaction():
                ids = execute(
                    project=manifest.project,
                    project_path=project_path,
                    benchmark=benchmark,
                    plan=plan,
                    store=store,
                    host=spec["host"],
                    logs_root=paths.logs_dir(),
                    artifacts_root=paths.artifacts_dir(),
                )
                if spec.get("set_baseline"):
                    store.set_baseline(
                        manifest.project.name,
                        benchmark.name,
                        plan.git_state.sha,
                        utc_now(),
                        notes=spec.get("baseline_notes"),
                        meta_seed=plan.meta_seed,
                    )
        _finalize(job_id, status="done", inserted_run_ids=ids)
        return 0
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        _finalize(
            job_id,
            status="failed",
            inserted_run_ids=[],
            message=f"{type(exc).__name__}: {exc}",
        )
        return 1


def _finalize(
    job_id: str,
    *,
    status: str,
    inserted_run_ids: list[int],
    message: str | None = None,
) -> None:
    ended_at = utc_now()
    job = jobs.load(job_id)
    jobs.save(dataclasses.replace(
        job,
        status=status,
        ended_at=ended_at,
        inserted_run_ids=list(inserted_run_ids),
        message=message,
    ))
    # Spec is only needed while the worker is running; drop it now so
    # $BENCHSTONE_HOME/jobs/ doesn't accumulate diff-bearing artifacts.
    jobs.discard_spec(job_id)


if __name__ == "__main__":
    sys.exit(main())
