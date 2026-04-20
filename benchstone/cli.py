from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path

import click

from . import __version__, background, jobs, paths
from .gate import Verdict, evaluate as gate_evaluate
from .manifest import Benchmark, ManifestError, Project, load as load_manifest
from .provenance import GitState, ProvenanceError, git_state
from .registry import Registry, RegistryError
from .runner import (
    RunPlan,
    RunnerError,
    execute as runner_execute,
    plan_baseline,
    plan_evaluation,
)
from .scheduler import HostCapacity, SchedulerError, admit
from .store import Store


@click.group()
@click.version_option(__version__, prog_name="bench")
def main() -> None:
    """benchstone — portfolio-wide benchmark harness."""


@main.command()
@click.argument(
    "project_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
)
def register(project_path: str) -> None:
    """Register a project located at PROJECT_PATH."""
    registry = Registry()
    try:
        rp = registry.register(project_path)
    except (RegistryError, ManifestError) as exc:
        raise click.ClickException(str(exc))
    click.echo(f"registered {rp.name}  {rp.path}")
    click.echo(f"  manifest: {rp.manifest_hash}")


@main.command("list")
@click.option("--project", "-p", default=None, help="Limit output to a single project.")
def list_cmd(project: str | None) -> None:
    """List registered projects and the benchmarks each one declares."""
    registry = Registry()
    projects = registry.list_projects()
    if project is not None:
        projects = [p for p in projects if p.name == project]
        if not projects:
            raise click.ClickException(f"no registered project named {project!r}")
    if not projects:
        click.echo("(no projects registered)")
        return
    for p in projects:
        click.echo(f"{p.name}  {p.path}")
        try:
            manifest = load_manifest(p.path)
        except ManifestError as exc:
            click.echo(f"  (manifest error: {exc})")
            continue
        for b in manifest.benchmarks:
            click.echo(
                f"  - {b.name}  tier={b.tier}  reps={b.repetitions}  "
                f"threads={b.threads}  gpu={b.gpu}  bg_required={b.background_required}"
            )


@main.command()
@click.argument("project_name")
@click.argument("benchmark_name")
@click.option(
    "--seed-set",
    type=click.Choice(["baseline", "fresh"]),
    default="fresh",
    help="Which seeds to use: the manifest's baseline_seeds or a freshly derived set.",
)
@click.option("--meta-seed", type=int, default=None,
              help="Override the meta-seed used to derive fresh seeds (reproducibility aid).")
@click.option("--allow-dirty", is_flag=True, default=False,
              help="Allow execution against a dirty git tree (records a diff file).")
@click.option("--background/--foreground", "background_flag", default=None,
              help="Force background or foreground dispatch; default follows the manifest.")
def run(
    project_name: str,
    benchmark_name: str,
    seed_set: str,
    meta_seed: int | None,
    allow_dirty: bool,
    background_flag: bool | None,
) -> None:
    """Run BENCHMARK_NAME of PROJECT_NAME and append the result row(s) to the store."""
    project_path, project, benchmark = _resolve(project_name, benchmark_name)
    gstate = _require_git_state(project_path)
    if seed_set == "baseline":
        plan = plan_baseline(benchmark, gstate, allow_dirty=allow_dirty)
    else:
        plan = plan_evaluation(benchmark, gstate, allow_dirty=allow_dirty, meta_seed=meta_seed)

    _dispatch(
        project=project,
        project_path=project_path,
        benchmark=benchmark,
        plan=plan,
        background_flag=background_flag,
        set_baseline=False,
        baseline_notes=None,
    )


@main.group()
def baseline() -> None:
    """Baseline pointer operations."""


@baseline.command("establish")
@click.argument("project_name")
@click.argument("benchmark_name")
@click.option("--allow-dirty", is_flag=True, default=False)
@click.option("--notes", default=None, help="Free-form note attached to the baseline.")
@click.option("--background/--foreground", "background_flag", default=None,
              help="Force background or foreground dispatch; default follows the manifest.")
def baseline_establish(
    project_name: str,
    benchmark_name: str,
    allow_dirty: bool,
    notes: str | None,
    background_flag: bool | None,
) -> None:
    """Run the baseline seed set and mark the current SHA as baseline."""
    project_path, project, benchmark = _resolve(project_name, benchmark_name)
    gstate = _require_git_state(project_path)
    plan = plan_baseline(benchmark, gstate, allow_dirty=allow_dirty)

    _dispatch(
        project=project,
        project_path=project_path,
        benchmark=benchmark,
        plan=plan,
        background_flag=background_flag,
        set_baseline=True,
        baseline_notes=notes,
    )


