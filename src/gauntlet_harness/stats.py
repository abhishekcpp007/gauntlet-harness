"""Uncertainty and significance for the report.

A resolve rate off 53 tasks is an estimate, not a measurement. "41.5%" and "47.2%" look like a
gap until you notice both intervals run from roughly a quarter to three fifths. Everything here
exists so a number in the report comes with how much it is worth.

Two choices worth stating:

*Bootstrap over tasks, not episodes.* With several attempts per task the episodes are clustered,
and treating them as independent Bernoulli trials makes the interval far too narrow. Resampling
whole tasks keeps the clustering. The resample is seeded, so a report is reproducible.

*McNemar's exact test, not a two-proportion z-test.* Both policies run the same tasks, so the
samples are paired and only the tasks where they disagree carry information. At these sample
sizes the disagreement count is small, so the exact binomial is used rather than the chi-square
approximation.
"""

from __future__ import annotations

import random
from math import comb, sqrt

Z95 = 1.959963984540054


def wilson(successes: int, trials: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation here because the counts are small and the rates sit
    near 0 or 1 often enough that the textbook interval leaves the unit range.
    """
    if trials <= 0:
        return None
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z / denominator * sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4))


def pass_at_k_per_task(outcomes: list[bool], k: int) -> float | None:
    """The unbiased pass@k estimator for one task (Chen et al., 2021)."""
    n, c = len(outcomes), sum(outcomes)
    if n < k:
        return None
    return 1.0 if n - c < k else 1.0 - comb(n - c, k) / comb(n, k)


def bootstrap_pass_at_k(
    per_task: list[list[bool]], k: int, *, iters: int = 10000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float] | None:
    """Percentile bootstrap interval for pass@k, resampling tasks with replacement."""
    scores = [s for outcomes in per_task if (s := pass_at_k_per_task(outcomes, k)) is not None]
    if len(scores) < 2:
        return None
    rng = random.Random(seed)
    n = len(scores)
    draws = sorted(sum(rng.choice(scores) for _ in range(n)) / n for _ in range(iters))
    lo = draws[int((alpha / 2) * iters)]
    hi = draws[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (round(lo, 4), round(hi, 4))


def mcnemar(a: dict[str, bool], b: dict[str, bool], *, alpha: float = 0.05) -> dict[str, object] | None:
    """Exact two-sided McNemar test over the tasks both policies attempted.

    ``a_only`` counts tasks the first policy solved and the second did not, ``b_only`` the
    reverse. Tasks they agree on carry no information about which is better and drop out, which
    is the whole point of pairing.
    """
    shared = sorted(set(a) & set(b))
    if not shared:
        return None
    a_only = sum(1 for t in shared if a[t] and not b[t])
    b_only = sum(1 for t in shared if b[t] and not a[t])
    disagreements = a_only + b_only
    if disagreements == 0:
        p = 1.0
    else:
        smaller = min(a_only, b_only)
        tail = sum(comb(disagreements, i) for i in range(smaller + 1)) / (2**disagreements)
        p = min(1.0, 2 * tail)
    # "not significant" and "could not have been significant" are different statements, and only
    # one of them is about the models. With d disagreements the smallest two-sided p the exact
    # test can return is 2 * 0.5**d, so below six disagreements no outcome at all reaches 0.05,
    # however lopsided it looks. A calibration run where the oracle beats a cheater four to
    # nothing lands here: the sweep is perfect and the test still cannot call it.
    floor = min(1.0, 2 * 0.5**disagreements) if disagreements else 1.0
    return {
        "tasks": len(shared),
        "a_only": a_only,
        "b_only": b_only,
        "p_value": round(p, 4),
        "significant": p < alpha,
        "min_p": round(floor, 4),
        "underpowered": floor >= alpha,
    }
