"""Public Python API.

Entry points for consumer scripts (autoloops, CI steps, custom dashboards)
that previously had to shell out to ``bench ...`` and parse stdout. The CLI
is built on the same primitives.

The read-only side (``evaluate``, ``history``, ``compute_verdict``) was the
first surface; the write-side (``run``, ``establish_baseline``, ``promote``,
``freeze_reference``, ``register``) was added so an autoresearch loop can
drive the harness without subprocessing.

Errors related to *configuration* (project not registered, manifest invalid,
benchmark name unknown, no git state) propagate as their native exceptions
(``RegistryError``, ``ManifestError``, ``KeyError``, ``ProvenanceError``).
*Gate outcomes* — including "no baseline yet" or "not enough runs" — are
returned as ``Verdict`` objects with the corresponding ``kind``.

The write-side API runs benchmarks in the foreground (synchronously); for
background dispatch with PID tracking, use the ``bench run --background`` CLI
path. Admission control (``benchstone.scheduler.admit``) still applies so
API callers don't bypass thread/GPU budgets the CLI would have enforced.
"""
from __future__ import annotations

import socket
from pathlib import Path

from . import paths, references
from ._timefmt import utc_now
from .gate import Verdict, evaluate as gate_evaluate
from .jobs import ACTIVE_STATUSES, list_all as list_jobs, refresh_staleness
from .manifest import Benchmark, Project, load as load_manifest
from .provenance import git_state
from .references import Reference
from .registry import RegisteredProject, Registry
from .runner import (
    RunPlan,
    execute as runner_execute,
    plan_baseline,
    plan_evaluation,
)
from .scheduler import HostCapacity, admit
from .store import Run, Store

__all__ = [
    "Reference",
    "RegisteredProject",
    "Run",
    "Verdict",
    "compute_verdict",
    "establish_baseline",
    "evaluate",
    "freeze_reference",
    "history",
    "promote",
    "register",
    "run",
]


def evaluate(project_name: str, benchmark_name: str) -> Verdict:
    """Compute the verdict for ``benchmark_name`` of ``project_name`` at the
    current git SHA. Read-only — does not run the benchmark.

    Equivalent to ``bench evaluate PROJECT BENCHMARK`` but returns a typed
    ``Verdict`` instead of a process exit code. Inspect ``verdict.kind`` for
    the categorical result (``PROMOTE`` / ``REJECT`` / ``PASS`` / ``FAIL`` /
    ``NEEDS_MORE_DATA`` / ``NO_BASELINE`` / ``NO_REFERENCE``).
    """
    project, benchmark, project_path = _resolve(project_name, benchmark_name)
    sha = git_state(project_path).sha
    with Store(paths.store_path()) as store:
        return compute_verdict(project, benchmark, sha, store)


