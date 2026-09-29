"""Small-sample statistics for reply rates. Pure Python, no dependencies."""

import math

Z_95 = 1.959964
Z_80_POWER = 0.841621


def wilson(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """95% Wilson score interval for a proportion. Honest at small n, unlike p ± 2·SE."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p-value for the 2x2 table [[a, b], [c, d]].

    Rows are arms, columns are (replied, did not reply).
    """
    row1, row2, col1 = a + b, c + d, a + c
    n = row1 + row2

    def prob(x: int) -> float:
        return math.comb(row1, x) * math.comb(row2, col1 - x) / math.comb(n, col1)

    observed = prob(a)
    lo, hi = max(0, col1 - row2), min(row1, col1)
    return min(1.0, sum(p for x in range(lo, hi + 1) if (p := prob(x)) <= observed * (1 + 1e-9)))


def n_per_arm(p1: float, p2: float) -> int:
    """Sends per arm needed to detect p1 vs p2 (two-sided α=0.05, 80% power)."""
    if p1 == p2:
        return math.inf
    pbar = (p1 + p2) / 2
    num = (Z_95 * math.sqrt(2 * pbar * (1 - pbar))
           + Z_80_POWER * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return math.ceil(num / (p1 - p2) ** 2)


def detectable_rate(baseline: float, n: int) -> float | None:
    """Smallest reply rate that n sends per arm could tell apart from the baseline."""
    baseline = min(max(baseline, 0.01), 0.9)
    for step in range(1, 200):
        p2 = baseline + step * 0.005
        if p2 >= 1:
            return None
        if n_per_arm(baseline, p2) <= n:
            return p2
    return None
