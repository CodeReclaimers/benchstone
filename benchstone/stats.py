from __future__ import annotations

import math
from collections.abc import Sequence


def mean_se(xs: Sequence[float]) -> tuple[float, float]:
    """Sample mean and standard error of the mean.

    Uses Bessel-corrected sample variance (divisor n-1). Requires at least two
    samples; callers needing a count-based early-exit should do so before calling.
    """
    n = len(xs)
    if n < 2:
        raise ValueError(f"mean_se requires at least 2 samples, got {n}")
    mean = sum(xs) / n
    variance = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return mean, math.sqrt(variance / n)


def directed_sigma(
    baseline: Sequence[float],
    candidate: Sequence[float],
    direction: str,
) -> float:
    """Signed sigma such that positive values indicate candidate improvement.

    For ``direction="minimize"``, a decrease in candidate metric versus baseline
    is an improvement (positive sigma). For ``direction="maximize"``, an increase
    is an improvement. When both samples have zero variance and identical means
    the function returns 0.0; when variances are both zero but means differ it
    returns +inf or -inf to reflect a deterministic shift.
    """
    if direction not in ("minimize", "maximize"):
        raise ValueError(
            f"direction must be 'minimize' or 'maximize', got {direction!r}"
        )
    mb, seb = mean_se(baseline)
    mc, sec = mean_se(candidate)
    denom = math.sqrt(sec * sec + seb * seb)
    if denom == 0.0:
        if mc == mb:
            return 0.0
        signed_inf = math.inf if mc > mb else -math.inf
        return -signed_inf if direction == "minimize" else signed_inf
    raw = (mc - mb) / denom
    return -raw if direction == "minimize" else raw


def mann_whitney_z(
    baseline: Sequence[float],
    candidate: Sequence[float],
    direction: str,
) -> float:
    """Direction-adjusted z-score from the two-sample Mann-Whitney U test.

    Positive values indicate candidate improvement. The test ranks combined
    samples and looks only at their ordering, so outlier *magnitude* in either
    sample doesn't inflate or deflate the result — the answer depends on how
    many candidate samples beat how many baseline samples, nothing else.

    With equal group sizes n, |z| is bounded by ``sqrt(3n^2/(2n+1))``:
    ~2.61 at n=5, ~3.78 at n=10, ~5.13 at n=18. Promotion thresholds
    calibrated for parametric sigma may need re-tuning for this policy;
    the gate documentation discusses the interaction.

    Ties are handled by the usual average-rank method, but tie correction
    is not applied to the denominator (negligible for continuous metrics).
    """
    if direction not in ("minimize", "maximize"):
        raise ValueError(
            f"direction must be 'minimize' or 'maximize', got {direction!r}"
        )
    n1 = len(baseline)
    n2 = len(candidate)
    if n1 < 2 or n2 < 2:
        raise ValueError(
            f"mann_whitney_z requires at least 2 samples per group, "
            f"got n_baseline={n1} n_candidate={n2}"
        )

    # Combine, sort, assign ranks (average-rank ties). Group flag identifies
    # which sample each value belonged to.
    combined = [(x, 0) for x in baseline] + [(x, 1) for x in candidate]
    combined.sort(key=lambda t: t[0])
    ranks = [0.0] * len(combined)
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j + 2) / 2.0  # 1-indexed positions
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1

    rank_sum_baseline = sum(r for r, (_, g) in zip(ranks, combined) if g == 0)
    u1 = rank_sum_baseline - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    sd = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
    if sd == 0.0:
        return 0.0
    z = (u1 - mu) / sd
    # Higher rank_sum_baseline means baseline samples are larger than candidate
    # samples, i.e. candidate samples are smaller. For direction='minimize',
    # smaller candidate is an improvement, so positive z already encodes that.
    return z if direction == "minimize" else -z
