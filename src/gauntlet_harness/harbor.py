"""Gauntlet tasks as Harbor tasks, and the grader that runs inside Harbor's verifier.

One task becomes one folder in Harbor's layout:

    <instance_id>/
        task.toml              agent offline, verifier in a separate fresh container
        instruction.md         the same prompt a Gauntlet episode starts with
        environment/
            Dockerfile         the env, rebuilt at the base commit, plus the grader in /opt/gauntlet
            install.sh         repo2env's install script for that commit
        solution/
            solve.sh           applies the merged fix, so Harbor's oracle agent can check the task
            gold.diff
        tests/
            test.sh            runs `gauntlet harbor-grade`
            task.json          the full task, hidden tests included, only ever sent to the verifier

The verifier never shares a container with the agent. A collect hook diffs the agent's tree into
/logs/artifacts, Harbor carries that file into a fresh copy of the env, and the grader runs there
with the canaries, the renamed targets and the file hashes, exactly as `grade_patch` does in its
own sandbox.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from gauntlet_cheatcatcher.models import Finding
from gauntlet_repo2env.detect import detect, install_script
from gauntlet_repo2env.env import INSTALL_LOG, PACKAGE_CACHES, base_image_for
from gauntlet_sandbox.models import ExecResult

from .env import instructions, scan_beyond_gold
from .grading import grade_in
from .profiles import profile_for
from .reward import compute_reward
from .runners import runner_for
from .sandboxing import SCRATCH_GIT
from .schema import HarnessTask

HARNESS_URL = "https://github.com/abhishekcpp007/gauntlet-harness"
UV_IMAGE = "ghcr.io/astral-sh/uv:0.7.13"
GRADER_PYTHON = "3.12"
GRADER_ROOT = "/opt/gauntlet"
GRADER_BIN = f"{GRADER_ROOT}/venv/bin/gauntlet"
PATCH_ARTIFACT = "/logs/artifacts/gauntlet.patch"
VERIFIER_LOGS = "/logs/verifier"
FAILURE_TAIL_BYTES = 3000
MAX_OUTPUT_BYTES = 200_000
BUILD_TIMEOUT_SEC = 1800
STORAGE_MB = 10240
COLLECT_TIMEOUT_SEC = 120
GRADE_MARGIN_SEC = 600  # reinstall, fingerprints and the compile checks on top of the suite itself
MIB_PER_GIB = 1024


def agent_timeout(task: HarnessTask) -> float:
    """Gauntlet counts actions and Harbor counts seconds, so the agent gets the action budget
    times the per-command timeout, which no Gauntlet episode could go past."""
    profile = profile_for(task)
    return profile.max_steps * profile.command_timeout


def _memory_mb(spec: str) -> int:
    """Docker's '512m' or '2g' as megabytes."""
    unit = spec[-1].lower()
    amount = float(spec[:-1])
    return int(amount * MIB_PER_GIB) if unit == "g" else int(amount)


# ------------------------------------------------------------------ export


def _toml_str(value: str) -> str:
    return json.dumps(value)


def task_toml(task: HarnessTask, agent_hosts: tuple[str, ...] = ()) -> str:
    """The agent's container starts offline. `agent_hosts` opens an allowlist for an agent that
    calls a model API from inside it; GitHub must never be on it, or the merged fix is a fetch
    away. Network policy is set when a container starts, since local Docker can't switch it later."""
    runner = runner_for(task)
    profile = profile_for(task)
    collect = f"cd {shlex.quote(task.workdir)} && git add -A && git diff --cached --binary > {PATCH_ARTIFACT}"
    resources = [
        f"cpus = {max(1, int(runner.cpus))}",
        f"memory_mb = {_memory_mb(runner.memory)}",
        f"storage_mb = {STORAGE_MB}",
        "gpus = 0",
    ]
    if agent_hosts:
        hosts = ", ".join(_toml_str(h) for h in agent_hosts)
        network = ['network_mode = "allowlist"', f"allowed_hosts = [{hosts}]"]
    else:
        network = ['network_mode = "no-network"']
    keywords = ", ".join(_toml_str(k) for k in ("gauntlet", task.category, runner.name))
    lines = [
        'schema_version = "1.0"',
        "",
        "[task]",
        f"name = {_toml_str('gauntlet/' + task.instance_id)}",
        f"description = {_toml_str(f'{task.pr.repo} #{task.pr.number}: {task.pr.title}')}",
        f"keywords = [{keywords}]",
        "",
        "[metadata]",
        f"category = {_toml_str(task.category)}",
        f"runner = {_toml_str(runner.name)}",
        f"repo = {_toml_str(task.repo)}",
        f"pr_url = {_toml_str(task.pr.url)}",
        f"base_commit = {_toml_str(task.base_commit)}",
        f"env_fingerprint = {_toml_str(task.env_fingerprint)}",
        f"gauntlet_format = {_toml_str(task.format)}",
        "",
        "[agent]",
        f"timeout_sec = {agent_timeout(task):.1f}",
        "",
        "[verifier]",
        'environment_mode = "separate"',
        f"timeout_sec = {profile.grade_timeout + GRADE_MARGIN_SEC:.1f}",
        "",
        "[[verifier.collect]]",
        f"command = {_toml_str(collect)}",
        f"timeout_sec = {COLLECT_TIMEOUT_SEC:.1f}",
        "",
        # its own fresh container from the same Dockerfile, offline from the start
        "[verifier.environment]",
        'network_mode = "no-network"',
        *resources,
        "",
        "[environment]",
        *network,
        f"build_timeout_sec = {BUILD_TIMEOUT_SEC:.1f}",
        *resources,
        "",
    ]
    return "\n".join(lines)


