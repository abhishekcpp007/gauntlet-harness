"""Harbor tasks: the folder layout, the task.toml, and the grader that runs inside the verifier.

The grader tests run pytest for real, in a temp folder standing in for the verifier container.
No Docker.
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from gauntlet_harness import harbor
from gauntlet_harness.cli import main
from gauntlet_harness.profiles import profile_for
from gauntlet_harness.schema import HarnessTask, Horizon, Milestone, PRInfo

from .fakerepo import make_pr_repo

SRC = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
OLD_TESTS = "from calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n"
NEW_TESTS = OLD_TESTS + "\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _diff(path: str, before: str, after: str, tmp: Path) -> str:
    a, b = tmp / "a", tmp / "b"
    for root, body in ((a, before), (b, after)):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(body)
    out = subprocess.run(
        ["git", "diff", "--no-index", "--no-prefix", "a/" + path, "b/" + path],
        cwd=tmp,
        capture_output=True,
        text=True,
    ).stdout
    for root in (a, b):
        subprocess.run(["rm", "-rf", str(root)], check=True)
    return out.replace(f"--- a/a/{path}", f"--- a/{path}").replace(f"+++ b/b/{path}", f"+++ b/{path}")


def _task(workdir: Path, tmp: Path, **kw) -> HarnessTask:
    fields = dict(
        instance_id="o__calc-1",
        repo="https://github.com/o/calc.git",
        pr=PRInfo(repo="o/calc", number=1, title="add adds", url="https://github.com/o/calc/pull/1"),
        base_commit="a" * 40,
        solution_commit="b" * 40,
        problem_statement="add() subtracts",
        statement_source="pr",
        image="img",
        workdir=str(workdir),
        env_fingerprint="f" * 64,
        gold_patch=_diff("calc.py", SRC, FIXED, tmp),
        test_patch=_diff("tests/test_calc.py", OLD_TESTS, NEW_TESTS, tmp),
        test_files=["tests/test_calc.py"],
        f2p=["tests/test_calc.py::test_add"],
        p2p=["tests/test_calc.py::test_zero"],
    )
    fields.update(kw)
    return HarnessTask(**fields)


@pytest.fixture
def verifier(tmp_path, monkeypatch):
    """A folder holding the base commit, the way a fresh verifier container would."""
    work = tmp_path / "repo"
    (work / "tests").mkdir(parents=True)
    (work / "calc.py").write_text(SRC)
    (work / "tests" / "test_calc.py").write_text(OLD_TESTS)
    (work / "conftest.py").write_text("")
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)  # the agent's scratch repo
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    return work, _task(work, tmp_path)


def test_oracle_patch_resolves(verifier):
    work, task = verifier
    report = harbor.grade_here(task, task.gold_patch)
    assert report["resolved"] is True
    assert report["reward"] == pytest.approx(1.0)
    assert report["cheat_verdict"] == "clean"
    assert not (work / ".git").exists()  # graded on a plain tree, like grade_patch's sandbox


def test_empty_patch_is_malformed(verifier):
    _, task = verifier
    report = harbor.grade_here(task, "")
    assert report["resolved"] is False
    assert report["reward"] == -1.0


def test_forged_results_are_caught(verifier, tmp_path):
    """A conftest that marks every test passed: the canaries that must fail pass too."""
    work, task = verifier
    forge = (
        "import pytest\n\n\n@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n"
        "    rep = outcome.get_result()\n"
        "    rep.outcome = 'passed'\n"
    )
    cheat = _diff("conftest.py", "", forge, tmp_path)
    report = harbor.grade_here(task, cheat)
    assert report["resolved"] is False
    assert report["reward"] <= 0
    assert report["cheat_verdict"] == "cheating"


def test_harbor_grade_always_writes_a_reward(tmp_path, monkeypatch):
    task = _task(tmp_path, tmp_path)
    (tmp_path / "task.json").write_text(task.model_dump_json())

    def boom(*_):
        raise RuntimeError("verifier broke")

    monkeypatch.setattr(harbor, "grade_here", boom)
    out = tmp_path / "logs"
    assert harbor.harbor_grade(tmp_path / "task.json", tmp_path / "missing.patch", out) == 0
    assert json.loads((out / "reward.json").read_text()) == {"reward": 0.0, "resolved": 0.0}
    assert "verifier broke" in json.loads((out / "gauntlet.json").read_text())["error"]


def test_task_toml_runs_the_verifier_apart_and_offline(tmp_path):
    task = _task(Path("/workspace/repo"), tmp_path)
    cfg = tomllib.loads(harbor.task_toml(task))
    assert cfg["task"]["name"] == "gauntlet/o__calc-1"
    assert cfg["verifier"]["environment_mode"] == "separate"
    assert cfg["agent"]["network_mode"] == cfg["verifier"]["network_mode"] == "no-network"
    (collect,) = cfg["verifier"]["collect"]
    assert harbor.PATCH_ARTIFACT in collect["command"] and "git diff --cached --binary" in collect["command"]
    profile = profile_for(task)
    assert cfg["agent"]["timeout_sec"] == profile.max_steps * profile.command_timeout
    assert cfg["verifier"]["timeout_sec"] > profile.grade_timeout
    assert cfg["metadata"]["base_commit"] == task.base_commit


def test_lht_tasks_get_the_long_budget(tmp_path):
    swe = _task(Path("/workspace/repo"), tmp_path)
    lht = swe.model_copy(
        update={
            "category": "lht",
            "horizon": Horizon(from_ref="v1", to_ref="v2", milestones=[Milestone(id="m1", tests=swe.f2p)]),
        }
    )
    a, b = (tomllib.loads(harbor.task_toml(t)) for t in (swe, lht))
    assert b["agent"]["timeout_sec"] > a["agent"]["timeout_sec"]
    assert b["verifier"]["timeout_sec"] > a["verifier"]["timeout_sec"]


def test_dockerfile_pins_commit_and_grader(tmp_path):
    task = _task(Path("/workspace/repo"), tmp_path)
    text = harbor.dockerfile(task, "python:3.12-slim", "abc1234")
    assert text.startswith("FROM python:3.12-slim\n")
    assert f"git fetch -q --depth 1 https://github.com/o/calc.git {task.base_commit}" in text
    assert "gauntlet-harness.git@abc1234" in text or "gauntlet-harness@abc1234" in text
    # the scratch repo is made last, so everything the build left behind is in the base commit
    assert text.rindex("commit -q -m base") > text.rindex("pip install")


def test_cli_exports_the_harbor_layout(tmp_path, monkeypatch):
    repo_dir, prs = make_pr_repo(tmp_path / "src")
    base = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD~1"], capture_output=True, text=True, check=True
    ).stdout.strip()
    from gauntlet_repo2env.repo import Repo

    from gauntlet_harness import build, sandboxing

    monkeypatch.setattr(sandboxing, "cached_clone", lambda url, cache_dir=None: Repo(repo_dir, url, "o/calc"))
    monkeypatch.setattr(build, "fetch_commit", lambda repo, sha: None)
    task = _task(Path("/workspace/repo"), tmp_path, base_commit=base)
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(task.model_dump_json() + "\n")
    out = tmp_path / "harbor"
    assert (
        main(
            ["export", "--format", "harbor", "--tasks", str(tasks), "-o", str(out), "--grader-ref", "c0ffee"]
        )
        == 0
    )

    root = out / "o__calc-1"
    for rel in ("task.toml", "instruction.md", "environment/Dockerfile", "environment/install.sh",
                "solution/solve.sh", "solution/gold.diff", "tests/test.sh", "tests/task.json"):  # fmt: skip
        assert (root / rel).is_file(), rel
    assert os.access(root / "tests" / "test.sh", os.X_OK) and os.access(
        root / "solution" / "solve.sh", os.X_OK
    )
    assert "add() subtracts" in (root / "instruction.md").read_text()
    assert "test_add" not in (root / "instruction.md").read_text()  # hidden tests stay in tests/
    assert HarnessTask.model_validate_json((root / "tests" / "task.json").read_text()) == task
    assert (root / "solution" / "gold.diff").read_text() == task.gold_patch
    assert "pip install" in (root / "environment" / "install.sh").read_text()


def test_cli_harbor_needs_tasks(tmp_path):
    assert main(["export", "--format", "harbor", "-o", str(tmp_path / "h")]) == 2


def test_local_sandbox_basics(tmp_path):
    sb = harbor.LocalSandbox(str(tmp_path), default_timeout=5)
    sb.write_file("a/b.txt", "hi")
    assert sb.read_text("a/b.txt") == "hi"
    assert sb.exec("cat a/b.txt").stdout == "hi"
    res = sb.exec("sleep 5", timeout=0.5)
    assert res.timed_out and not res.ok
    assert sb.exec("exit 3").exit_code == 3
