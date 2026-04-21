from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path

import click

from . import __version__, background, jobs, paths, references
from .gate import Verdict, evaluate as gate_evaluate
from .manifest import Benchmark, ManifestError, Project, load as load_manifest
from .provenance import GitState, ProvenanceError, git_state
from .references import ReferenceError
from .registry import Registry, RegistryError
from .runner import (
    RunPlan,
    RunnerError,
    execute as runner_execute,
    plan_baseline,
    plan_evaluation,
)
from .scheduler import HostCapacity, SchedulerError, admit
from .store import Run, Store
from .worktree import WorktreeError, with_git_worktree


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
@click.option("--repetitions", "-n", type=int, default=None,
              help="Override the manifest's repetition count for this invocation only. "
                   "For --seed-set baseline, must be <= len(baseline_seeds).")
@click.option("--allow-dirty", is_flag=True, default=False,
              help="Allow execution against a dirty git tree (records a diff file).")
@click.option("--background/--foreground", "background_flag", default=None,
              help="Force background or foreground dispatch; default follows the manifest.")
def run(
    project_name: str,
    benchmark_name: str,
    seed_set: str,
    meta_seed: int | None,
    repetitions: int | None,
    allow_dirty: bool,
    background_flag: bool | None,
) -> None:
    """Run BENCHMARK_NAME of PROJECT_NAME and append the result row(s) to the store."""
    project_path, project, benchmark = _resolve(project_name, benchmark_name)
    gstate = _require_git_state(project_path)
    try:
        if seed_set == "baseline":
            plan = plan_baseline(
                benchmark, gstate, allow_dirty=allow_dirty, repetitions=repetitions,
            )
        else:
            plan = plan_evaluation(
                benchmark, gstate, allow_dirty=allow_dirty,
                meta_seed=meta_seed, repetitions=repetitions,
            )
    except RunnerError as exc:
        raise click.ClickException(str(exc))

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
@click.option("--repetitions", "-n", type=int, default=None,
              help="Override the baseline seed count for this invocation. "
                   "Must be <= len(baseline_seeds) in the manifest.")
@click.option("--at-sha", "at_sha", default=None,
              help="Establish the baseline at a past SHA via a temporary git "
                   "worktree. Uses the manifest as it existed at that SHA. "
                   "Forces foreground execution.")
@click.option("--background/--foreground", "background_flag", default=None,
              help="Force background or foreground dispatch; default follows the manifest.")
def baseline_establish(
    project_name: str,
    benchmark_name: str,
    allow_dirty: bool,
    notes: str | None,
    repetitions: int | None,
    at_sha: str | None,
    background_flag: bool | None,
) -> None:
    """Run the baseline seed set and mark the current (or given) SHA as baseline."""
    project_path, project, _ = _resolve(project_name, benchmark_name)

    if at_sha is None:
        gstate = _require_git_state(project_path)
        try:
            benchmark = load_manifest(project_path).benchmark(benchmark_name)
        except (ManifestError, KeyError) as exc:
            raise click.ClickException(str(exc))
        try:
            plan = plan_baseline(
                benchmark, gstate, allow_dirty=allow_dirty, repetitions=repetitions,
            )
        except RunnerError as exc:
            raise click.ClickException(str(exc))

        _dispatch(
            project=project,
            project_path=project_path,
            benchmark=benchmark,
            plan=plan,
            background_flag=background_flag,
            set_baseline=True,
            baseline_notes=notes,
        )
        return

    # --at-sha: run the benchmark inside a temporary worktree of that SHA,
    # using that SHA's manifest. Background dispatch is incompatible (the
    # worktree would have to outlive the parent CLI invocation), so force fg.
    if background_flag is True:
        raise click.ClickException("--at-sha is incompatible with --background")

    _establish_at_sha(
        project=project,
        project_path=project_path,
        benchmark_name=benchmark_name,
        at_sha=at_sha,
        allow_dirty=allow_dirty,
        repetitions=repetitions,
        notes=notes,
    )


