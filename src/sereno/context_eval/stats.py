"""Deterministic interval estimates and exact tests for small binary samples."""

import random
from math import comb, sqrt
from statistics import NormalDist

Z95 = NormalDist().inv_cdf(0.975)


def wilson(k: int, n: int, z: float = Z95) -> list[float] | None:
    """Wilson score interval for k successes in n trials; null without trials."""
    if not n:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [max(0.0, centre - half), min(1.0, centre + half)]


def rate(k: int, n: int, unknown: int = 0) -> dict:
    """A proportion that always carries its numerator, denominator and the eligible sessions left unknown."""
    return {"k": k, "n": n, "rate": k / n if n else None, "ci95": wilson(k, n), "unknown": unknown}


def newcombe(k1: int, n1: int, k2: int, n2: int, z: float = Z95) -> dict:
    """Difference p1 - p2 with Newcombe's hybrid score interval (method 10)."""
    if not n1 or not n2:
        return {"difference": None, "ci95": None}
    p1, p2 = k1 / n1, k2 / n2
    (l1, u1), (l2, u2) = wilson(k1, n1, z), wilson(k2, n2, z)
    difference = p1 - p2
    return {
        "difference": difference,
        "ci95": [
            difference - sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
            difference + sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2),
        ],
    }


def mcnemar_exact(b: int, c: int) -> float | None:
    """Two-sided exact McNemar p-value from the discordant pairs; null without discordant pairs."""
    n = b + c
    if not n:
        return None
    tail = sum(comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def pass_power_k(c: int, n: int, k: int) -> float:
    """Unbiased probability that k distinct runs drawn from n, c of them successful, all succeed."""
    return comb(c, k) / comb(n, k)


def any_in_k(c: int, n: int, k: int) -> float:
    """Unbiased probability that at least one of k distinct runs drawn from n succeeds."""
    return 1 - comb(n - c, k) / comb(n, k)


def cluster_bootstrap(clusters: list[tuple[int, int]], replicates: int, seed: int) -> list[float] | None:
    """Percentile 95% interval of the pooled rate sum(k)/sum(n), resampling whole clusters with replacement."""
    clusters = [cluster for cluster in clusters if cluster[1]]
    if len(clusters) < 2 or replicates < 1:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        sample = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        estimates.append(sum(k for k, _ in sample) / sum(n for _, n in sample))
    estimates.sort()
    return [percentile(estimates, 0.025), percentile(estimates, 0.975)]


def percentile(ordered: list[float], q: float) -> float:
    """Linear interpolation between closest ranks."""
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
