"""Deterministic interval estimates and exact tests for small binary samples."""

import random
from array import array
from fractions import Fraction
from functools import cache, lru_cache
from itertools import accumulate, repeat
from math import comb, sqrt, sumprod
from operator import mul
from statistics import NormalDist
from typing import NamedTuple

Z95 = NormalDist().inv_cdf(0.975)
# Boschloo's supremum over the shared rate: a grid of GRID + 1 evenly spaced rates, 0 and 1 included, then a
# golden-section search between the neighbours of every local maximum of the grid.
GRID = 10_000
GOLDEN = (sqrt(5) - 1) / 2
REFINE_STEPS = 80


def wilson(k: int, n: int, z: float = Z95) -> list[float] | None:
    """Wilson score interval for k successes in n trials; null without trials. Its bounds are exactly 0 at k = 0 and
    exactly 1 at k = n, where the formula's rounding leaves a residue."""
    if not n:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [0.0 if k == 0 else max(0.0, centre - half), 1.0 if k == n else min(1.0, centre + half)]


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


def fisher_exact(k1: int, n1: int, k2: int, n2: int) -> float | None:
    """Two-sided Fisher exact p of k1 successes in n1 against k2 in n2: the summed probability of every table with the
    same margins that is no more likely than the observed one (ties within a relative 1e-7), in exact integer
    arithmetic. Null when a group is empty."""
    if not n1 or not n2:
        return None
    total = k1 + k2
    weights = [comb(n1, a) * comb(n2, total - a) for a in range(max(0, total - n2), min(n1, total) + 1)]
    observed = comb(n1, k1) * comb(n2, k2)
    tail = sum(w for w in weights if w * 10**7 <= observed * (10**7 + 1))
    return min(1.0, tail / comb(n1 + n2, total))


def boschloo_exact(k1: int, n1: int, k2: int, n2: int) -> float | None:
    """Boschloo's exact unconditional two-sided p of k1 successes in n1 against k2 in n2, with tables ordered by the
    two-sided Fisher p: the largest probability, over a success rate pi shared by both groups, of the tables (any y1
    of n1 against any y2 of n2) whose Fisher p is at most the observed table's, ties included. This variant differs
    from doubling the smaller one-sided p. Fisher's p is `fisher_exact`'s, kept as an exact fraction with its own tie
    rule (within a margin, weights within a relative 1e-7 of the observed one count as equal; no margin of groups up
    to 40 + 40 holds two unequal weights that close, so there it equals exact ties); the p values of tables in
    different margins are compared exactly, never as floats. The supremum is taken on a grid of GRID + 1 rates and
    refined around each of its local maxima (`boschloo_supremum`), and the value returned is the exact probability at
    the best rate found, so it is never above Fisher's p, which bounds the probability at every rate. Null when a
    group is empty."""
    if not n1 or not n2:
        return None
    return boschloo_supremum(boschloo_region(k1, n1, k2, n2)).p


@cache
def fisher_levels(n1: int, n2: int) -> tuple[tuple[Fraction, ...], ...]:
    """The two-sided Fisher p of every table of n1 against n2 as an exact fraction, `levels[y1][y2]`, by
    `fisher_exact`'s rule."""
    levels = [[Fraction(0)] * (n2 + 1) for _ in range(n1 + 1)]
    for total in range(n1 + n2 + 1):
        tables = range(max(0, total - n2), min(n1, total) + 1)
        weights = [comb(n1, a) * comb(n2, total - a) for a in tables]
        for a, observed in zip(tables, weights, strict=True):
            tail = sum(w for w in weights if w * 10**7 <= observed * (10**7 + 1))
            levels[a][total - a] = Fraction(tail, comb(n1 + n2, total))
    return tuple(map(tuple, levels))


def boschloo_region(k1: int, n1: int, k2: int, n2: int) -> tuple[int, ...]:
    """The tables whose Fisher p is at most the observed table's, as one coefficient for each total s of successes:
    the sum of C(n1, y1) * C(n2, y2) over those tables with y1 + y2 = s. Their probability at a shared rate pi is
    the sum over s of the coefficient times pi^s * (1 - pi)^(n1 + n2 - s)."""
    levels = fisher_levels(n1, n2)
    observed = levels[k1][k2]
    coefficients = [0] * (n1 + n2 + 1)
    for y1, row in enumerate(levels):
        for y2, level in enumerate(row):
            if level <= observed:
                coefficients[y1 + y2] += comb(n1, y1) * comb(n2, y2)
    return tuple(coefficients)