def _establish_at_sha(
    *,
    project: Project,
    project_path: Path,
    benchmark_name: str,
    at_sha: str,
    allow_dirty: bool,
    repetitions: int | None,
    notes: str | None,
) -> None:
    try:
        with with_git_worktree(project_path, at_sha) as wt_path:
            try:
                manifest_at_sha = load_manifest(wt_path)
            except ManifestError as exc:
                raise click.ClickException(
                    f"manifest at {at_sha[:10]} is invalid: {exc}"
                )
            try:
                benchmark = manifest_at_sha.benchmark(benchmark_name)
            except KeyError:
                raise click.ClickException(
                    f"benchmark {benchmark_name!r} does not exist at {at_sha[:10]}"
                )
            gstate_at_sha = _require_git_state(wt_path)
            try:
                plan = plan_baseline(
                    benchmark, gstate_at_sha,
                    allow_dirty=allow_dirty, repetitions=repetitions,
                )
            except RunnerError as exc:
                raise click.ClickException(str(exc))

            click.echo(
                f"establishing baseline in worktree at {gstate_at_sha.sha[:10]}"
            )
            _dispatch(
                project=manifest_at_sha.project,
                project_path=wt_path,
                benchmark=benchmark,
                plan=plan,
                background_flag=False,
                set_baseline=True,
                baseline_notes=_annotate_notes(notes, at_sha),
            )
    except WorktreeError as exc:
        raise click.ClickException(str(exc))


def _annotate_notes(notes: str | None, at_sha: str) -> str:
    tag = f"[established at {at_sha[:10]} via --at-sha]"
    return f"{tag} {notes}" if notes else tag


VALID_VERDICT_KINDS: frozenset[str] = frozenset({
    "PROMOTE", "REJECT", "NEEDS_MORE_DATA", "NO_BASELINE",
    "PASS", "FAIL", "NO_REFERENCE",
})


@main.command()
@click.argument("project_name")
@click.argument("benchmark_name")
@click.option("--expect", "expect", default=None,
              help="Assert the verdict has this kind. Exit 0 on match, 4 on mismatch. "
                   "Intended for validation scripts (e.g. whitespace-commit smoke tests).")
def evaluate(
    project_name: str, benchmark_name: str, expect: str | None
) -> None:
    """Compare runs at the current SHA against the recorded baseline. Read-only."""
    project_path, project, benchmark = _resolve(project_name, benchmark_name)
    gstate = _require_git_state(project_path)
    if expect is not None and expect not in VALID_VERDICT_KINDS:
        raise click.ClickException(
            f"--expect must be one of {sorted(VALID_VERDICT_KINDS)}, got {expect!r}"
        )
    verdict = _compute_verdict(project, benchmark, project_path, gstate.sha)
    _print_verdict(project, benchmark, gstate.sha, verdict)
    if expect is not None:
        if verdict.kind == expect:
            click.echo(f"  expected:    {expect}  (match)")
            raise SystemExit(0)
        click.echo(f"  expected:    {expect}  (MISMATCH — got {verdict.kind})")
        raise SystemExit(4)
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


@main.command("freeze-reference")
@click.argument("project_name")
@click.argument("benchmark_name")
@click.option("--from-run", "run_id", type=int, default=None,
              help="Run ID whose artifact becomes the reference. "
                   "Defaults to the latest run at the current SHA.")
@click.option("--notes", default=None)
def freeze_reference_cmd(
    project_name: str, benchmark_name: str, run_id: int | None, notes: str | None,
) -> None:
    """Capture a correctness benchmark's run artifact as the frozen reference."""
    project_path, project, benchmark = _resolve(project_name, benchmark_name)
    if benchmark.tier != "correctness":
        raise click.ClickException(
            f"freeze-reference only applies to correctness-tier benchmarks; "
            f"{benchmark.name!r} is tier={benchmark.tier}"
        )
    run = _resolve_artifact_run(project, benchmark, project_path, run_id)
    try:
        ref = references.freeze(project.name, benchmark.name, run, notes=notes)
    except ReferenceError as exc:
        raise click.ClickException(str(exc))
    click.echo(
        f"frozen reference: {project.name}/{benchmark.name}  "
        f"content_hash={ref.content_hash}  from_run={ref.from_run_id}"
    )


@main.command("replace-reference")
@click.argument("project_name")
@click.argument("benchmark_name")
@click.option("--reason", required=True,
              help="Required: the reason the reference is being replaced (logged to history).")
@click.option("--from-run", "run_id", type=int, default=None,
              help="Run ID whose artifact becomes the new reference. "
                   "Defaults to the latest run at the current SHA.")
