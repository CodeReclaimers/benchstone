from __future__ import annotations

import math

import pytest

from benchstone.stats import directed_z, mann_whitney_z, mean_se


def test_mean_se_basic() -> None:
    mean, se = mean_se([1.0, 2.0, 3.0, 4.0, 5.0])
    assert mean == pytest.approx(3.0)
    # sample variance = 2.5, SE = sqrt(2.5 / 5) = sqrt(0.5)
    assert se == pytest.approx(math.sqrt(0.5))


def test_mean_se_requires_two_samples() -> None:
    with pytest.raises(ValueError, match="at least 2 samples"):
        mean_se([1.0])
    with pytest.raises(ValueError, match="at least 2 samples"):
        mean_se([])


def test_directed_z_minimize_improvement_positive() -> None:
    baseline = [10.0, 10.1, 9.9, 10.05, 9.95]
    candidate = [9.0, 9.1, 8.9, 9.05, 8.95]  # lower is better for minimize
    sigma = directed_z(baseline, candidate, "minimize")
    assert sigma > 0


def test_directed_z_maximize_improvement_positive() -> None:
    baseline = [10.0, 10.1, 9.9, 10.05, 9.95]
    candidate = [11.0, 11.1, 10.9, 11.05, 10.95]  # higher is better for maximize
    sigma = directed_z(baseline, candidate, "maximize")
    assert sigma > 0


def test_directed_z_regression_negative() -> None:
    baseline = [10.0, 10.1, 9.9, 10.05, 9.95]
    candidate = [11.0, 11.1, 10.9, 11.05, 10.95]  # worse for minimize
    sigma = directed_z(baseline, candidate, "minimize")
    assert sigma < 0


def test_directed_z_identical_samples_zero() -> None:
    xs = [1.0, 2.0, 3.0]
    assert directed_z(xs, xs, "minimize") == 0.0


def test_directed_z_zero_variance_differing_means() -> None:
    baseline = [1.0, 1.0, 1.0]
    candidate = [2.0, 2.0, 2.0]
    # worse for minimize, better for maximize
    assert directed_z(baseline, candidate, "minimize") == -math.inf
    assert directed_z(baseline, candidate, "maximize") == math.inf


def test_directed_z_rejects_bad_direction() -> None:
    with pytest.raises(ValueError, match="direction must be"):
        directed_z([1.0, 2.0], [1.0, 2.0], "sideways")


# --- Mann-Whitney z ---


def test_mann_whitney_clear_improvement_minimize() -> None:
    baseline = [10.0, 11.0, 12.0, 13.0, 14.0]
    candidate = [5.0, 6.0, 7.0, 8.0, 9.0]  # strictly smaller, so improvement
    z = mann_whitney_z(baseline, candidate, "minimize")
    assert z == pytest.approx(2.611, abs=0.01)


def test_mann_whitney_clear_regression_minimize() -> None:
    baseline = [5.0, 6.0, 7.0, 8.0, 9.0]
    candidate = [10.0, 11.0, 12.0, 13.0, 14.0]  # strictly larger, regression
    z = mann_whitney_z(baseline, candidate, "minimize")
    assert z == pytest.approx(-2.611, abs=0.01)


def test_mann_whitney_direction_flips_sign() -> None:
    baseline = [10.0, 11.0, 12.0, 13.0, 14.0]
    candidate = [5.0, 6.0, 7.0, 8.0, 9.0]
    z_min = mann_whitney_z(baseline, candidate, "minimize")
    z_max = mann_whitney_z(baseline, candidate, "maximize")
    assert z_min == pytest.approx(-z_max)


def test_mann_whitney_no_separation_near_zero() -> None:
    # Symmetrically interleaved samples: half the candidates beat their
    # paired baseline, half lose. Rank sums are close to equal -> |z| small.
    baseline = [1.0, 3.0, 5.0, 7.0, 9.0]
    candidate = [0.5, 3.5, 4.5, 8.0, 9.5]
    z = mann_whitney_z(baseline, candidate, "minimize")
    assert abs(z) < 0.2


def test_mann_whitney_immune_to_outlier_magnitude() -> None:
    """Changing a baseline value from 1.832 to 1e6 moves sigma but not z.

    This is the Arborist pathology: parametric sigma inflates wildly with
    outliers, while the rank-based statistic only cares that the outlier
    is still the largest value.
    """
    baseline_mild = [1.248, 1.324, 1.254, 1.261, 1.832]
    baseline_extreme = [1.248, 1.324, 1.254, 1.261, 1_000_000.0]
    candidate = [1.10, 1.12, 1.14, 1.16, 1.18]
    z_mild = mann_whitney_z(baseline_mild, candidate, "minimize")
    z_extreme = mann_whitney_z(baseline_extreme, candidate, "minimize")
    assert z_mild == pytest.approx(z_extreme)


def test_mann_whitney_handles_ties() -> None:
    """Ties use average ranks; a value appearing on both sides contributes
    half its rank to each group."""
    baseline = [1.0, 2.0, 3.0, 4.0, 5.0]
    candidate = [1.0, 2.0, 3.0, 4.0, 5.0]  # identical sets
    z = mann_whitney_z(baseline, candidate, "minimize")
    assert z == pytest.approx(0.0)


def test_mann_whitney_requires_two_samples_per_group() -> None:
    with pytest.raises(ValueError, match="at least 2 samples"):
        mann_whitney_z([1.0], [2.0, 3.0], "minimize")
    with pytest.raises(ValueError, match="at least 2 samples"):
        mann_whitney_z([1.0, 2.0], [3.0], "minimize")


def test_mann_whitney_rejects_bad_direction() -> None:
    with pytest.raises(ValueError, match="direction must be"):
        mann_whitney_z([1.0, 2.0], [3.0, 4.0], "sideways")
