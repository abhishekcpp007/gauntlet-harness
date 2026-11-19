"""Write a task as a Harbor task directory, so it runs under `harbor run`.

Harbor (the harness behind Terminal-Bench 2) wants one folder per task: an instruction, an
environment to build, a solution, and a test script that leaves a reward in
/logs/verifier/reward.txt. Everything a Gauntlet task needs for that is already in the task.

The verifier here is the strict, plain check and nothing more: the grader's tests replace
whatever the agent left in those files, every target test has to pass, every test that passed
before has to pass still, and a run that touched the graded test files scores 0 whatever the
tests say. The canaries and the before-and-after file hashes stay in the harness's own grader,
because they need a second process the agent can't reach.

The rubric rides at the end of the task, in tests/rubric.json. The verifier scores the items
it can see from inside the container and names the ones only the harness can score.
"""

from __future__ import annotations

import json
import math
import re
import shlex
from pathlib import Path

from gauntlet_repo2env import split_patch
from gauntlet_repo2env.testrun import suite_command

from .rubric import default_rubric
from .runners import runner_for
from .schema import HarnessTask
from .testing import JUNIT_PATH

SCHEMA_VERSION = "1.4"
ORG = "gauntlet"
# The verifier is Python and fetches the base commit over https. The Node env images carry
# neither Python nor CA certificates, so a vitest or jest task would not even build.
SUPPORTED_RUNNERS = frozenset({"pytest"})
VERIFIER_TIMEOUT_SEC = 1800.0
AGENT_TIMEOUT_SEC = 1800.0
BUILD_TIMEOUT_SEC = 900.0
MIN_MEMORY_MB = 2048
_MEMORY_UNITS = {"m": 1, "g": 1024}

# Rubric rules the verifier can score with what it has inside the container. The others need
# the candidate patch next to the gold one, or the cheat report, which only the harness holds.
IN_CONTAINER_RULES = frozenset({"targets_fixed", "no_regressions", "no_test_tampering"})


class HarborExportError(ValueError):
    pass


def task_name(task: HarnessTask) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.instance_id.lower()).strip("-")
    return f"{ORG}/{slug}"


def _memory_mb(spec: str) -> int:
    return max(MIN_MEMORY_MB, int(spec[:-1]) * _MEMORY_UNITS[spec[-1].lower()])


def test_paths(task: HarnessTask) -> list[str]:
    """Every file the grader owns for this task: the listed test files and whatever the test patch touches."""
    from_patch = {p for paths, _ in split_patch(task.test_patch or "") for p in paths}
    return sorted(from_patch | set(task.test_files))