@click.option("--notes", default=None)
def replace_reference_cmd(
    project_name: str,
    benchmark_name: str,
    reason: str,
    run_id: int | None,
    notes: str | None,
) -> None:
    """Replace the frozen reference and record the replacement in history."""
    project_path, project, benchmark = _resolve(project_name, benchmark_name)
    if benchmark.tier != "correctness":
        raise click.ClickException(
            f"replace-reference only applies to correctness-tier benchmarks; "
            f"{benchmark.name!r} is tier={benchmark.tier}"
        )
    run = _resolve_artifact_run(project, benchmark, project_path, run_id)
    try:
        ref = references.replace(
            project.name, benchmark.name, run, reason=reason, notes=notes
        )
    except ReferenceError as exc:
        raise click.ClickException(str(exc))
    click.echo(
        f"replaced reference: {project.name}/{benchmark.name}  "
        f"new_content_hash={ref.content_hash}  from_run={ref.from_run_id}  "
        f"reason={reason!r}"
    )


@main.command("history")
@click.argument("project_name")
@click.argument("benchmark_name")
@click.option("--since", default=None,
              help="Only show runs whose timestamp is >= this ISO string "
                   "(e.g. 2026-04-19 or 2026-04-19T14:22:11Z).")
@click.option("--git-sha", default=None,
              help="Filter to runs at a specific git SHA (prefix match).")
@click.option("--limit", type=int, default=None,
              help="Show only the most recent N runs after other filters are applied.")
def history_cmd(
    project_name: str,
    benchmark_name: str,
    since: str | None,
    git_sha: str | None,
    limit: int | None,
) -> None:
    """Print a timeline of runs for PROJECT_NAME/BENCHMARK_NAME."""
    with Store(paths.store_path()) as store:
        runs = store.fetch_runs(project_name, benchmark_name)
    if git_sha is not None:
        runs = [r for r in runs if r.git_sha.startswith(git_sha)]
    if since is not None:
        runs = [r for r in runs if r.timestamp >= since]
    if limit is not None and limit >= 0:
        runs = runs[-limit:]
    if not runs:
        click.echo("(no runs match)")
        return

    with Store(paths.store_path()) as store:
        baseline_row = store.get_baseline(project_name, benchmark_name)
    baseline_sha = baseline_row.git_sha if baseline_row is not None else None
    if baseline_sha:
        click.echo(f"# baseline @ {baseline_sha[:10]}")

    for r in runs:
        metric_str = f"metric={r.metric:.6f}" if r.metric is not None else "metric=-"
        meta_tag = "baseline" if r.meta_seed is None else f"meta={r.meta_seed}"
        artifact = f"  artifact={r.artifact_hash[:18]}..." if r.artifact_hash else ""
        dirty = "  DIRTY" if r.git_dirty else ""
        marker = "  *" if baseline_sha and r.git_sha == baseline_sha else "   "
        click.echo(
            f"{r.timestamp}{marker}run={r.id}  sha={r.git_sha[:10]}  "
            f"seed={r.seed}  {meta_tag}  rep={r.repetition_index}  "
            f"{r.status:<5}  {metric_str}  wall={r.wall_clock_seconds:.3f}s"
            f"{artifact}{dirty}"
        )


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
                artifacts_root=paths.artifacts_dir(),
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


def _resolve_artifact_run(
    project: Project, benchmark: Benchmark, project_path: Path, run_id: int | None,
) -> Run:
    """Look up the Run whose artifact should become (or replace) the reference.

    Explicit ``--from-run`` takes precedence; otherwise picks the latest run at
    the current SHA that produced an artifact.
    """
    with Store(paths.store_path()) as store:
        if run_id is not None:
            run = store.get_run(run_id)
            if run is None:
                raise click.ClickException(f"no run with id={run_id}")
            if run.project != project.name or run.benchmark != benchmark.name:
                raise click.ClickException(
                    f"run id={run_id} belongs to {run.project}/{run.benchmark}, "
                    f"not {project.name}/{benchmark.name}"
                )
            return run
        gstate = _require_git_state(project_path)
        candidates = store.fetch_candidate_runs(
            project.name, benchmark.name, gstate.sha
        )
        with_artifact = [r for r in candidates if r.artifact_hash is not None]
        if not with_artifact:
            raise click.ClickException(
                f"no run with an artifact at current SHA {gstate.sha[:10]}; "
                f"run `bench run {project.name} {benchmark.name}` first, or pass --from-run"
            )
        return with_artifact[-1]


