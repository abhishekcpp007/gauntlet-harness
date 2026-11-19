"""Harbor export: the task folder, and the verifier it carries. No Docker.

The verifier is run for real here against a small git repo, because it is the part that decides
a reward and it has to stand without the harness around it.
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from gauntlet_repo2env.testrun import parse_junit

from gauntlet_harness import harbor, harbor_grade
from gauntlet_harness.cli import main
from gauntlet_harness.schema import HarnessTask, PRInfo

BASE = "1" * 40
FIX = "2" * 40
GOLD = (
    "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
)
TEST_PATCH = (
    "diff --git a/tests/test_calc.py b/tests/test_calc.py\n"
    "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n"
    "@@ -5,3 +5,7 @@ def test_zero():\n \n def test_same():\n     assert add(2, 0) == 2\n"
    "+\n+\n+def test_add():\n+    assert add(2, 3) == 5\n"
)


def make_task(**over) -> HarnessTask:
    fields = dict(
        instance_id="Owner__calc-7",
        repo="https://github.com/owner/calc.git",
        pr=PRInfo(
            repo="owner/calc",
            number=7,
            title="t",
            url="https://github.com/owner/calc/pull/7",
            merged_at="2026-10-01",
        ),
        base_commit=BASE,
        solution_commit=FIX,
        problem_statement="add() subtracts.",
        statement_source="pr",
        image="gauntlet-harness-env:abc",
        env_fingerprint="abc",
        gold_patch=GOLD,
        test_patch=TEST_PATCH,
        test_files=["tests/test_calc.py"],
        f2p=["tests/test_calc.py::test_add"],
        p2p=["tests/test_calc.py::test_zero", "tests/test_calc.py::test_same"],
    )
    return HarnessTask(**{**fields, **over})


# ---------------------------------------------------------------------- the folder


def test_export_writes_a_harbor_task(tmp_path):
    root = harbor.export_task(make_task(), tmp_path)
    assert root == tmp_path / "owner-calc-7"
    files = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    assert files == {
        "task.toml",
        "instruction.md",
        "environment/Dockerfile",
        "solution/solve.sh",
        "solution/fix.patch",
        "tests/test.sh",
        "tests/grade.py",
        "tests/test.patch",
        "tests/task.json",
        "tests/rubric.json",
    }
    assert os.access(root / "solution" / "solve.sh", os.X_OK)
    assert os.access(root / "tests" / "test.sh", os.X_OK)
    assert (root / "solution" / "fix.patch").read_text() == GOLD


def test_task_toml_is_what_harbor_reads(tmp_path):
    root = harbor.export_task(make_task(), tmp_path)
    data = tomllib.loads((root / "task.toml").read_text())
    assert data["schema_version"] == harbor.SCHEMA_VERSION
    assert data["task"]["name"] == "gauntlet/owner-calc-7"
    assert data["metadata"]["instance_id"] == "Owner__calc-7"
    assert data["metadata"]["target_tests"] == 1
    assert data["environment"]["workdir"] == "/workspace/repo"
    assert data["environment"]["memory_mb"] >= harbor.MIN_MEMORY_MB


def test_dockerfile_fetches_the_base_commit_and_nothing_later(tmp_path):
    text = (harbor.export_task(make_task(), tmp_path) / "environment" / "Dockerfile").read_text()
    assert text.startswith("FROM gauntlet-harness-env:abc\n")
    assert f"git fetch -q --depth 1 origin {BASE}" in text
    assert "git remote remove origin" in text
    assert FIX not in text


def test_the_fix_never_reaches_the_agent(tmp_path):
    root = harbor.export_task(make_task(), tmp_path)
    seen_by_agent = (root / "instruction.md").read_text() + (root / "environment" / "Dockerfile").read_text()
    assert "return a + b" not in seen_by_agent
    assert "test_add" not in seen_by_agent


def test_rubric_rides_at_the_end_of_the_task(tmp_path):
    items = json.loads((harbor.export_task(make_task(), tmp_path) / "tests" / "rubric.json").read_text())
    by_id = {i["id"]: i for i in items}
    assert by_id["r_targets"]["scored_by"] == "verifier"
    assert by_id["p_tests"]["scored_by"] == "verifier"
    assert by_id["r_local"]["scored_by"] == "harness"
    assert all(i["weight"] > 0 for i in items)


@pytest.mark.parametrize("runner", ["vitest", "jest"])
def test_node_tasks_are_refused_with_a_reason(tmp_path, runner):
    with pytest.raises(harbor.HarborExportError, match="needs Python in the env image"):
        harbor.export_task(make_task(language="node", runner=runner), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cli_export_harbor(tmp_path, capsys):
    tasks = tmp_path / "tasks.jsonl"
    node = make_task(instance_id="o__js-1", language="node", runner="vitest")
    tasks.write_text(make_task().model_dump_json() + "\n" + node.model_dump_json() + "\n")
    out = tmp_path / "harbor"
    assert main(["export", "--format", "harbor", "--tasks", str(tasks), "-o", str(out)]) == 0
    assert "wrote 1 harbor tasks" in capsys.readouterr().out
    assert json.loads((out / "owner-calc-7" / "tests" / "task.json").read_text())["strict"] is True
    assert main(["export", "--format", "harbor", "--tasks", str(tasks), "-o", str(out), "--lenient"]) == 0
    assert json.loads((out / "owner-calc-7" / "tests" / "task.json").read_text())["strict"] is False
    assert main(["export", "--format", "harbor", "-o", str(out)]) == 2


# ---------------------------------------------------------------------- the verifier

PYTEST_XML = """<testsuites><testsuite name="pytest">
<testcase classname="tests.test_calc" name="test_zero" file="tests/test_calc.py"/>
<testcase classname="tests.test_calc.TestAdd" name="test_add" file="tests/test_calc.py">
<failure>no</failure></testcase>
<testcase classname="tests.test_calc" name="test_skip" file="tests/test_calc.py"><skipped/></testcase>
<testcase classname="tests.test_other" name="test_err"><error>boom</error></testcase>
</testsuite></testsuites>"""
VITEST_XML = """<testsuites><testsuite name="src/a.test.ts">
<testcase classname="src/a.test.ts" name="a &gt; adds"/></testsuite></testsuites>"""


@pytest.mark.parametrize("xml", [PYTEST_XML, VITEST_XML])
def test_verifier_reads_a_report_the_way_the_harness_does(xml):
    assert harbor_grade.parse_junit(xml) == parse_junit(xml)


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def staged(tmp_path):
    """A repo at its base commit with the exported verifier beside it, as Harbor would lay it out."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n"
        "\n\ndef test_same():\n    assert add(2, 0) == 2\n"
    )
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    tests = harbor.export_task(make_task(base_commit=base, workdir=str(repo)), tmp_path / "out") / "tests"
    spec = json.loads((tests / "task.json").read_text())
    spec["junit_path"] = str(tmp_path / "junit.xml")
    flags = f"-p no:cacheprovider -q -o junit_family=xunit1 --junitxml={spec['junit_path']}"
    spec["suite_command"] = f"cd {repo} && {sys.executable} -m pytest {flags}"
    (tests / "task.json").write_text(json.dumps(spec))
    return repo, tests, tmp_path / "logs"