def dockerfile(task: HarnessTask, base_image: str, grader_ref: str) -> str:
    runner = runner_for(task)
    wd = task.workdir
    caches = " ".join(PACKAGE_CACHES)
    grader = f"gauntlet-harness @ git+{HARNESS_URL}@{grader_ref}"
    return f"""\
FROM {base_image}

RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\
    --no-install-recommends git ca-certificates >/dev/null

# the repo at the base commit, without its history, the way Gauntlet's sandbox gets it
RUN mkdir -p {wd} && git init -q /tmp/src && cd /tmp/src \\
    && git fetch -q --depth 1 {shlex.quote(task.repo)} {task.base_commit} \\
    && git archive FETCH_HEAD | tar -x -C {wd} && rm -rf /tmp/src

COPY install.sh /tmp/gauntlet-install.sh
RUN cd {wd} && ( sh /tmp/gauntlet-install.sh ) > {INSTALL_LOG} 2>&1 \\
    || (tail -c {FAILURE_TAIL_BYTES} {INSTALL_LOG}; exit 1)
RUN cd {wd} && {runner.reinstall}

# the grader lives outside the repo with its own Python, so the project's deps never meet its deps
COPY --from={UV_IMAGE} /uv {GRADER_ROOT}/bin/uv
ENV UV_PYTHON_INSTALL_DIR={GRADER_ROOT}/python
RUN {GRADER_ROOT}/bin/uv venv -q --python {GRADER_PYTHON} {GRADER_ROOT}/venv \\
    && {GRADER_ROOT}/bin/uv pip install -q \\
       --python {GRADER_ROOT}/venv/bin/python {shlex.quote(grader)} \\
    && rm -rf /root/.cache/uv

RUN rm -rf /tmp/gauntlet-* /var/lib/apt/lists/* {caches}

# a throwaway one-commit repo, so the collect hook can diff whatever the agent changed
RUN cd {wd} && git init -q && git add -A && {SCRATCH_GIT} commit -q -m base

WORKDIR {wd}
"""


SOLVE_SH = """\
#!/bin/bash
set -euo pipefail
cd {workdir}
git apply --whitespace=nowarn /solution/gold.diff
"""

TEST_SH = f"""\
#!/bin/bash
set -uo pipefail
mkdir -p {VERIFIER_LOGS}
{GRADER_BIN} harbor-grade --task /tests/task.json --patch {PATCH_ARTIFACT} --out {VERIFIER_LOGS}
"""


def export_task(
    task: HarnessTask,
    dest: Path,
    *,
    grader_ref: str,
    agent_hosts: tuple[str, ...] = (),
    cache_dir: Path | None = None,
) -> Path:
    """Write one task in Harbor's layout under dest/<instance_id>. Needs the repo clone, for the
    install script of the base commit."""
    from .build import fetch_commit
    from .sandboxing import cached_clone

    repo = cached_clone(task.repo, cache_dir)
    fetch_commit(repo, task.base_commit)
    stack = detect(repo, task.base_commit)
    python = str(task.validation.get("python", GRADER_PYTHON))

    root = dest / task.instance_id
    for sub in ("environment", "solution", "tests"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "task.toml").write_text(task_toml(task, agent_hosts))
    (root / "instruction.md").write_text(instructions(task, profile_for(task).max_steps) + "\n")
    (root / "environment" / "Dockerfile").write_text(
        dockerfile(task, base_image_for(stack, python), grader_ref)
    )
    (root / "environment" / "install.sh").write_text(install_script(stack) + "\n")
    gold = task.gold_patch if task.gold_patch.endswith("\n") else task.gold_patch + "\n"
    (root / "solution" / "gold.diff").write_text(gold)
    solve = root / "solution" / "solve.sh"
    solve.write_text(SOLVE_SH.format(workdir=shlex.quote(task.workdir)))
    test = root / "tests" / "test.sh"
    test.write_text(TEST_SH)
    for script in (solve, test):
        script.chmod(0o755)
    (root / "tests" / "task.json").write_text(task.model_dump_json(indent=2) + "\n")
    return root


