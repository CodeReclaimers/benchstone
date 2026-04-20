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
from datetime import datetime, timezone
from pathlib import Path

from . import jobs, paths
from .manifest import load as load_manifest
from .provenance import GitState
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
        plan_data = spec["plan"]
        plan = RunPlan(
            seeds=tuple(int(s) for s in plan_data["seeds"]),
            meta_seed=plan_data["meta_seed"],
            git_state=GitState(
                sha=plan_data["git_sha"],
                dirty=bool(plan_data["git_dirty"]),
                diff=plan_data.get("git_diff", ""),
            ),
            allow_dirty=bool(plan_data["allow_dirty"]),
        )

        with Store(paths.store_path()) as store:
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
                established_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                store.set_baseline(
                    manifest.project.name,
                    benchmark.name,
                    plan.git_state.sha,
                    established_at,
                    notes=spec.get("baseline_notes"),
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
    ended_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    job = jobs.load(job_id)
    jobs.save(dataclasses.replace(
        job,
        status=status,
        ended_at=ended_at,
        inserted_run_ids=list(inserted_run_ids),
        message=message,
    ))


if __name__ == "__main__":
    sys.exit(main())