def _compute_verdict(
    project: Project, benchmark: Benchmark, project_path: Path, current_sha: str
) -> Verdict:
    with Store(paths.store_path()) as store:
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
            project.name, benchmark.name, baseline_row.git_sha
        )
        candidate_runs = store.fetch_candidate_runs(
            project.name, benchmark.name, current_sha
        )
        return gate_evaluate(benchmark, baseline_row, baseline_runs, candidate_runs)


BASELINE_CV_WARN = 0.05  # SE/|mean| above this triggers a fragility hint.


def _print_verdict(
    project: Project, benchmark: Benchmark, current_sha: str, v: Verdict
) -> None:
    click.echo(f"{project.name} / {benchmark.name}")
    click.echo(f"  current_sha: {current_sha[:10]}")
    click.echo(f"  tier:        {benchmark.tier}")
    if benchmark.tier == "correctness":
        if v.reference_hash is not None:
            click.echo(f"  reference:   {v.reference_hash}")
        if v.candidate_hash is not None:
            click.echo(f"  candidate:   {v.candidate_hash}")
    else:
        click.echo(f"  direction:   {benchmark.metric_direction}")
        click.echo(f"  gate_policy: {benchmark.gate_policy}")
        if v.sigma is not None:
            b_cv = _cv(v.baseline_mean, v.baseline_se)
            c_cv = _cv(v.candidate_mean, v.candidate_se)
            click.echo(
                f"  baseline:    mean={v.baseline_mean:.6f}  se={v.baseline_se:.6f}  "
                f"cv={b_cv:.4f}"
            )
            click.echo(
                f"  candidate:   mean={v.candidate_mean:.6f}  se={v.candidate_se:.6f}  "
                f"cv={c_cv:.4f}"
            )
            stat_label = "z:" if benchmark.gate_policy == "mann_whitney" else "sigma:"
            click.echo(
                f"  {stat_label:<12} {v.sigma:+.3f}  (threshold {v.threshold:.3f})"
            )
            if b_cv > BASELINE_CV_WARN and benchmark.gate_policy == "sigma":
                click.echo(
                    f"  warning:     baseline cv={b_cv:.4f} > {BASELINE_CV_WARN:.2f}; "
                    f"the promotion threshold may be dominated by outliers in the "
                    f"baseline sample. Consider gate_policy = \"mann_whitney\" in "
                    f"the manifest."
                )
    click.echo(f"  verdict:     {v.kind}  {v.reason}")


def _cv(mean: float | None, se: float | None) -> float:
    if mean is None or se is None or mean == 0:
        return 0.0
    return abs(se / mean)


def _verdict_exit_code(v: Verdict) -> int:
    return {
        "PROMOTE": 0,
        "PASS": 0,
        "REJECT": 1,
        "FAIL": 1,
        "NEEDS_MORE_DATA": 2,
        "NO_BASELINE": 2,
        "NO_REFERENCE": 2,
    }.get(v.kind, 3)


def _summarize_runs(store: Store, ids: list[int], plan: RunPlan) -> None:
    click.echo(f"dispatched {len(ids)} run(s)  git_sha={plan.git_state.sha[:10]}  "
               f"dirty={plan.git_state.dirty}  meta_seed={plan.meta_seed}")
    for rid in ids:
        r = store.get_run(rid)
        if r is None:
            continue
        if r.status == "ok":
            metric_fmt = f"metric={r.metric:.6f}" if r.metric is not None else "metric=-"
            art = f"  artifact={r.artifact_hash[:18]}..." if r.artifact_hash else ""
            click.echo(
                f"  run={rid} rep={r.repetition_index} seed={r.seed}  "
                f"{metric_fmt}  wall={r.wall_clock_seconds:.3f}s{art}"
            )
        else:
            meta = r.project_metadata or {}
            click.echo(
                f"  run={rid} rep={r.repetition_index} seed={r.seed}  ERROR  {meta}"
            )


if __name__ == "__main__":
    main()