def grade(staged, *, strict=True) -> dict:
    repo, tests, logs = staged
    spec = json.loads((tests / "task.json").read_text())
    spec["strict"] = strict
    (tests / "task.json").write_text(json.dumps(spec))
    env = {**os.environ, "GAUNTLET_VERIFIER_LOGS": str(logs), "PYTHONPATH": str(repo)}
    subprocess.run([sys.executable, str(tests / "grade.py")], env=env, check=True, capture_output=True)
    detail = json.loads((logs / "gauntlet.json").read_text())
    assert (logs / "reward.txt").read_text().strip() == str(detail["reward"])
    return detail


def fix(repo: Path) -> None:
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")


def test_doing_nothing_scores_zero(staged):
    detail = grade(staged)
    assert detail["reward"] == 0
    assert detail["targets"]["failed"] == ["tests/test_calc.py::test_add"]


def test_the_real_fix_scores_one(staged):
    fix(staged[0])
    detail = grade(staged)
    assert detail["reward"] == 1
    assert detail["tampered"] == []
    assert detail["rubric"]["score"] == 1.0
    assert "r_local" in detail["rubric"]["left_to_the_harness"]


def test_a_fix_that_breaks_an_old_test_scores_zero(staged):
    (staged[0] / "calc.py").write_text("def add(a, b):\n    return 5\n")
    detail = grade(staged)
    assert detail["reward"] == 0
    assert detail["targets"]["failed"] == []
    assert "tests/test_calc.py::test_zero" in detail["p2p"]["broken"]


def test_editing_the_graded_tests_scores_zero_even_with_the_fix(staged):
    fix(staged[0])
    (staged[0] / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n")
    detail = grade(staged)
    assert detail["resolved"] is True
    assert detail["tampered"] == ["tests/test_calc.py"]
    assert detail["reward"] == 0
    assert detail["rubric"]["score"] < 1.0


def test_lenient_only_throws_the_test_edits_away(staged):
    fix(staged[0])
    (staged[0] / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n")
    detail = grade(staged, strict=False)
    assert detail["tampered"] == ["tests/test_calc.py"]
    assert detail["reward"] == 1


def test_rewriting_the_tests_without_a_fix_gains_nothing(staged):
    (staged[0] / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n")
    assert grade(staged, strict=False)["reward"] == 0


def test_committing_the_edits_does_not_hide_them(staged):
    repo = staged[0]
    fix(repo)
    (repo / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n")
    git(repo, "commit", "-q", "-am", "agent")
    assert grade(staged)["tampered"] == ["tests/test_calc.py"]


def test_deleting_git_metadata_scores_zero(staged):
    fix(staged[0])
    subprocess.run(["rm", "-rf", str(staged[0] / ".git")], check=True)
    detail = grade(staged)
    assert detail["reward"] == 0
    assert "git metadata" in detail["error"]


def test_a_folder_among_the_test_paths_does_not_crash_the_verifier(staged):
    repo, tests, _ = staged
    fix(repo)
    (repo / "tests" / "data").mkdir()
    spec = json.loads((tests / "task.json").read_text())
    spec["test_paths"].append("tests/data")
    (tests / "task.json").write_text(json.dumps(spec))
    assert grade(staged)["reward"] == 1
