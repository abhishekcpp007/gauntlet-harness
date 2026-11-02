"""The Gymnasium front, over a fake inner env. No Docker."""

import gymnasium
import pytest

import gauntlet_harness.gym  # noqa: F401  registers gauntlet/SWE-v0
from gauntlet_harness.gym import GauntletSWEEnv
from gauntlet_harness.schema import HarnessTask, PRInfo


def task(n):
    return HarnessTask(
        instance_id=f"o__r-{n}",
        repo="r",
        pr=PRInfo(repo="o/r", number=n, title="t", url="u"),
        base_commit="a",
        solution_commit="b",
        problem_statement=f"task {n}",
        statement_source="issue",
        image="img",
        env_fingerprint="f",
        gold_patch="",
        test_patch="",
        test_files=[],
    )


class FakeEnv:
    closed = 0

    def __init__(self, task, **kw):
        self.task, self.kw = task, kw

    def reset(self):
        return self.task.problem_statement, {"instance_id": self.task.instance_id}

    def step(self, action):
        if action["type"] == "submit":
            return "done", 1.0, True, False, {}
        return f"ran {action['command']}", 0.0, False, False, {"error": False}

    def close(self):
        FakeEnv.closed += 1


TASKS = [task(1), task(2), task(3)]


def test_seeded_reset_and_text_actions():
    env = GauntletSWEEnv(TASKS, env_factory=FakeEnv, max_steps=9)
    obs1, info1 = env.reset(seed=7)
    obs2, _ = GauntletSWEEnv(TASKS, env_factory=FakeEnv).reset(seed=7)
    assert obs1 == obs2 and info1["instance_id"] in {t.instance_id for t in TASKS}
    assert env.inner.kw == {"max_steps": 9}

    assert env.step('{"type": "bash", "command": "ls"}')[0] == "ran ls"
    obs, reward, term, trunc, info = env.step("not json")
    assert obs.startswith("Error") and info["error"] and not term
    assert env.step("[1]")[4]["error"]
    assert env.step({"type": "submit"})[1:3] == (1.0, True)
    env.close()


def test_pick_task_and_registry(tmp_path):
    env = GauntletSWEEnv(TASKS, env_factory=FakeEnv)
    assert env.reset(options={"task_id": "o__r-2"})[0] == "task 2"
    assert GauntletSWEEnv(TASKS, task_id="o__r-3", env_factory=FakeEnv).reset()[0] == "task 3"
    with pytest.raises(ValueError):
        GauntletSWEEnv(TASKS, task_id="nope", env_factory=FakeEnv)

    path = tmp_path / "tasks.jsonl"
    path.write_text("".join(t.model_dump_json() + "\n" for t in TASKS))
    made = gymnasium.make("gauntlet/SWE-v0", tasks=str(path), env_factory=FakeEnv, disable_env_checker=True)
    assert made.reset(seed=0)[0].startswith("task")
    with pytest.raises(RuntimeError):
        GauntletSWEEnv(TASKS, env_factory=FakeEnv).step("{}")
