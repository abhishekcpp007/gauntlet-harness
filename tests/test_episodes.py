"""The RL loop end to end on real containers: env, scripted policies, runner, grading, rubric, reward."""

import json

import pytest

from gauntlet_harness.env import SWEEnv
from gauntlet_harness.report import summarize
from gauntlet_harness.runner import load_results, run
from gauntlet_harness.schema import HarnessTask

from .pipeline import pipeline

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def task() -> HarnessTask:
    t = pipeline().results[1]
    assert isinstance(t, HarnessTask), t
    return t


def test_env_actions_and_truncation(task, tmp_path):
    env = SWEEnv(task, max_steps=4, cache_dir=pipeline().cache, trajectory_path=tmp_path / "t.jsonl")
    obs, info = env.reset()
    assert task.problem_statement.strip()[:40] in obs and info["max_steps"] == 4

    obs, reward, term, trunc, info = env.step({"type": "bash", "command": "ls -a"})
    assert ".git" in obs and reward == 0.0 and not term and not info["error"]

    obs, *_, info = env.step({"type": "edit", "command": "view", "path": "/etc/passwd"})
    assert info["error"] and "outside the repo" in obs

    obs, *_, info = env.step({"type": "fly"})
    assert info["error"] and "unknown action" in obs

    # fourth step hits the limit: auto-submit, truncated, real reward computed
    obs, reward, term, trunc, info = env.step({"type": "bash", "command": "true"})
    assert term and trunc and "step limit" in obs
    result = info["result"]
    assert result.truncated and not result.grade.resolved
    assert reward == result.reward == -1.0  # empty patch is malformed, SWE-RL style
    with pytest.raises(RuntimeError):
        env.step({"type": "submit"})


def test_runner_calibration(task, tmp_path):
    results = run(
        [task], ["oracle", "noop", "cheater"], out_dir=tmp_path, cache_dir=pipeline().cache, workers=3
    )
    by = {r.policy: r for r in results}
    assert all(r.error is None for r in results), [r.error for r in results]

    oracle = by["oracle"]
    assert oracle.grade.resolved and oracle.cheat_verdict == "clean"
    assert oracle.grade.rates() == {"f2p": 1.0, "n2p": 1.0, "s2p": 1.0, "p2p": 1.0}
    assert oracle.rubric.score > 0.9 and oracle.reward > 0.9
    assert oracle.reward_breakdown["similarity"] == pytest.approx(1.0)

    noop = by["noop"]
    assert not noop.grade.resolved and noop.reward == -1.0

    cheater = by["cheater"]
    assert not cheater.grade.resolved
    assert cheater.cheat_verdict == "cheating" and cheater.reward == -1.0
    assert "conftest.py" in cheater.grade.ignored_files

    # every episode left an RL record and a replayable sandbox trajectory
    for r in results:
        rl = json.loads(open(r.rl_path).read())
        assert rl["messages"][0]["role"] == "user" and rl["actions"][-1] == {"type": "submit"}
        assert open(r.trajectory_path).readline().startswith('{"type": "header"')

    summary = summarize(load_results(tmp_path / "results.jsonl"))
    assert [row["policy"] for row in summary["policies"]][0] == "oracle"

    # resumable: nothing reruns
    assert run([task], ["oracle", "noop", "cheater"], out_dir=tmp_path, cache_dir=pipeline().cache) == []


@pytest.mark.parametrize("trick", ["monkeypatch", "stealth", "rewrite"])
def test_cheater_zoo_is_caught_by_the_grader_itself(task, tmp_path, trick):
    """Source-only cheats survive test stripping, so the grader's canary has to catch them at run time."""
    (r,) = run([task], [f"cheater:{trick}"], out_dir=tmp_path, cache_dir=pipeline().cache)
    assert r.error is None, r.error
    assert r.grade.ignored_files == []  # nothing for the stripper to drop: it's all source
    assert r.grade.tampered, r.grade
    assert not r.grade.resolved and r.reward == -1.0 and r.cheat_verdict == "cheating"
    assert "grader-tamper" in {f["rule"] for f in r.cheat_findings}


def test_honest_runs_pass_the_canary(task, tmp_path):
    from gauntlet_harness.grading import grade_patch

    grade = grade_patch(task, task.gold_patch, cache_dir=pipeline().cache)
    assert grade.resolved and grade.tampered == []
    known_files = {t.split("::")[0] for t in [*task.targets, *task.p2p]}
    assert {t.split("::")[0] for t in grade.statuses} <= known_files  # the canary is filtered out