def _toml(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _description(task: HarnessTask) -> str:
    return f"{task.category.upper()} task from {task.repo}, graded by {runner_for(task).label}"


def task_toml(task: HarnessTask) -> str:
    runner = runner_for(task)
    targets = len(task.f2p) + len(task.n2p) + len(task.s2p)
    return "\n".join(
        [
            f"schema_version = {_toml(SCHEMA_VERSION)}",
            "artifacts = []",
            "",
            "[task]",
            f"name = {_toml(task_name(task))}",
            'version = "1.0.0"',
            f"description = {_toml(_description(task))}",
            'authors = [{ name = "Gauntlet" }]',
            f"keywords = {_toml(['gauntlet', task.category, runner.language, runner.name])}",
            "",
            "[metadata]",
            f"instance_id = {_toml(task.instance_id)}",
            f"repo = {_toml(task.repo)}",
            f"base_commit = {_toml(task.base_commit)}",
            f"category = {_toml(task.category)}",
            f"runner = {_toml(runner.name)}",
            f"statement_source = {_toml(task.statement_source)}",
            f"target_tests = {targets}",
            f"p2p_tests = {len(task.p2p)}",
            f"env_fingerprint = {_toml(task.env_fingerprint)}",
            "",
            "[verifier]",
            f"timeout_sec = {VERIFIER_TIMEOUT_SEC}",
            "",
            "[agent]",
            f"timeout_sec = {AGENT_TIMEOUT_SEC}",
            "",
            "[environment]",
            f"build_timeout_sec = {BUILD_TIMEOUT_SEC}",
            f"cpus = {math.ceil(runner.cpus)}",
            f"memory_mb = {_memory_mb(runner.memory)}",
            f"workdir = {_toml(task.workdir)}",
            "",
        ]
    )


def dockerfile(task: HarnessTask) -> str:
    """The env image plus the repo at the base commit and nothing after it.

    The fetch asks for that one commit, so the fix that landed later is not in the object
    store for an agent to read back out of git.
    """
    runner = runner_for(task)
    wd = shlex.quote(task.workdir)
    checkout = (
        f"rm -rf {wd} && mkdir -p {wd} && cd {wd} && git init -q && "
        f"git remote add origin {shlex.quote(task.repo)} && "
        f"git fetch -q --depth 1 origin {task.base_commit} && git checkout -q FETCH_HEAD && "
        "git remote remove origin && rm -f .git/FETCH_HEAD"
    )
    return "\n".join(
        [
            f"FROM {task.image}",
            f"RUN {checkout}",
            f"WORKDIR {task.workdir}",
            f"RUN {runner.reinstall}",
            "",
        ]
    )


def instruction(task: HarnessTask) -> str:
    runner = runner_for(task)
    return (
        f"{task.problem_statement.strip()}\n\n"
        "---\n\n"
        f"The repository is checked out at `{task.workdir}`. Fix the problem in the source code. "
        f"The project is tested with {runner.label}. Hidden tests decide the result, and changes "
        "to the existing test files are thrown away before grading.\n"
    )


SOLVE_SH = """\
#!/bin/bash
set -euo pipefail
cd {workdir}
git apply --whitespace=nowarn "$(dirname "$0")/fix.patch"
"""

TEST_SH = """\
#!/bin/bash
# Copied to /tests and run after the agent is done. grade.py writes the reward itself, and the
# fallback is here so a crash in the grader can never read as a pass.
mkdir -p /logs/verifier
python3 /tests/grade.py || echo 0 > /logs/verifier/reward.txt
test -s /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt
"""

GRADE_PY = (Path(__file__).parent / "harbor_grade.py").read_text()


def verifier_spec(task: HarnessTask, *, strict: bool = True) -> dict:
    runner = runner_for(task)
    return {
        "instance_id": task.instance_id,
        "workdir": task.workdir,
        "base_commit": task.base_commit,
        "runner": runner.name,
        "strict": strict,
        "junit_path": JUNIT_PATH,
        "suite_command": suite_command(runner.name, JUNIT_PATH, task.test_scope, keep_going=True),
        "test_paths": test_paths(task),
        "targets": [*task.f2p, *task.n2p, *task.s2p],
        "p2p": task.p2p,
    }


def rubric_items(task: HarnessTask) -> list[dict]:
    items = task.rubric or default_rubric(task)
    return [
        {
            **item.model_dump(),
            "weight": item.effective_weight,
            "scored_by": "verifier" if item.rule in IN_CONTAINER_RULES else "harness",
        }
        for item in items
    ]


def export_task(task: HarnessTask, out_dir: Path, *, strict: bool = True) -> Path:
    """Write one Harbor task directory under `out_dir` and return its path."""
    runner = runner_for(task)
    if runner.name not in SUPPORTED_RUNNERS:
        raise HarborExportError(
            f"{task.instance_id}: the Harbor verifier needs Python in the env image, "
            f"and {runner.name} images have none"
        )
    if not task.test_patch.strip():
        raise HarborExportError(
            f"{task.instance_id}: no test patch, so there is nothing hidden to grade with"
        )
    root = Path(out_dir) / task_name(task).split("/", 1)[1]
    for sub in ("environment", "solution", "tests"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "task.toml").write_text(task_toml(task))
    (root / "instruction.md").write_text(instruction(task))
    (root / "environment" / "Dockerfile").write_text(dockerfile(task))
    solve = root / "solution" / "solve.sh"
    solve.write_text(SOLVE_SH.format(workdir=shlex.quote(task.workdir)))
    solve.chmod(0o755)
    (root / "solution" / "fix.patch").write_text(task.gold_patch)
    test = root / "tests" / "test.sh"
    test.write_text(TEST_SH)
    test.chmod(0o755)
    (root / "tests" / "grade.py").write_text(GRADE_PY)
    (root / "tests" / "test.patch").write_text(task.test_patch)
    (root / "tests" / "task.json").write_text(json.dumps(verifier_spec(task, strict=strict), indent=1) + "\n")
    (root / "tests" / "rubric.json").write_text(json.dumps(rubric_items(task), indent=1) + "\n")
    return root
