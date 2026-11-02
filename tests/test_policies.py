"""Policies against a fake env and a scripted fake Claude client. No Docker, no API calls."""

from types import SimpleNamespace as NS

import pytest

from gauntlet_harness.policies import ClaudePolicy, NoopPolicy, OraclePolicy, make_policy


class FakeEnv:
    def __init__(self, gold="diff --git a/x.py b/x.py\n", max_steps=50):
        self.task = NS(gold_patch=gold, test_files=["tests/test_x.py"])
        self.actions = []
        self.max_steps = max_steps
        self.submitted = False

    def reset(self):
        return "solve the task", {}

    def step(self, action):
        self.actions.append(action)
        if action["type"] == "submit" or len(self.actions) >= self.max_steps:
            self.submitted = True
            return "submitted", 1.0, True, len(self.actions) >= self.max_steps, {}
        if action.get("path") == "/etc/passwd":
            return "Error: outside the repo", 0.0, False, False, {"error": True}
        return f"ok: {action}", 0.0, False, False, {"error": False}


def tool_use(id, name, **input):
    return NS(type="tool_use", id=id, name=name, input=input)


def reply(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, usage=NS(input_tokens=7, output_tokens=3))


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return self.replies.pop(0)


def test_make_policy():
    assert make_policy("oracle").name == "oracle"
    assert make_policy("claude:claude-sonnet-5-5").name == "claude:claude-sonnet-5-5"
    assert make_policy("claude").name == "claude:claude-opus-5-5"
    with pytest.raises(ValueError):
        make_policy("gpt-9")


def test_oracle_applies_gold_and_submits():
    env = FakeEnv(gold="diff --git a/x.py b/x.py\n+fixed\n")
    OraclePolicy().run(env)
    assert "+fixed" in env.actions[0]["command"] and "git apply" in env.actions[0]["command"]
    assert env.actions[-1] == {"type": "submit"}


def test_noop_submits():
    env = FakeEnv()
    NoopPolicy().run(env)
    assert env.submitted and len(env.actions) == 2


def test_claude_maps_tools_to_env_actions():
    client = FakeClient(
        [
            reply(
                NS(type="text", text="looking"),
                tool_use("a", "bash", command="pytest -q"),
                tool_use("b", "str_replace_based_edit_tool", command="view", path="/etc/passwd"),
            ),
            reply(tool_use("c", "str_replace_based_edit_tool", command="create", path="x.py", file_text="y")),
            reply(NS(type="text", text="done"), stop="end_turn"),
        ]
    )
    env = FakeEnv()
    policy = ClaudePolicy("claude-sonnet-5-5", client=client)
    policy.run(env)

    assert env.actions == [
        {"type": "bash", "command": "pytest -q"},
        {"type": "edit", "command": "view", "path": "/etc/passwd"},
        {"type": "edit", "command": "create", "path": "x.py", "file_text": "y"},
        {"type": "submit"},
    ]
    first = client.calls[0]
    assert first["model"] == "claude-sonnet-5-5"
    assert first["thinking"] == {"type": "adaptive"} and first["fallbacks"] == "default"
    results = client.calls[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert results[1]["is_error"] is True and "is_error" not in results[0]
    assert policy.usage == {"input_tokens": 21, "output_tokens": 9}
    assert policy.messages[0] == {"role": "user", "content": "solve the task"}


def test_claude_refusal_submits():
    env = FakeEnv()
    ClaudePolicy(client=FakeClient([reply(stop="refusal")])).run(env)
    assert env.actions == [{"type": "submit"}]


def test_claude_stops_when_env_truncates():
    replies = [reply(tool_use(str(i), "bash", command="ls")) for i in range(5)]
    env = FakeEnv(max_steps=3)
    ClaudePolicy(client=FakeClient(replies)).run(env)
    assert len(env.actions) == 3 and env.submitted


def test_calibration_violations_catch_every_broken_promise():
    from gauntlet_harness.cli import calibration_violations
    from gauntlet_harness.schema import EpisodeResult, GradeResult

    def ep(policy, reward, resolved=False, error=None):
        g = GradeResult(instance_id="t", patch_applied=True, resolved=resolved)
        return EpisodeResult(
            episode_id="e", instance_id="t", policy=policy, reward=reward, grade=g, error=error
        )

    good = [ep("oracle", 1.0, True), ep("noop", -1.0), ep("cheater:stealth", -1.0), ep("cheater", 0.0)]
    assert calibration_violations(good) == []
    bad = calibration_violations(
        [
            ep("oracle", 0.2),
            ep("noop", 0.1),
            ep("cheater:stealth", 0.88, True),
            ep("cheater:rewrite", -1, error="x"),
        ]
    )
    assert len(bad) == 4 and "cheat earned +0.880" in bad[2]


def test_zoo_policies_and_their_payloads():
    from gauntlet_harness.policies import ZOO, ZooCheater, make_policy

    for trick in ZOO:
        policy = make_policy(f"cheater:{trick}")
        assert isinstance(policy, ZooCheater) and policy.name == f"cheater:{trick}"
        compile(policy.payload(), trick, "exec")
    assert "from_item_and_call" not in make_policy("cheater:stealth").payload()  # hidden from regexes
    with pytest.raises(ValueError):
        make_policy("cheater:nope")
