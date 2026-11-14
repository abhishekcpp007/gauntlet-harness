from datetime import date

import pytest

from gauntlet_harness.report import pass_at_k, render_html, render_markdown, summarize
from gauntlet_harness.schema import EpisodeResult, GradeResult


def ep(task, policy, attempt, resolved, *, cheat="clean", merged="2026-03-01T00:00:00Z", reward=None):
    grade = GradeResult(
        instance_id=task,
        patch_applied=True,
        resolved=resolved,
        passed={"f2p": {"t1": resolved}, "n2p": {"t2": resolved}, "s2p": {}, "p2p": {"t3": True}},
    )
    return EpisodeResult(
        episode_id=f"{task}-{policy}-{attempt}",
        instance_id=task,
        policy=policy,
        attempt=attempt,
        grade=grade,
        cheat_verdict=cheat,
        reward=reward if reward is not None else (1.0 if resolved else 0.0),
        merged_at=merged,
    )


def test_pass_at_k_unbiased():
    # one task, 4 attempts, 1 success: pass@1 = 1/4, pass@2 = 1 - C(3,2)/C(4,2) = 0.5
    rs = [ep("a", "m", i, i == 0) for i in range(4)]
    assert pass_at_k(rs, 1) == 0.25
    assert pass_at_k(rs, 2) == 0.5
    assert pass_at_k(rs, 5) is None  # not enough attempts


def test_cheating_never_counts():
    rs = [ep("a", "m", 0, True, cheat="cheating")]
    assert pass_at_k(rs, 1) == 0.0
    row = summarize(rs)["policies"][0]
    assert row["resolved_rate"] == 0.0 and row["cheating_rate"] == 1.0


def test_summary_rates_and_ranking():
    rs = [
        ep("a", "good", 0, True),
        ep("b", "good", 0, True),
        ep("a", "bad", 0, False),
        ep("b", "bad", 0, True),
    ]
    s = summarize(rs)
    assert [r["policy"] for r in s["policies"]] == ["good", "bad"]
    good, bad = s["policies"]
    assert good["f2p_rate"] == 1.0 and good["n2p_rate"] == 1.0 and good["p2p_rate"] == 1.0
    assert bad["f2p_rate"] == 0.5 and bad["s2p_rate"] is None


def test_cutoff_split_spots_contamination():
    rs = [
        ep("old1", "m", 0, True, merged="2025-01-01T00:00:00Z"),
        ep("old2", "m", 0, True, merged="2025-02-01T00:00:00Z"),
        ep("new1", "m", 0, False, merged="2026-08-01T00:00:00Z"),
        ep("new2", "m", 0, False, merged="2026-09-01T00:00:00Z"),
    ]
    row = summarize(rs, cutoff=date(2026, 1, 1))["policies"][0]
    assert row["before_cutoff"]["resolved_rate"] == 1.0
    assert row["after_cutoff"]["resolved_rate"] == 0.0
    md = render_markdown(summarize(rs, cutoff=date(2026, 1, 1)))
    assert "1.000 before, 0.000 after" in md


@pytest.mark.parametrize("fmt", [render_markdown, render_html])
def test_renderers_escape(fmt):
    out = fmt(summarize([ep("a", "<script>", 0, True)]))
    assert "<script>" not in out or fmt is render_markdown


# ------------------------------------------------------------------ uncertainty


def _episode(task, policy, resolved, attempt=0):
    from gauntlet_harness.schema import GradeResult

    return EpisodeResult(
        episode_id=f"{task}__{policy}__{attempt}",
        instance_id=task,
        policy=policy,
        attempt=attempt,
        steps=1,
        seconds=1.0,
        usage={},
        patch="",
        grade=GradeResult(instance_id=task, patch_applied=True, resolved=resolved),
        reward=1.0 if resolved else 0.0,
        cheat_verdict="clean",
    )


def test_every_policy_row_carries_an_interval():
    rows = [_episode(f"t{i}", "claude:x", i % 2 == 0) for i in range(20)]
    row = summarize(rows)["policies"][0]
    lo, hi = row["pass@1_ci95"]
    assert lo < row["pass@1"] < hi
    assert row["tasks_solved"] == 10
    tlo, thi = row["tasks_solved_ci95"]
    assert tlo < 0.5 < thi


def test_a_small_gap_between_two_policies_is_not_called_significant():
    # 11 of 20 against 9 of 20 is the shape of most leaderboard gaps
    rows = [_episode(f"t{i}", "a", i < 11) for i in range(20)]
    rows += [_episode(f"t{i}", "b", i >= 11 or i < 9) for i in range(20)]
    (comparison,) = summarize(rows)["comparisons"]
    assert comparison["p_value"] > 0.05 and not comparison["significant"]


def test_a_real_gap_is_called_significant():
    rows = [_episode(f"t{i}", "oracle", True) for i in range(20)]
    rows += [_episode(f"t{i}", "noop", False) for i in range(20)]
    (comparison,) = summarize(rows)["comparisons"]
    assert comparison["a_only"] == 20 and comparison["b_only"] == 0
    assert comparison["significant"]


def test_the_markdown_shows_the_interval_and_the_comparison():
    rows = [_episode(f"t{i}", "a", i < 11) for i in range(20)]
    rows += [_episode(f"t{i}", "b", i >= 11 or i < 9) for i in range(20)]
    text = render_markdown(summarize(rows))
    assert "95% CI" in text and " to " in text
    assert "McNemar" in text
    # the policy table is sorted by resolve rate, so don't assume which name leads the row
    assert "| a | b |" in text or "| b | a |" in text


def test_the_report_separates_underpowered_from_no_difference():
    rows = [_episode(f"t{i}", "oracle", True) for i in range(4)]
    rows += [_episode(f"t{i}", "cheater", False) for i in range(4)]
    summary = summarize(rows)
    (comparison,) = summary["comparisons"]
    assert comparison["underpowered"] and not comparison["significant"]

    text = render_markdown(summary)
    assert "too few tasks" in text and "best possible p = 0.1250" in text
    assert "not a statement about the models" in text


def test_pairs_that_solved_the_same_tasks_leave_the_table():
    """Gauntlet Live JS: 10 cheats that all scored 0 filled 45 rows with nothing to read."""
    rows = [_episode(f"t{i}", "oracle", True) for i in range(8)]
    rows += [_episode(f"t{i}", p, False) for i in range(8) for p in ("noop", "cheater")]
    text = render_markdown(summarize(rows))
    assert "| oracle | noop |" in text and "| oracle | cheater |" in text
    assert "| noop | cheater |" not in text and "| cheater | noop |" not in text
    assert "solved exactly the same tasks" in text
    assert "too few tasks" not in text