class Supremum(NamedTuple):
    p: float
    rate: float | None
    grid: float
    local_maxima: int


@lru_cache(maxsize=4096)
def boschloo_supremum(coefficients: tuple[int, ...]) -> Supremum:
    """The supremum over pi in [0, 1] of sum_s coefficients[s] * pi^s * (1 - pi)^(n - s), each coefficient at most
    C(n, s). Every local maximum of the grid (a rate above its left neighbour and not below its right one, the ends
    against their one neighbour) is refined by a golden-section search between its two grid neighbours. Each grid
    maximum and each refined rate within a relative 1e-9 of the best is evaluated again in exact arithmetic, and the
    largest exact value is `p`, at the rate `rate`. `grid` is the exact value at the best grid rate, so `p - grid` is
    what the refinement added; `local_maxima` is how many were refined. With every coefficient at C(n, s) the region
    holds every table, the sum is 1 at every rate and `p` is 1 without a search."""
    degree = len(coefficients) - 1
    if all(c == comb(degree, s) for s, c in enumerate(coefficients)):
        return Supremum(p=1.0, rate=None, grid=1.0, local_maxima=0)
    weights = [float(c) for c in coefficients]
    values = [sumprod(weights, row) for row in rate_grid(degree)]

    def value(pi: float) -> float:
        return sumprod(weights, powers(pi, degree))

    peaks = [i for i, v in enumerate(values) if (i == 0 or v > values[i - 1]) and (i == GRID or v >= values[i + 1])]
    on_grid, refined = [(values[i], i / GRID) for i in peaks], []
    for i in peaks:
        low, high = max(0, i - 1) / GRID, min(GRID, i + 1) / GRID
        left, right = high - GOLDEN * (high - low), low + GOLDEN * (high - low)
        at_left, at_right = value(left), value(right)
        best = max((at_left, left), (at_right, right))
        for _ in range(REFINE_STEPS):
            if at_left >= at_right:
                high, right, at_right = right, left, at_left
                left = high - GOLDEN * (high - low)
                at_left = value(left)
            else:
                low, left, at_left = left, right, at_right
                right = low + GOLDEN * (high - low)
                at_right = value(right)
            best = max(best, (at_left, left), (at_right, right))
        refined.append(best)

    def exact_best(found: list[tuple[float, float]]) -> tuple[Fraction, float]:
        top = max(v for v, _ in found)
        return max((exact_value(coefficients, pi), pi) for v, pi in found if v >= top * (1 - 1e-9))

    grid = exact_best(on_grid)
    p, rate = max(grid, exact_best(refined))
    return Supremum(p=float(p), rate=rate, grid=float(grid[0]), local_maxima=len(peaks))


@lru_cache(maxsize=8)
def rate_grid(degree: int) -> tuple[array, ...]:
    """pi^s * (1 - pi)^(degree - s) for s = 0..degree at each rate pi = i / GRID of the grid."""
    return tuple(powers(i / GRID, degree) for i in range(GRID + 1))


def powers(pi: float, degree: int) -> array:
    up = accumulate(repeat(pi, degree), mul, initial=1.0)
    down = list(accumulate(repeat(1.0 - pi, degree), mul, initial=1.0))
    return array("d", map(mul, up, reversed(down)))


def exact_value(coefficients: tuple[int, ...], pi: float) -> Fraction:
    """sum_s coefficients[s] * pi^s * (1 - pi)^(n - s) in exact arithmetic, pi read as the binary fraction it is."""
    rate = Fraction(pi)
    up, down, scale = rate.numerator, rate.denominator - rate.numerator, rate.denominator
    degree = len(coefficients) - 1
    ups = list(accumulate(repeat(up, degree), mul, initial=1))
    downs = list(accumulate(repeat(down, degree), mul, initial=1))
    return Fraction(sum(c * ups[s] * downs[degree - s] for s, c in enumerate(coefficients)), scale**degree)


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
