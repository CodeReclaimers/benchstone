"""Public Python API.

Read-only entry points for consumer scripts (autoloops, CI steps, custom
dashboards) that previously had to shell out to ``bench evaluate`` and parse
stdout for the verdict line. The CLI is built on the same primitives.

Errors related to *configuration* (project not registered, manifest invalid,
benchmark name unknown, no git state) propagate as their native exceptions
(``RegistryError``, ``ManifestError``, ``KeyError``, ``ProvenanceError``).
*Gate outcomes* — including "no baseline yet" or "not enough runs" — are
returned as ``Verdict`` objects with the corresponding ``kind``.
"""
from __future__ import annotations

from pathlib import Path

from . import paths, references
from .gate import Verdict, evaluate as gate_evaluate
from .manifest import Benchmark, Project, load as load_manifest
from .provenance import git_state
from .registry import Registry
from .store import Run, Store

__all__ = ["Run", "Verdict", "compute_verdict", "evaluate", "history"]


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


def _resolve(
    project_name: str, benchmark_name: str,
) -> tuple[Project, Benchmark, Path]:
    rp = Registry().resolve(project_name)
    manifest = load_manifest(rp.path)
    benchmark = manifest.benchmark(benchmark_name)
    return manifest.project, benchmark, rp.path
