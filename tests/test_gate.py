from __future__ import annotations

from typing import Any

import pytest

from benchstone.gate import evaluate
from benchstone.manifest import Benchmark
from benchstone.store import Baseline, Run


def _bench(**overrides: Any) -> Benchmark:
    base: dict[str, Any] = dict(
        name="B",
        entry_point="B",
        tier="quality",
        deterministic=False,
        metric_direction="minimize",
        expected_runtime_seconds=None,
        threads=1,
        gpu="none",
        background_required=False,
        repetitions=5,
        baseline_seeds=(1, 2, 3, 4, 5),
        promotion_sigma=2.0,
        corpus_path=None,
        corpus_hash=None,
        reference_policy=None,
    )
    base.update(overrides)
    return Benchmark(**base)


def _run(metric: float | None, status: str = "ok", rep: int = 0) -> Run:
    return Run(
        id=0, project="P", benchmark="B", git_sha="sha",
        git_dirty=False, dirty_diff_path=None,
        timestamp="t", harness_version="0.1.0", host="h",
        seed=1, meta_seed=None, repetition_index=rep,
        status=status, metric=metric, metric_components=None,
        wall_clock_seconds=0.0, project_metadata=None,
        stderr_log_path=None,
    )


def _baseline() -> Baseline:
    return Baseline(
        project="P", benchmark="B", git_sha="baseline_sha",
        established_at="t", notes=None,
    )


def test_no_baseline() -> None:
    v = evaluate(_bench(), None, [], [])
    assert v.kind == "NO_BASELINE"


def test_needs_more_baseline_data() -> None:
    bench = _bench()
    v = evaluate(bench, _baseline(), [_run(1.0)], [_run(1.0) for _ in range(5)])
    assert v.kind == "NEEDS_MORE_DATA"
    assert "baseline" in v.reason


def test_needs_more_candidate_data() -> None:
    bench = _bench()
    baseline_runs = [_run(1.0 + 0.01 * i, rep=i) for i in range(5)]
    candidate_runs = [_run(1.0, rep=0)]
    v = evaluate(bench, _baseline(), baseline_runs, candidate_runs)
    assert v.kind == "NEEDS_MORE_DATA"
    assert "candidate" in v.reason


def test_promote_minimize() -> None:
    bench = _bench()  # minimize, sigma threshold 2.0
    baseline = [_run(10.0 + 0.1 * i, rep=i) for i in range(5)]
    candidate = [_run(5.0 + 0.1 * i, rep=i) for i in range(5)]
    v = evaluate(bench, _baseline(), baseline, candidate)
    assert v.kind == "PROMOTE"
    assert v.sigma is not None and v.sigma >= 2.0


def test_reject_minimize_no_improvement() -> None:
    bench = _bench()
    baseline = [_run(10.0 + 0.1 * i, rep=i) for i in range(5)]
    candidate = [_run(10.0 + 0.1 * i, rep=i) for i in range(5)]
    v = evaluate(bench, _baseline(), baseline, candidate)
    assert v.kind == "REJECT"
    assert v.sigma is not None and abs(v.sigma) < 2.0


def test_reject_minimize_regression() -> None:
    bench = _bench()
    baseline = [_run(10.0 + 0.1 * i, rep=i) for i in range(5)]
    candidate = [_run(15.0 + 0.1 * i, rep=i) for i in range(5)]
    v = evaluate(bench, _baseline(), baseline, candidate)
    assert v.kind == "REJECT"
    assert v.sigma is not None and v.sigma < 0


def test_promote_maximize() -> None:
    bench = _bench(metric_direction="maximize")
    baseline = [_run(5.0 + 0.1 * i, rep=i) for i in range(5)]
    candidate = [_run(10.0 + 0.1 * i, rep=i) for i in range(5)]
    v = evaluate(bench, _baseline(), baseline, candidate)
    assert v.kind == "PROMOTE"


def test_errored_runs_excluded_from_count() -> None:
    bench = _bench()
    baseline = [_run(10.0 + 0.1 * i, rep=i) for i in range(5)]
    candidate = (
        [_run(5.0, rep=0)]
        + [_run(None, status="error", rep=i) for i in range(1, 5)]
    )
    v = evaluate(bench, _baseline(), baseline, candidate)
    assert v.kind == "NEEDS_MORE_DATA"
    assert "candidate" in v.reason


def test_correctness_tier_raises() -> None:
    bench = _bench(
        tier="correctness", deterministic=True, metric_direction=None,
        promotion_sigma=None, reference_policy="byte_equivalence",
    )
    with pytest.raises(NotImplementedError):
        evaluate(bench, _baseline(), [], [])


def test_deterministic_non_correctness_raises() -> None:
    bench = _bench(deterministic=True)
    with pytest.raises(NotImplementedError):
        evaluate(bench, _baseline(), [], [])
