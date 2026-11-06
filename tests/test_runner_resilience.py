"""One episode going wrong must not take the sweep with it, and a typo must fail at once."""

from __future__ import annotations

import pytest

from gauntlet_harness import runner
from gauntlet_harness.runner import load_results, run

from .test_budget import _task  # the same minimal task


def test_a_bad_policy_spec_fails_before_any_work(tmp_path):
    """A typo is worth one clear error now, not 53 identical ones later."""
    with pytest.raises(ValueError, match="unknown policy"):
        run([_task()], ["oracle", "gpt-9"], out_dir=tmp_path)
    assert not (tmp_path / "results.jsonl").exists()


def test_an_env_that_will_not_build_costs_one_episode(tmp_path, monkeypatch):
    """A pull that times out or a daemon that went away is this episode's problem, not the run's."""
    tasks = [_task()]

    def boom(*args, **kwargs):
        raise RuntimeError("no space left on device")

    monkeypatch.setattr(runner, "SWEEnv", boom)
    out = run(tasks, ["noop", "oracle"], out_dir=tmp_path, workers=1)

    assert len(out) == 2
    assert all(r.error == "RuntimeError: no space left on device" for r in out)
    # and it was written, so a rerun does not repeat it blindly
    assert {r.episode_id for r in load_results(tmp_path / "results.jsonl")} == {r.episode_id for r in out}


def test_a_failed_episode_still_records_its_policy_name(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(runner, "SWEEnv", boom)
    (result,) = run([_task()], ["noop"], out_dir=tmp_path, workers=1)
    assert result.policy == "noop" and result.instance_id == "a__b-1"
