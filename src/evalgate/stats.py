"""The few statistics the gate needs, with no dependencies."""

from math import comb, sqrt

ALPHA = 0.05


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes out of n (95% by default)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_one_sided(b: int, c: int) -> float:
    """Exact one-sided McNemar p-value that the PR is worse than the base.

    b = cases the base got right and the PR got wrong, c = the other way round. Under "no
    difference" each discordant case is a coin flip, so this is P(X >= b) for X ~ Bin(b + c, 0.5).
    """
    n = b + c
    if n == 0:
        return 1.0
    return sum(comb(n, i) for i in range(b, n + 1)) / 2**n


def min_regressions(alpha: float = ALPHA) -> int:
    """Smallest b that fails the gate when c = 0, i.e. the best case for detecting a drop."""
    b = 1
    while mcnemar_one_sided(b, 0) >= alpha:
        b += 1
    return b


def verdict(base_correct: int, head_correct: int, p_value: float, alpha: float = ALPHA) -> str:
    return "FAIL" if head_correct < base_correct and p_value < alpha else "PASS"