@main.command()
@click.argument("project_name")
@click.argument("benchmark_name")
def evaluate(project_name: str, benchmark_name: str) -> None:
    """Compare runs at the current SHA against the recorded baseline. Read-only."""
    project_path, project, benchmark = _resolve(project_name, benchmark_name)
    gstate = _require_git_state(project_path)
    verdict = _compute_verdict(project, benchmark, project_path, gstate.sha)
    _print_verdict(project, benchmark, gstate.sha, verdict)
    raise SystemExit(_verdict_exit_code(verdict))


@main.command()
@click.argument("project_name")
@click.argument("benchmark_name")
@click.option("--force", is_flag=True, default=False,
              help="Promote even if the current verdict is not PROMOTE.")
@click.option("--notes", default=None)
def promote(
    project_name: str, benchmark_name: str, force: bool, notes: str | None
) -> None:
    """Update the baseline pointer to the current SHA if the verdict is PROMOTE."""
    project_path, project, benchmark = _resolve(project_name, benchmark_name)
    gstate = _require_git_state(project_path)
    verdict = _compute_verdict(project, benchmark, project_path, gstate.sha)
    _print_verdict(project, benchmark, gstate.sha, verdict)
    if verdict.kind != "PROMOTE" and not force:
        raise click.ClickException(
            f"refusing to promote: verdict is {verdict.kind} (pass --force to override)"
        )
    with Store(paths.store_path()) as store:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.set_baseline(project.name, benchmark.name, gstate.sha, now, notes)
    click.echo(f"baseline promoted: {project.name}/{benchmark.name} -> {gstate.sha[:10]}")


@main.command("status")
@click.option("--all", "show_all", is_flag=True, default=False,
              help="Include terminal (done/failed/stale) jobs as well as active ones.")
def status_cmd(show_all: bool) -> None:
    """List background jobs and their current state."""
    all_jobs = jobs.refresh_staleness(jobs.list_all())
    if not show_all:
        all_jobs = [j for j in all_jobs if j.status in jobs.ACTIVE_STATUSES]
    if not all_jobs:
        click.echo("(no jobs)" if show_all else "(no active jobs)")
        return
    capacity = HostCapacity.from_env()
    active = [j for j in all_jobs if j.status in jobs.ACTIVE_STATUSES]
    threads_used = sum(j.threads for j in active)
    gpu_used = sum(1 for j in active if j.gpu == "direct")
    click.echo(
        f"host capacity: threads={capacity.threads} gpu={capacity.gpu_count}  "
        f"| in use: threads={threads_used} gpu_direct={gpu_used}"
    )
    for j in all_jobs:
        run_summary = (
            f" runs={len(j.inserted_run_ids)}" if j.inserted_run_ids else ""
        )
        msg = f"  msg={j.message}" if j.message else ""
        click.echo(
            f"  {j.job_id}  {j.status:<8}  {j.project}/{j.benchmark}"
            f"  pid={j.pid}  threads={j.threads}  gpu={j.gpu}"
            f"  started={j.started_at}{run_summary}{msg}"
        )


# --- helpers -----------------------------------------------------------------


def _resolve(project_name: str, benchmark_name: str) -> tuple[Path, Project, Benchmark]:
    registry = Registry()
    try:
        rp = registry.resolve(project_name)
    except RegistryError as exc:
        raise click.ClickException(str(exc))
    try:
        manifest = load_manifest(rp.path)
    except ManifestError as exc:
        raise click.ClickException(f"{rp.path}: {exc}")
    try:
        bench = manifest.benchmark(benchmark_name)
    except KeyError:
        raise click.ClickException(
            f"no benchmark named {benchmark_name!r} in project {project_name!r}"
        )
    return rp.path, manifest.project, bench


def _require_git_state(project_path: Path) -> GitState:
    try:
        return git_state(project_path)
    except ProvenanceError as exc:
        raise click.ClickException(f"git state: {exc}")


