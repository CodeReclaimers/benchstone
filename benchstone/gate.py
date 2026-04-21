from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .manifest import Benchmark
from .references import Reference
from .stats import directed_z, mann_whitney_z, mean_se
from .store import Baseline, Run

VerdictKind = str
# Stochastic tiers: "PROMOTE" | "REJECT" | "NEEDS_MORE_DATA" | "NO_BASELINE"
# Correctness tier: "PASS"    | "FAIL"   | "NEEDS_MORE_DATA" | "NO_REFERENCE"

STOCHASTIC_KINDS: frozenset[str] = frozenset(
    {"PROMOTE", "REJECT", "NEEDS_MORE_DATA", "NO_BASELINE"}
)
CORRECTNESS_KINDS: frozenset[str] = frozenset(
    {"PASS", "FAIL", "NEEDS_MORE_DATA", "NO_REFERENCE"}
)
ALL_VERDICT_KINDS: frozenset[str] = STOCHASTIC_KINDS | CORRECTNESS_KINDS


VerdictCategory = str  # "stochastic" | "correctness"


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    # Dispatch key for formatters: "stochastic" verdicts carry
    # sigma/threshold/mean/se fields; "correctness" verdicts carry
    # reference_hash/candidate_hash. Keyed off the Verdict itself rather than
    # the benchmark tier so downstream consumers don't need to re-derive which
    # fields are populated.
    category: VerdictCategory = "stochastic"
    sigma: float | None = None
    threshold: float | None = None
    baseline_mean: float | None = None
    baseline_se: float | None = None
    candidate_mean: float | None = None
    candidate_se: float | None = None
    reference_hash: str | None = None
    candidate_hash: str | None = None
    reason: str = ""


def evaluate(
    benchmark: Benchmark,
    baseline: Baseline | None,
    baseline_runs: Sequence[Run],
    candidate_runs: Sequence[Run],
    reference: Reference | None = None,
) -> Verdict:
    """Produce a verdict for the benchmark at hand.

    For correctness tier: compares the latest candidate run's artifact_hash to
    the frozen reference's content_hash. Returns PASS on match, FAIL on
    mismatch, NO_REFERENCE if none has been frozen, NEEDS_MORE_DATA if no
    candidate run with an artifact exists at the current SHA.

    For quality/performance tiers: sigma-based comparison of baseline and
    candidate sample distributions; positive sigma means improvement after the
    direction flip.
    """
    if benchmark.tier == "correctness":
        return _correctness_verdict(benchmark, reference, candidate_runs)

    if benchmark.deterministic:
        raise NotImplementedError(
            "deterministic non-correctness benchmarks are not yet supported by the gate"
        )
    if baseline is None:
        return Verdict(
            kind="NO_BASELINE",
            category="stochastic",
            reason="no baseline has been established",
        )

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
            category="stochastic",
            reason=(
                f"baseline has {len(baseline_metrics)} ok run(s) at "
                f"{baseline.git_sha[:10]}, need {required_baseline}"
            ),
        )
    if len(candidate_metrics) < required_candidate:
        return Verdict(
            kind="NEEDS_MORE_DATA",
            category="stochastic",
            reason=(
                f"candidate has {len(candidate_metrics)} ok run(s), "
                f"need {required_candidate}"
            ),
        )

    bmean, bse = mean_se(baseline_metrics)
    cmean, cse = mean_se(candidate_metrics)
    direction = benchmark.metric_direction
    assert direction is not None

    policy = benchmark.gate_policy
    if policy == "mann_whitney":
        statistic = mann_whitney_z(baseline_metrics, candidate_metrics, direction)
        policy_tag = "mann_whitney z"
        threshold = (
            benchmark.promotion_z
            if benchmark.promotion_z is not None
            else benchmark.promotion_sigma
        )
    else:
        statistic = directed_z(baseline_metrics, candidate_metrics, direction)
        policy_tag = "sigma"
        threshold = benchmark.promotion_sigma
    assert threshold is not None

    kind = "PROMOTE" if statistic >= threshold else "REJECT"
    reason = (
        f"{policy_tag} {statistic:+.3f} {'>=' if statistic >= threshold else '<'} "
        f"threshold {threshold:.3f} (direction={direction})"
    )
    return Verdict(
        kind=kind,
        category="stochastic",
        sigma=statistic,
        threshold=threshold,
        baseline_mean=bmean,
        baseline_se=bse,
        candidate_mean=cmean,
        candidate_se=cse,
        reason=reason,
    )


def _correctness_verdict(
    benchmark: Benchmark,
    reference: Reference | None,
    candidate_runs: Sequence[Run],
) -> Verdict:
    if reference is None:
        return Verdict(
            kind="NO_REFERENCE",
            category="correctness",
            reason=f"no frozen reference for {benchmark.name}",
        )
    candidates_with_artifact = [
        r for r in candidate_runs
        if r.status == "ok" and r.artifact_hash is not None
    ]
    if not candidates_with_artifact:
        return Verdict(
            kind="NEEDS_MORE_DATA",
            category="correctness",
            reason=(
                f"no ok candidate run with an artifact at the current SHA; "
                f"run `bench run {benchmark.name}` first"
            ),
        )
    latest = candidates_with_artifact[-1]
    if latest.artifact_hash == reference.content_hash:
        return Verdict(
            kind="PASS",
            category="correctness",
            reference_hash=reference.content_hash,
            candidate_hash=latest.artifact_hash,
            reason=f"artifact matches reference ({reference.content_hash[:18]}...)",
        )
    return Verdict(
        kind="FAIL",
        category="correctness",
        reference_hash=reference.content_hash,
        candidate_hash=latest.artifact_hash,
        reason=(
            f"artifact hash {latest.artifact_hash[:18]}... "
            f"differs from reference {reference.content_hash[:18]}..."
        ),
    )