# ------------------------------------------------------------------ inside the verifier


class LocalSandbox:
    """The few sandbox calls the grader makes, run directly in the container it's already in.

    The verifier is a fresh copy of the task's env that never saw the agent, which is the
    sandbox `grade_patch` would have booted, so nothing here needs Docker.
    """

    def __init__(self, workdir: str, *, default_timeout: float, max_output_bytes: int = MAX_OUTPUT_BYTES):
        self.workdir = workdir
        self.default_timeout = default_timeout
        self.max_output_bytes = max_output_bytes
        self.metadata: dict[str, Any] = {}
        # the grader's own venv must not leak into the project's test run
        venv_bin = str(Path(GRADER_BIN).parent)
        path = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p != venv_bin]
        self.env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME")}
        self.env["PATH"] = os.pathsep.join(path)

    def _resolve(self, path: str) -> Path:
        return Path(path) if os.path.isabs(path) else Path(self.workdir) / path

    def exec(
        self, command: str, *, timeout: float | None = None, workdir: str | None = None, env=None
    ) -> ExecResult:
        secs = timeout or self.default_timeout
        started = time.monotonic()
        merged = {**self.env, **(env or {})}
        try:
            proc = subprocess.run(
                ["sh", "-c", command],
                cwd=workdir or self.workdir,
                env=merged,
                capture_output=True,
                timeout=secs,
                start_new_session=True,
            )
            code, out, err, timed_out = proc.returncode, proc.stdout, proc.stderr, False
        except subprocess.TimeoutExpired as e:
            code, out, err, timed_out = -1, e.stdout or b"", e.stderr or b"", True
        cap = self.max_output_bytes
        return ExecResult(
            exit_code=code,
            stdout=out[:cap].decode("utf-8", errors="replace"),
            stderr=err[:cap].decode("utf-8", errors="replace"),
            duration=time.monotonic() - started,
            timed_out=timed_out,
            truncated=len(out) > cap or len(err) > cap,
        )

    def write_file(self, path: str, content: str | bytes, *, mode: int = 0o644) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        target.chmod(mode)
        return str(target)

    def read_file(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def read_text(self, path: str) -> str:
        return self.read_file(path).decode("utf-8")

    def close(self) -> None:
        pass


def grade_here(task: HarnessTask, patch: str) -> dict[str, Any]:
    """Grade the agent's patch in this container and turn it into Gauntlet's reward.

    Every check `SWEEnv` makes on the patch runs here too: the grader's runtime tamper checks
    and Cheat Catcher's patch rules. Rubric judging is left out, because the verifier runs
    offline, so its weight is spread over the rest the way `compute_reward` already does.
    """
    profile = profile_for(task)
    runner = runner_for(task)
    sb = LocalSandbox(task.workdir, default_timeout=profile.grade_timeout)
    # the env image carries a scratch repo for the agent's diff, the grader works on a plain tree
    sb.exec("rm -rf .git")
    res = sb.exec(runner.reinstall, timeout=profile.grade_timeout)
    if "REINSTALL_FAILED" in res.stdout:
        raise RuntimeError("couldn't reinstall the project in the verifier")

    cheat = scan_beyond_gold(None, patch, task)
    grade = grade_in(sb, task, patch, timeout=profile.grade_timeout)
    for problem in grade.tampered:
        cheat.findings.append(Finding(rule="grader-tamper", severity="high", message=problem))
    findings = [f.model_dump() for f in cheat.findings]
    verdict = cheat.verdict
    reward, breakdown = compute_reward(
        grade, None, verdict, patch, task.gold_patch, profile.reward, task=task
    )
    return {
        "instance_id": task.instance_id,
        "reward": reward,
        "resolved": grade.resolved,
        "cheat_verdict": verdict,
        "cheat_findings": findings,
        "reward_breakdown": breakdown,
        "grade": grade.model_dump(),
    }


def harbor_grade(task_path: Path, patch_path: Path, out_dir: Path) -> int:
    """Entry point for tests/test.sh. Always leaves a reward behind, so Harbor never reads a
    crashed verifier as a missing one."""
    out_dir.mkdir(parents=True, exist_ok=True)
    task = HarnessTask.model_validate_json(task_path.read_text())
    patch = patch_path.read_text(errors="surrogateescape") if patch_path.exists() else ""
    try:
        report = grade_here(task, patch)
    except Exception as e:  # noqa: BLE001 - the reward file is the contract, whatever broke
        report = {"instance_id": task.instance_id, "reward": 0.0, "resolved": False, "error": repr(e)}
    (out_dir / "gauntlet.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    rewards = {"reward": report["reward"], "resolved": float(report["resolved"])}
    (out_dir / "reward.json").write_text(json.dumps(rewards) + "\n")
    print(
        json.dumps(
            {k: report.get(k) for k in ("instance_id", "reward", "resolved", "cheat_verdict", "error")}
        )
    )
    return 0