def _dispatch(
    *,
    project: Project,
    project_path: Path,
    benchmark: Benchmark,
    plan: RunPlan,
    background_flag: bool | None,
    set_baseline: bool,
    baseline_notes: str | None,
) -> None:
    """Admission-check and dispatch a plan either foreground or background."""
    use_background = _decide_background(benchmark, background_flag)
    active_jobs = jobs.refresh_staleness(jobs.list_all())
    active_jobs = [j for j in active_jobs if j.status in jobs.ACTIVE_STATUSES]
    try:
        admit(benchmark, active_jobs, HostCapacity.from_env())
    except SchedulerError as exc:
        raise click.ClickException(str(exc))

    host = socket.gethostname()

    if use_background:
        job = background.spawn(
            project=project,
            project_path=project_path,
            benchmark=benchmark,
            plan=plan,
            host=host,
            set_baseline=set_baseline,
            baseline_notes=baseline_notes,
        )
        click.echo(
            f"dispatched background job {job.job_id}  pid={job.pid}  "
            f"{project.name}/{benchmark.name}  "
            f"git_sha={plan.git_state.sha[:10]}  dirty={plan.git_state.dirty}  "
            f"meta_seed={plan.meta_seed}"
        )
        if set_baseline:
            click.echo("  (baseline pointer will be updated when the job completes)")
        return

    with Store(paths.store_path()) as store:
        try:
            ids = runner_execute(
                project=project,
                project_path=project_path,
                benchmark=benchmark,
                plan=plan,
                store=store,
                host=host,
                logs_root=paths.logs_dir(),
            )
        except RunnerError as exc:
            raise click.ClickException(str(exc))
        _summarize_runs(store, ids, plan)

        if set_baseline:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            store.set_baseline(
                project.name, benchmark.name, plan.git_state.sha, now, baseline_notes
            )
            click.echo(
                f"baseline set: {project.name}/{benchmark.name} -> "
                f"{plan.git_state.sha[:10]} ({len(ids)} run(s))"
            )


def _decide_background(benchmark: Benchmark, flag: bool | None) -> bool:
    if flag is not None:
        return flag
    return benchmark.background_required


def _compute_verdict(
    project: Project, benchmark: Benchmark, project_path: Path, current_sha: str
) -> Verdict:
    with Store(paths.store_path()) as store:
        baseline_row = store.get_baseline(project.name, benchmark.name)
        if baseline_row is None:
            return gate_evaluate(benchmark, None, [], [])
        baseline_runs = store.fetch_baseline_runs(
            project.name, benchmark.name, baseline_row.git_sha
        )
        candidate_runs = store.fetch_candidate_runs(
            project.name, benchmark.name, current_sha
        )
        return gate_evaluate(benchmark, baseline_row, baseline_runs, candidate_runs)


def _print_verdict(
    project: Project, benchmark: Benchmark, current_sha: str, v: Verdict
) -> None:
    click.echo(f"{project.name} / {benchmark.name}")
    click.echo(f"  current_sha: {current_sha[:10]}")
    click.echo(f"  direction:   {benchmark.metric_direction}")
    if v.sigma is not None:
        click.echo(
            f"  baseline:    mean={v.baseline_mean:.6f}  se={v.baseline_se:.6f}"
        )
        click.echo(
            f"  candidate:   mean={v.candidate_mean:.6f}  se={v.candidate_se:.6f}"
        )
        click.echo(f"  sigma:       {v.sigma:+.3f}  (threshold {v.threshold:.3f})")
    click.echo(f"  verdict:     {v.kind}  {v.reason}")


def _verdict_exit_code(v: Verdict) -> int:
    return {
        "PROMOTE": 0,
        "REJECT": 1,
        "NEEDS_MORE_DATA": 2,
        "NO_BASELINE": 2,
    }.get(v.kind, 3)


def _summarize_runs(store: Store, ids: list[int], plan: RunPlan) -> None:
    click.echo(f"dispatched {len(ids)} run(s)  git_sha={plan.git_state.sha[:10]}  "
               f"dirty={plan.git_state.dirty}  meta_seed={plan.meta_seed}")
    for rid in ids:
        r = store.get_run(rid)
        if r is None:
            continue
        if r.status == "ok":
            click.echo(f"  [rep {r.repetition_index} seed={r.seed}] "
                       f"metric={r.metric:.6f}  wall={r.wall_clock_seconds:.3f}s")
        else:
            meta = r.project_metadata or {}
            click.echo(f"  [rep {r.repetition_index} seed={r.seed}] ERROR  {meta}")


if __name__ == "__main__":
    main()
