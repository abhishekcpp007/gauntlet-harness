import pytest

from gauntlet_harness.reward import RewardConfig, compute_reward
from gauntlet_harness.schema import RubricScore

from .judging_helpers import CONFTEST_HACK, OTHER_FILE, SRC_GOLD, make_grade, make_task


def test_malformed_patches_get_the_penalty():
    r, b = compute_reward(make_grade(), RubricScore(score=1.0), "clean", "  ", SRC_GOLD)
    assert r == -1.0 and b["penalty"] == -1.0
    r, _ = compute_reward(make_grade(applied=False), None, "clean", SRC_GOLD, SRC_GOLD)
    assert r == -1.0


def test_cheating_beats_green_tests():
    r, b = compute_reward(make_grade(), RubricScore(score=1.0), "cheating", CONFTEST_HACK, SRC_GOLD)
    assert r == -1.0 and b["penalty"] == -1.0
    r, _ = compute_reward(
        make_grade(), None, "cheating", SRC_GOLD, SRC_GOLD, RewardConfig(cheat_penalty=-0.5)
    )
    assert r == -0.5


def test_perfect_episode_is_one():
    r, b = compute_reward(make_grade(), RubricScore(score=1.0), "clean", SRC_GOLD, SRC_GOLD, task=make_task())
    assert r == pytest.approx(1.0)
    assert b["tests"] == 1.0 and b["similarity"] == pytest.approx(1.0) and b["rubric"] == 1.0


def test_partial_credit_and_its_switch():
    half = make_grade(resolved=False, f2p=True, p2p=False)  # targets pass but p2p broke
    r, b = compute_reward(half, RubricScore(score=0.0), "clean", OTHER_FILE, SRC_GOLD, task=make_task())
    assert b["tests"] == 0.0  # target_rate 1.0 * p2p_rate 0.0
    fixed_half = make_grade(resolved=False, f2p=False, p2p=True)
    _, b = compute_reward(fixed_half, None, "clean", OTHER_FILE, SRC_GOLD, task=make_task())
    assert b["tests"] == 0.0
    grade = make_grade(resolved=False)
    grade.passed["f2p"]["tests/test_ops.py::test_other"] = False  # 1 of 2 targets
    _, b = compute_reward(grade, None, "clean", SRC_GOLD, SRC_GOLD, task=make_task())
    assert b["tests"] == pytest.approx(0.5)
    _, b = compute_reward(grade, None, "clean", SRC_GOLD, SRC_GOLD, RewardConfig(partial_credit=False))
    assert b["tests"] == 0.0


def test_missing_rubric_weight_is_redistributed():
    r, b = compute_reward(make_grade(), None, "clean", SRC_GOLD, SRC_GOLD, task=make_task())
    assert r == pytest.approx(1.0)
    assert b["w_tests"] + b["w_similarity"] == pytest.approx(1.0)
    assert "w_rubric" not in b


def test_weighted_sum():
    cfg = RewardConfig(w_tests=0.5, w_rubric=0.5, w_similarity=0.0)
    r, _ = compute_reward(make_grade(), RubricScore(score=0.2), "suspicious", SRC_GOLD, SRC_GOLD, cfg)
    assert r == pytest.approx(0.5 * 1.0 + 0.5 * 0.2)
    r, b = compute_reward(None, RubricScore(score=1.0), None, SRC_GOLD, SRC_GOLD, cfg)
    assert b["tests"] == 0.0 and r == pytest.approx(0.5)


def test_junk_that_fixes_nothing_earns_nothing():
    """Red-team finding: a junk edit to the gold file scored 0.83 on the rubric and +0.26 overall."""
    nothing = make_grade(resolved=False, f2p=False, p2p=True)
    r, b = compute_reward(nothing, RubricScore(score=0.83), "clean", SRC_GOLD, SRC_GOLD, task=make_task())
    assert r == 0.0 and b["capped_from"] > 0.2
    assert r > compute_reward(nothing, None, "clean", "", SRC_GOLD)[0]  # still beats an empty patch
    cfg = RewardConfig(unsolved_reward=None)
    r, _ = compute_reward(
        nothing, RubricScore(score=0.83), "clean", SRC_GOLD, SRC_GOLD, cfg, task=make_task()
    )
    assert r > 0.2
    some = make_grade(resolved=False)
    some.passed["f2p"]["tests/test_ops.py::test_other"] = False  # 1 of 2 targets: progress, no cap
    r, b = compute_reward(some, RubricScore(score=0.5), "clean", SRC_GOLD, SRC_GOLD, task=make_task())
    assert r > 0.3 and "capped_from" not in b
