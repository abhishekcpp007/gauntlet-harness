"""The HTTP env server against a fake env, over a real socket. No Docker."""

import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

import pytest

from gauntlet_harness.schema import EpisodeResult, GradeResult, HarnessTask, PRInfo
from gauntlet_harness.serve import EnvServer, serve

TASK = HarnessTask(
    instance_id="o__r-1",
    repo="r",
    pr=PRInfo(repo="o/r", number=1, title="t", url="u"),
    base_commit="a",
    solution_commit="b",
    problem_statement="fix it",
    statement_source="issue",
    image="img",
    env_fingerprint="f",
    gold_patch="",
    test_patch="",
    test_files=[],
)


class FakeEnv:
    def __init__(self, task, trajectory_path=None):
        self.actions, self.trajectory_path = [], trajectory_path

    def reset(self):
        return "the task", {"max_steps": 5}

    def step(self, action):
        self.actions.append(action)
        if action["type"] == "submit":
            grade = GradeResult(instance_id="o__r-1", patch_applied=True, resolved=True)
            result = EpisodeResult(episode_id="x", instance_id="o__r-1", policy="", reward=1.0, grade=grade)
            return "submitted", 1.0, True, False, {"result": result}
        return f"ran {action['command']}", 0.0, False, False, {"error": False}

    def close(self):
        pass


@pytest.fixture
def url(tmp_path):
    env_server = EnvServer(TASK, policy="me", out_dir=tmp_path, env_factory=FakeEnv)
    httpd = serve(env_server, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    httpd.shutdown()


def call(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_episode_over_http(url):
    base, out = url
    assert call(base, "/step", {"type": "bash", "command": "ls"})[0] == 400  # reset first
    code, body = call(base, "/reset")
    assert code == 200 and body["obs"] == "the task" and body["episode_id"] == "o__r-1__me__0"
    assert call(base, "/step", {"type": "bash", "command": "ls"})[1]["obs"] == "ran ls"
    code, body = call(base, "/step", {"type": "submit"})
    assert body["terminated"] and body["result"]["reward"] == 1.0 and body["result"]["policy"] == "me"

    rec = json.loads((out / "rl" / "o__r-1__me__0.json").read_text())
    assert [m["role"] for m in rec["messages"]] == ["user", "assistant", "tool", "assistant", "tool"]
    assert len((out / "results.jsonl").read_text().splitlines()) == 1
    assert NS(**json.loads(urllib.request.urlopen(base + "/health").read())).policy == "me"
