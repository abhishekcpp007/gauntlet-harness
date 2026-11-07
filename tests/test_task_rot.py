"""A task pins a commit in somebody else's repository, and that commit can stop existing.

Measured on the live set on 6 Nov 2026: two of the 53 tasks pinned commits that tkem/cachetools
had since rewritten, so eleven policies each crashed on each of them and the nightly reported 22
violations. Every one of those said the same thing, that the task could not be built, and none
of them said anything about the harness. A nightly that goes red for somebody else's force-push
is a nightly people learn to ignore.
"""

from __future__ import annotations

import pytest

from gauntlet_harness.cli import calibration_violations, is_task_rot, rotted_tasks
from gauntlet_harness.schema import EpisodeResult, GradeResult

GONE = (
    "GitError: git ls-tree -r --name-only 5877d83c7ab53639f1cfb8664173219e5e5cfbf1 failed: "
    "fatal: not a tree object"
)


def ep(policy, instance="a__b-1", *, error=None, resolved=True, reward=1.0, verdict="clean"):
    grade = None if error else GradeResult(instance_id=instance, patch_applied=True, resolved=resolved)
    return EpisodeResult(
        episode_id=f"{instance}__{policy}__0",
        instance_id=instance,
        policy=policy,
        grade=grade,
        reward=reward,
        cheat_verdict=verdict,
        error=error,
    )


@pytest.mark.parametrize(
    "error",
    [
        GONE,
        "GitError: git archive --format=tar f4f2885 failed: fatal: not a tree object: f4f2885",
        "fatal: bad object deadbeef",
        "error: pathspec 'abc123' did not match any file(s) known to git",
    ],
)
def test_a_rewritten_history_is_recognised(error):
    assert is_task_rot(error)


@pytest.mark.parametrize(
    "error",
    [
        None,
        "SandboxError: Can't reach Docker. Is the daemon running?",
        "RuntimeError: hidden tests didn't apply",
        "TimeoutError: the suite took too long",
    ],
)
def test_a_real_failure_is_not_mistaken_for_rot(error):
    assert not is_task_rot(error)


def test_a_rotted_task_is_not_a_violation():
    """Eleven policies crashing on one dead commit is one rotted task, not eleven broken promises."""
    results = [ep(p, "tkem__cachetools-424", error=GONE) for p in ("oracle", "noop", "cheater")]
    assert calibration_violations(results) == []
    assert rotted_tasks(results) == {"tkem__cachetools-424": GONE}


def test_rot_does_not_hide_a_real_violation_elsewhere():
    results = [
        ep("oracle", "rotted", error=GONE),
        ep("oracle", "live", resolved=False, reward=0.0),  # the real fix stopped resolving
    ]
    (violation,) = calibration_violations(results)
    assert "live" in violation and "didn't resolve" in violation
    assert list(rotted_tasks(results)) == ["rotted"]


def test_a_crash_that_is_not_rot_is_still_a_violation():
    results = [ep("oracle", error="SandboxError: Can't reach Docker. Is the daemon running?")]
    (violation,) = calibration_violations(results)
    assert "episode crashed" in violation
    assert rotted_tasks(results) == {}


def test_the_calibration_promises_still_hold():
    """The rot exemption must not have loosened anything else."""
    assert calibration_violations([ep("noop", reward=0.5, resolved=False)])
    assert calibration_violations([ep("cheater", reward=0.3, resolved=False, verdict="cheating")])
    assert calibration_violations([ep("oracle", resolved=False, reward=0.0)])
    assert calibration_violations([ep("oracle", resolved=True, reward=1.0)]) == []
    assert calibration_violations([ep("noop", resolved=False, reward=-1.0)]) == []
