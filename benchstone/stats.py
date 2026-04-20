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
