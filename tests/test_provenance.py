"""A number is only comparable to another number taken the same way."""

from __future__ import annotations

from gauntlet_harness.report import provenance_warnings, render_markdown, summarize
from gauntlet_harness.schema import EpisodeResult, GradeResult, Provenance


def ep(task, policy, resolved=True, **prov):
    base = {
        "harness_version": "0.1.0",
        "policy_params": {"model": "m", "effort": "high"},
        "image": "img@sha256:aaa",
        "env_fingerprint": "fp1",
        "ran_at": "2026-11-06T00:00:00+00:00",
    }
    return EpisodeResult(
        episode_id=f"{task}__{policy}__0",
        instance_id=task,
        policy=policy,
        grade=GradeResult(instance_id=task, patch_applied=True, resolved=resolved),
        reward=1.0 if resolved else 0.0,
        cheat_verdict="clean",
        provenance=Provenance(**{**base, **prov}),
    )


def test_a_clean_run_has_nothing_to_warn_about():
    rows = [ep(f"t{i}", "claude:x") for i in range(5)]
    assert provenance_warnings(rows) == []


def test_two_harness_versions_in_one_report_is_called_out():
    rows = [ep("t1", "claude:x"), ep("t2", "claude:x", harness_version="0.2.0")]
    warnings = provenance_warnings(rows)
    assert any("mixed harness versions: 0.1.0, 0.2.0" == w for w in warnings)
    # the version is part of what makes a setup, so the per-policy warning fires too
    assert any("2 different setups" in w for w in warnings)


def test_the_same_policy_under_two_images_is_not_one_measurement():
    rows = [ep("t1", "claude:x"), ep("t2", "claude:x", image="img@sha256:bbb")]
    assert any("claude:x ran under 2 different setups" in w for w in provenance_warnings(rows))


def test_the_same_policy_with_two_sampling_settings_is_not_one_measurement():
    rows = [ep("t1", "claude:x"), ep("t2", "claude:x", policy_params={"model": "m", "effort": "low"})]
    assert any("2 different setups" in w for w in provenance_warnings(rows))


def test_two_policies_differing_is_fine():
    """Different policies are supposed to differ; that is the comparison."""
    rows = [ep("t1", "claude:x"), ep("t1", "openai:y", policy_params={"model": "y"})]
    assert provenance_warnings(rows) == []


def test_a_pinned_grading_seed_on_only_some_episodes_is_called_out():
    """Unset has to count as a value, or the mix that matters most slips through."""
    rows = [ep("t1", "claude:x"), ep("t2", "claude:x", grading_seed="abc")]
    assert any("mixed grading seeds: abc, unset" == w for w in provenance_warnings(rows))


def test_episodes_with_no_provenance_are_called_out():
    rows = [ep("t1", "claude:x")]
    rows.append(EpisodeResult(episode_id="t2__claude:x__0", instance_id="t2", policy="claude:x"))
    assert any("carry no provenance" in w for w in provenance_warnings(rows))


def test_an_entirely_old_results_file_says_so():
    rows = [EpisodeResult(episode_id="a", instance_id="t", policy="p")]
    (warning,) = provenance_warnings(rows)
    assert "no provenance recorded" in warning


def test_the_warning_reaches_the_rendered_report():
    rows = [ep("t1", "claude:x"), ep("t2", "claude:x", harness_version="0.2.0")]
    text = render_markdown(summarize(rows))
    assert "Comparability" in text and "mixed harness versions" in text