def history(
    project_name: str,
    benchmark_name: str,
    *,
    git_sha_prefix: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[Run]:
    """Return the run timeline for ``benchmark_name`` of ``project_name``.

    Filters mirror ``bench history``: ``git_sha_prefix`` matches any row whose
    git_sha starts with the prefix; ``since`` is an ISO timestamp lower bound;
    ``limit`` keeps only the most recent N rows after other filters apply.
    """
    # Validate the project is registered; resolve raises RegistryError otherwise.
    Registry().resolve(project_name)
    with Store(paths.store_path()) as store:
        return store.fetch_runs(
            project_name, benchmark_name,
            git_sha_prefix=git_sha_prefix, since=since, limit=limit,
        )


def compute_verdict(
    project: Project,
    benchmark: Benchmark,
    current_sha: str,
    store: Store,
) -> Verdict:
    """Evaluate the gate for ``benchmark`` at ``current_sha`` against the
    recorded baseline. Lower-level entry point used by both the CLI and
    ``api.evaluate``; takes an open Store so callers in a transaction don't
    re-open the database.
    """
    if benchmark.tier == "correctness":
        reference = references.get(project.name, benchmark.name)
        candidate_runs = store.fetch_candidate_runs(
            project.name, benchmark.name, current_sha
        )
        return gate_evaluate(
            benchmark, None, [], candidate_runs, reference=reference
        )
    baseline_row = store.get_baseline(project.name, benchmark.name)
    if baseline_row is None:
        return gate_evaluate(benchmark, None, [], [])
    baseline_runs = store.fetch_baseline_runs(
        project.name, benchmark.name, baseline_row.git_sha,
        meta_seed=baseline_row.meta_seed,
    )
    candidate_runs = store.fetch_candidate_runs(
        project.name, benchmark.name, current_sha
    )
    return gate_evaluate(benchmark, baseline_row, baseline_runs, candidate_runs)


def register(project_path: str | Path) -> RegisteredProject:
    """Register the project at ``project_path`` (reads ``bench/manifest.toml``).

    Equivalent to ``bench register PATH``. Returns the resulting registry
    entry. Re-registering an already-known project name moves the pointer to
    the new path.
    """
    result = Registry().register(project_path)
    return result.project


def run(
    project_name: str,
    benchmark_name: str,
    *,
    seed_set: str = "fresh",
    meta_seed: int | None = None,
    repetitions: int | None = None,
    allow_dirty: bool = False,
) -> list[Run]:
    """Run ``benchmark_name`` of ``project_name`` and append rows to the store.

    Equivalent to ``bench run`` (foreground only). Returns the inserted ``Run``
    rows in repetition order. ``seed_set`` is ``"fresh"`` (derive seeds from a
    meta-seed, like ``bench evaluate``) or ``"baseline"`` (use the manifest's
    explicit ``baseline_seeds``). For background dispatch, use the CLI.
    """
    if seed_set not in ("baseline", "fresh"):
        raise ValueError(
            f"seed_set must be 'baseline' or 'fresh', got {seed_set!r}"
        )
    project, benchmark, project_path = _resolve(project_name, benchmark_name)
    gstate = git_state(project_path)
    if seed_set == "baseline":
        plan = plan_baseline(
            benchmark, gstate, allow_dirty=allow_dirty, repetitions=repetitions,
        )
    else:
        plan = plan_evaluation(
            benchmark, gstate, allow_dirty=allow_dirty,
            meta_seed=meta_seed, repetitions=repetitions,
        )
    return _run_plan(
        project=project, project_path=project_path, benchmark=benchmark,
        plan=plan, set_baseline=False, baseline_notes=None,
    )


def establish_baseline(
    project_name: str,
    benchmark_name: str,
    *,
    repetitions: int | None = None,
    allow_dirty: bool = False,
    notes: str | None = None,
) -> list[Run]:
    """Run the baseline seed set and mark the current SHA as baseline.

    Equivalent to ``bench baseline establish`` (foreground only, no
    ``--at-sha``). Returns the inserted ``Run`` rows in repetition order.
    Use the CLI for the historical-SHA worktree path.
    """
    project, benchmark, project_path = _resolve(project_name, benchmark_name)
    gstate = git_state(project_path)
    plan = plan_baseline(
        benchmark, gstate, allow_dirty=allow_dirty, repetitions=repetitions,
    )
    return _run_plan(
        project=project, project_path=project_path, benchmark=benchmark,
        plan=plan, set_baseline=True, baseline_notes=notes,
    )


def promote(
    project_name: str,
    benchmark_name: str,
    *,
    meta_seed: int | None = None,
    notes: str | None = None,
    force: bool = False,
) -> Verdict:
    """Move the baseline pointer to the current SHA if the verdict is PROMOTE.

    Equivalent to ``bench promote``. Returns the ``Verdict`` that was
    evaluated; on a non-``PROMOTE`` verdict raises ``ValueError`` unless
    ``force=True`` is passed. When the SHA carries multiple meta_seed groups,
    pass ``meta_seed`` to disambiguate; otherwise raises ``ValueError``.
    """
    project, benchmark, project_path = _resolve(project_name, benchmark_name)
    gstate = git_state(project_path)
    with Store(paths.store_path()) as store:
        verdict = compute_verdict(project, benchmark, gstate.sha, store)
        if verdict.kind != "PROMOTE" and not force:
            raise ValueError(
                f"refusing to promote: verdict is {verdict.kind} "
                f"(pass force=True to override)"
            )
        chosen_meta_seed = _resolve_promote_meta_seed(
            project, benchmark, gstate.sha, store, meta_seed,
        )
        store.set_baseline(
            project.name, benchmark.name, gstate.sha, utc_now(),
            notes=notes, meta_seed=chosen_meta_seed,
        )
    return verdict


def freeze_reference(
    project_name: str,
    benchmark_name: str,
    *,
    run_id: int | None = None,
    notes: str | None = None,
) -> Reference:
    """Capture a correctness benchmark's run artifact as the frozen reference.

    Equivalent to ``bench freeze-reference``. Defaults to the latest run at
    the current SHA that produced an artifact; pass ``run_id`` to pin a
    specific run. Raises ``ReferenceError`` if no eligible run exists or if a
    reference is already frozen — use ``benchstone.references.replace`` for
    overwrites.
    """
    project, benchmark, project_path = _resolve(project_name, benchmark_name)
    if benchmark.tier != "correctness":
        raise ValueError(
            f"freeze_reference only applies to correctness-tier benchmarks; "
            f"{benchmark.name!r} is tier={benchmark.tier!r}"
        )
    target_run = _resolve_artifact_run(
        project, benchmark, project_path, run_id,
    )
    return references.freeze(project.name, benchmark.name, target_run, notes=notes)


# -- internals ---------------------------------------------------------------


def _resolve(
    project_name: str, benchmark_name: str,
) -> tuple[Project, Benchmark, Path]:
    rp = Registry().resolve(project_name)
    manifest = load_manifest(rp.path)
    benchmark = manifest.benchmark(benchmark_name)
    return manifest.project, benchmark, rp.path


def _run_plan(
    *,
    project: Project,
    project_path: Path,
    benchmark: Benchmark,
    plan: RunPlan,
    set_baseline: bool,
    baseline_notes: str | None,
) -> list[Run]:
    """Foreground dispatch with admission control and an atomic transaction.

    Mirrors the foreground branch of ``cli._dispatch`` but raises native
    exceptions (``SchedulerError``, ``RunnerError``) instead of
    ``click.ClickException``. Background dispatch is intentionally not
    supported on the API path — use the CLI when you need a detached job.
    """
    active = [
        j for j in refresh_staleness(list_jobs())
        if j.status in ACTIVE_STATUSES
    ]
    admit(benchmark, active, HostCapacity.from_env())

    host = socket.gethostname()
    with Store(paths.store_path()) as store:
        with store.transaction():
            ids = runner_execute(
                project=project,
                project_path=project_path,
                benchmark=benchmark,
                plan=plan,
                store=store,
                host=host,
                logs_root=paths.logs_dir(),
                artifacts_root=paths.artifacts_dir(),
            )
            if set_baseline:
                store.set_baseline(
                    project.name, benchmark.name, plan.git_state.sha,
                    utc_now(), notes=baseline_notes,
                    meta_seed=plan.meta_seed,
                )
        return [r for r in (store.get_run(rid) for rid in ids) if r is not None]


def _resolve_promote_meta_seed(
    project: Project,
    benchmark: Benchmark,
    sha: str,
    store: Store,
    explicit: int | None,
) -> int | None:
    """Pick which meta_seed group at ``sha`` becomes the new baseline.

    Mirrors ``cli._resolve_promote_meta_seed`` but raises ``ValueError``
    rather than ``click.ClickException``. For correctness benchmarks the
    pointer just moves, since the gate doesn't read meta_seeded baseline
    distributions.
    """
    if benchmark.tier == "correctness":
        return None
    available = store.distinct_candidate_meta_seeds(
        project.name, benchmark.name, sha
    )
    if explicit is not None:
        if explicit not in available:
            raise ValueError(
                f"meta_seed={explicit} not found among candidate runs at "
                f"{sha[:10]} (available: {available or 'none'})"
            )
        return explicit
    if len(available) == 0:
        return None
    if len(available) > 1:
        raise ValueError(
            f"refusing to promote: candidate runs at {sha[:10]} cover "
            f"multiple meta_seeds ({available}); pass meta_seed=N to choose "
            f"which group becomes the new baseline"
        )
    return available[0]


def _resolve_artifact_run(
    project: Project, benchmark: Benchmark, project_path: Path, run_id: int | None,
) -> Run:
    """Look up the Run whose artifact should become the reference.

    Mirrors ``cli._resolve_artifact_run`` but raises ``ValueError`` rather
    than ``click.ClickException``.
    """
    with Store(paths.store_path()) as store:
        if run_id is not None:
            target = store.get_run(run_id)
            if target is None:
                raise ValueError(f"no run with id={run_id}")
            if target.project != project.name or target.benchmark != benchmark.name:
                raise ValueError(
                    f"run id={run_id} belongs to {target.project}/{target.benchmark}, "
                    f"not {project.name}/{benchmark.name}"
                )
            return target
        sha = git_state(project_path).sha
        candidates = store.fetch_candidate_runs(project.name, benchmark.name, sha)
        with_artifact = [r for r in candidates if r.artifact_hash is not None]
        if not with_artifact:
            raise ValueError(
                f"no run with an artifact at current SHA {sha[:10]}; "
                f"call api.run({project.name!r}, {benchmark.name!r}) first, "
                f"or pass run_id=N"
            )
        return with_artifact[-1]
