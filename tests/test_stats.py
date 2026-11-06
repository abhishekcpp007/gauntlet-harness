"""The statistics behind the report, checked against values worked out by hand."""

from __future__ import annotations

from gauntlet_harness.stats import bootstrap_pass_at_k, mcnemar, pass_at_k_per_task, wilson


def test_wilson_matches_known_values():
    # 1 of 10 and 9 of 10 are the cases the textbook normal interval gets wrong
    lo, hi = wilson(1, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.018, 0.404)
    lo, hi = wilson(9, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.596, 0.982)


def test_wilson_stays_inside_the_unit_range_at_the_extremes():
    assert wilson(0, 20)[0] == 0.0
    assert wilson(20, 20)[1] == 1.0
    assert wilson(0, 0) is None


def test_wilson_narrows_as_the_sample_grows():
    def width(k, n):
        lo, hi = wilson(k, n)
        return hi - lo

    assert width(5, 10) > width(50, 100) > width(500, 1000)


def test_a_rate_off_53_tasks_is_wide():
    # the point of the whole module: 22 of 53 reads as "41.5%", and the honest version of that
    # number spans 25 points, which is wider than most of the gaps people argue about
    lo, hi = wilson(22, 53)
    assert (lo, hi) == (0.2926, 0.5491)
    assert hi - lo > 0.25


def test_pass_at_k_per_task():
    assert pass_at_k_per_task([True, False], 1) == 0.5
    assert pass_at_k_per_task([False, False], 1) == 0.0
    assert pass_at_k_per_task([True, False], 2) == 1.0  # one of the two attempts resolved
    assert pass_at_k_per_task([False], 2) is None  # fewer attempts than k


def test_bootstrap_brackets_the_point_estimate():
    per_task = [[True]] * 20 + [[False]] * 20
    lo, hi = bootstrap_pass_at_k(per_task, 1, iters=2000, seed=1)
    assert lo < 0.5 < hi


def test_bootstrap_is_reproducible():
    per_task = [[True], [False], [True], [True], [False], [True], [False], [True]]
    assert bootstrap_pass_at_k(per_task, 1, iters=500, seed=7) == bootstrap_pass_at_k(
        per_task, 1, iters=500, seed=7
    )


def test_bootstrap_collapses_when_every_task_agrees():
    assert bootstrap_pass_at_k([[True]] * 12, 1, iters=500, seed=0) == (1.0, 1.0)


def test_bootstrap_needs_more_than_one_task():
    assert bootstrap_pass_at_k([[True]], 1) is None
    assert bootstrap_pass_at_k([], 1) is None


def test_mcnemar_ignores_the_tasks_both_got_right():
    a = {"t1": True, "t2": True, "t3": False}
    b = {"t1": True, "t2": True, "t3": False}
    out = mcnemar(a, b)
    assert out == {
        "tasks": 3,
        "a_only": 0,
        "b_only": 0,
        "p_value": 1.0,
        "significant": False,
        "min_p": 1.0,
        "underpowered": True,  # nothing disagreed, so nothing could have been found
    }


def test_mcnemar_small_disagreement_is_not_significant():
    # 3 against 1 is the sort of gap that looks real on a leaderboard and is not
    a = {f"t{i}": i < 4 for i in range(20)}
    b = {f"t{i}": i in (4, 5, 6, 7) for i in range(20)}
    out = mcnemar(a, b)
    assert out["a_only"] == 4 and out["b_only"] == 4
    assert out["p_value"] == 1.0 and not out["significant"]


def test_mcnemar_one_sided_sweep_is_significant():
    a = {f"t{i}": True for i in range(10)}
    b = {f"t{i}": False for i in range(10)}
    out = mcnemar(a, b)
    # 10 against 0 is 2 * 0.5**10
    assert out["a_only"] == 10 and out["b_only"] == 0
    assert out["p_value"] == round(2 * 0.5**10, 4) and out["significant"]


def test_mcnemar_is_symmetric_in_its_p_value():
    a = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": True}
    b = {"t1": False, "t2": True, "t3": True, "t4": False, "t5": False}
    forward, backward = mcnemar(a, b), mcnemar(b, a)
    assert forward["p_value"] == backward["p_value"]
    assert forward["a_only"] == backward["b_only"]


def test_mcnemar_uses_only_the_shared_tasks():
    a = {"t1": True, "t2": False, "only_a": True}
    b = {"t1": False, "t2": False, "only_b": True}
    out = mcnemar(a, b)
    assert out["tasks"] == 2 and out["a_only"] == 1 and out["b_only"] == 0


def test_mcnemar_needs_an_overlap():
    assert mcnemar({"a": True}, {"b": True}) is None


# --------------------------------------------- not significant vs could not have been


def test_a_perfect_sweep_of_four_tasks_cannot_reach_significance():
    """The calibration case: the oracle beats a cheater 4-0 and the test still cannot call it."""
    a = {f"t{i}": True for i in range(4)}
    b = {f"t{i}": False for i in range(4)}
    out = mcnemar(a, b)
    assert (out["a_only"], out["b_only"]) == (4, 0)
    assert out["p_value"] == 0.125 and not out["significant"]
    assert out["underpowered"] and out["min_p"] == 0.125


def test_six_disagreements_is_where_significance_becomes_possible():
    for n, powered in ((5, False), (6, True)):
        a = {f"t{i}": True for i in range(n)}
        b = {f"t{i}": False for i in range(n)}
        out = mcnemar(a, b)
        assert out["underpowered"] is not powered, (n, out)
    # and at six a clean sweep does land
    a = {f"t{i}": True for i in range(6)}
    b = {f"t{i}": False for i in range(6)}
    assert mcnemar(a, b)["significant"]


def test_a_real_finding_is_not_called_underpowered():
    a = {f"t{i}": i < 9 for i in range(20)}
    b = {f"t{i}": i >= 9 and i < 10 for i in range(20)}
    out = mcnemar(a, b)
    assert out["significant"] and not out["underpowered"]


def test_plenty_of_disagreement_and_no_difference_is_its_own_answer():
    """Eight each way: the test had the power to find a gap and did not."""
    a = {f"t{i}": i < 8 for i in range(16)}
    b = {f"t{i}": i >= 8 for i in range(16)}
    out = mcnemar(a, b)
    assert (out["a_only"], out["b_only"]) == (8, 8)
    assert not out["significant"] and not out["underpowered"]


def test_min_p_floors_at_one_when_nothing_disagreed():
    a = b = {"t1": True, "t2": False}
    out = mcnemar(a, b)
    assert out["min_p"] == 1.0 and out["underpowered"]
