from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .manifest import Benchmark
from .stats import directed_sigma, mean_se
from .store import Baseline, Run

VerdictKind = str  # "PROMOTE" | "REJECT" | "NEEDS_MORE_DATA" | "NO_BASELINE"
                    # "PASS" | "FAIL" come in Phase 3 for the correctness tier.


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    sigma: float | None = None
    threshold: float | None = None
    baseline_mean: float | None = None
    baseline_se: float | None = None
    candidate_mean: float | None = None
    candidate_se: float | None = None
    reason: str = ""


def evaluate(
    benchmark: Benchmark,
    baseline: Baseline | None,
    baseline_runs: Sequence[Run],
    candidate_runs: Sequence[Run],
) -> Verdict:
    """Produce a verdict comparing candidate to baseline for a stochastic benchmark.

    Correctness-tier and deterministic non-correctness benchmarks are out of
    scope for Phase 1: the former lands with Phase 3's reference store; the
    latter is deferred until a real use-case materializes.
    """
    if benchmark.tier == "correctness":
        raise NotImplementedError(
            "correctness-tier gate requires the frozen reference store (Phase 3)"
        )
    if benchmark.deterministic:
        raise NotImplementedError(
            "deterministic non-correctness benchmarks are not yet supported by the gate"
        )
    if baseline is None:
        return Verdict(kind="NO_BASELINE", reason="no baseline has been established")

    baseline_metrics = [
        r.metric for r in baseline_runs if r.status == "ok" and r.metric is not None
    ]
    candidate_metrics = [
        r.metric for r in candidate_runs if r.status == "ok" and r.metric is not None
    ]

    required_baseline = max(len(benchmark.baseline_seeds), 2)
    required_candidate = max(benchmark.repetitions, 2)

    if len(baseline_metrics) < required_baseline:
        return Verdict(
            kind="NEEDS_MORE_DATA",
            reason=(
                f"baseline has {len(baseline_metrics)} ok run(s) at "
                f"{baseline.git_sha[:10]}, need {required_baseline}"
            ),
        )
    if len(candidate_metrics) < required_candidate:
        return Verdict(
            kind="NEEDS_MORE_DATA",
            reason=(
                f"candidate has {len(candidate_metrics)} ok run(s), "
                f"need {required_candidate}"
            ),
        )

    bmean, bse = mean_se(baseline_metrics)
    cmean, cse = mean_se(candidate_metrics)
    direction = benchmark.metric_direction
    assert direction is not None  # manifest validation guarantees this for non-correctness
    sigma = directed_sigma(baseline_metrics, candidate_metrics, direction)
    threshold = benchmark.promotion_sigma
    assert threshold is not None

    kind = "PROMOTE" if sigma >= threshold else "REJECT"
    reason = (
        f"sigma {sigma:+.3f} {'>=' if sigma >= threshold else '<'} "
        f"threshold {threshold:.3f} (direction={direction})"
    )
    return Verdict(
        kind=kind,
        sigma=sigma,
        threshold=threshold,
        baseline_mean=bmean,
        baseline_se=bse,
        candidate_mean=cmean,
        candidate_se=cse,
        reason=reason,
    )
