"""SWEEnv: a gym-style RL environment over one task.

    env = SWEEnv(task)
    obs, info = env.reset()
    while True:
        obs, reward, terminated, truncated, info = env.step({"type": "bash", "command": "pytest -q"})
        ...
        if terminated or truncated:
            break
    env.result  # EpisodeResult with grade, rubric, reward breakdown

Actions:
    {"type": "bash", "command": str}
    {"type": "edit", "command": "view" | "create" | "str_replace" | "insert", "path": str, ...}
        (the same fields as Anthropic's text editor tool: file_text, old_str, new_str,
         insert_line, insert_text, view_range)
    {"type": "submit"}

Reward is sparse: 0.0 on every step, the full episode reward on the terminal step. Running
out of steps submits whatever is in the working tree and sets truncated=True.
"""

from __future__ import annotations

import posixpath
import shlex
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gauntlet_cheatcatcher import scan
from gauntlet_repo2env import extract_patch
from gauntlet_sandbox import Sandbox, Trajectory

from .grading import grade_patch
from .reward import RewardConfig, compute_reward
from .rubric import default_rubric, score_rubric
from .sandboxing import boot
from .schema import EpisodeResult, GradeResult, HarnessTask

MAX_OBS = 20_000

INSTRUCTIONS = """\
You're working in a checkout of {repo} at {workdir}, at the commit right before a change was \
merged. Make the change the task describes.

<task>
{statement}
</task>

The project and pytest are installed. There's no network. Hidden tests will grade your work \
after you submit, and a separate checker flags edits to tests or the test runner, so fix the \
real behavior."""


class ActionError(Exception):
    pass


class SWEEnv:
    def __init__(
        self,
        task: HarnessTask,
        *,
        max_steps: int = 60,
        command_timeout: float = 120,
        trajectory_path: str | Path | None = None,
        cache_dir: Path | None = None,
        reward_config: RewardConfig | None = None,
        judge_client: Any = None,
        judge_model: str = "claude-opus-5-5",
        grade_fn: Callable[[HarnessTask, str], GradeResult] | None = None,
    ) -> None:
        self.task = task
        self.max_steps = max_steps
        self.command_timeout = command_timeout
        self.trajectory_path = Path(trajectory_path) if trajectory_path else None
        self.cache_dir = cache_dir
        self.reward_config = reward_config or RewardConfig()
        self.judge_client = judge_client
        self.judge_model = judge_model
        self._grade = grade_fn or (lambda t, p: grade_patch(t, p, cache_dir=cache_dir))
        self.sb: Sandbox | None = None
        self.steps = 0
        self.actions: list[dict[str, Any]] = []
        self.observations: list[str] = []  # one per action, what the agent saw back
        self.prompt = ""
        self.result: EpisodeResult | None = None
        self._agent_start = 0
        self._started = 0.0

    # ------------------------------------------------------------------ gym api

    def reset(self) -> tuple[str, dict[str, Any]]:
        self.close()
        self.sb = boot(
            self.task,
            apply_tests=False,
            baseline_git=True,
            timeout=self.command_timeout,
            trajectory_path=self.trajectory_path,
            record=True,
            cache_dir=self.cache_dir,
        )
        self._agent_start = len(self.sb.trajectory) if self.sb.trajectory else 0
        self.steps, self.actions, self.observations, self.result = 0, [], [], None
        self._started = time.time()
        obs = INSTRUCTIONS.format(
            repo=self.task.pr.repo,
            workdir=self.task.workdir,
            statement=self.task.problem_statement.strip(),
        )
        info = {
            "instance_id": self.task.instance_id,
            "workdir": self.task.workdir,
            "max_steps": self.max_steps,
        }
        self.prompt = obs
        return obs, info

    def step(self, action: dict[str, Any]) -> tuple[str, float, bool, bool, dict[str, Any]]:
        out = self._step(action)
        self.observations.append(out[0])
        return out

    def _step(self, action: dict[str, Any]) -> tuple[str, float, bool, bool, dict[str, Any]]:
        if self.sb is None:
            raise RuntimeError("call reset() first")
        if self.result is not None:
            raise RuntimeError("episode is over, call reset() for a new one")

        self.steps += 1
        self.actions.append(action)
        kind = action.get("type")
        if kind == "submit":
            return self._finish(truncated=False)

        info: dict[str, Any] = {"step": self.steps}
        try:
            if kind == "bash":
                obs = self._bash(action)
            elif kind == "edit":
                obs = self._edit(action)
            else:
                raise ActionError(f"unknown action type {kind!r}, use bash, edit or submit")
            info["error"] = False
        except ActionError as e:
            obs, info["error"] = f"Error: {e}", True

        if self.steps >= self.max_steps:
            final = self._finish(truncated=True)
            return f"{obs}\n\n[step limit reached, submitted]", *final[1:]
        return obs[-MAX_OBS:], 0.0, False, False, info

    def close(self) -> None:
        if self.sb is not None:
            self.sb.close()
            self.sb = None

    def __enter__(self) -> SWEEnv:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ actions

    def _bash(self, action: dict[str, Any]) -> str:
        assert self.sb is not None
        command = action.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ActionError("bash needs a non-empty command")
        res = self.sb.exec(command, workdir=self.task.workdir, timeout=self.command_timeout)
        out = (res.stdout + res.stderr)[-MAX_OBS:]
        if res.timed_out:
            raise ActionError(f"{out}\n[killed after {self.command_timeout:g}s]")
        return f"{out}\n[exit code {res.exit_code}]" if res.exit_code else (out or "[no output]")

    def _path(self, raw: Any) -> str:
        if not isinstance(raw, str) or not raw:
            raise ActionError("a path is required")
        root = self.task.workdir.rstrip("/")
        full = posixpath.normpath(raw if raw.startswith("/") else posixpath.join(root, raw))
        if full != root and not full.startswith(root + "/"):
            raise ActionError(f"{raw} is outside the repo at {root}")
        return full

    def _read(self, path: str) -> str:
        assert self.sb is not None
        try:
            return self.sb.read_text(path)
        except FileNotFoundError as e:
            raise ActionError(f"{path} doesn't exist") from e
        except (IsADirectoryError, UnicodeDecodeError) as e:
            raise ActionError(f"can't open {path} as text") from e

    def _edit(self, action: dict[str, Any]) -> str:
        assert self.sb is not None
        command = action.get("command")
        path = self._path(action.get("path"))

        if command == "view":
            q = shlex.quote(path)
            if self.sb.exec(f"test -d {q}").ok:
                return self.sb.exec(f"find {q} -maxdepth 2 -not -path '*/.git*' | sort | head -400").stdout
            lines = self._read(path).splitlines()
            start, end = 1, len(lines)
            if action.get("view_range"):
                start, end = action["view_range"]
                end = len(lines) if end == -1 else end
            shown = [f"{n:6}\t{lines[n - 1]}" for n in range(max(1, start), min(end, len(lines)) + 1)]
            return "\n".join(shown)[:MAX_OBS] or "[empty file]"

        if command == "create":
            text = action.get("file_text")
            if not isinstance(text, str):
                raise ActionError("create needs file_text")
            self.sb.write_file(path, text)
            return f"wrote {path}"

        if command == "str_replace":
            old, new = action.get("old_str"), action.get("new_str") or ""
            if not isinstance(old, str) or not old:
                raise ActionError("str_replace needs a non-empty old_str")
            text = self._read(path)
            count = text.count(old)
            if count != 1:
                raise ActionError(f"old_str matched {count} times in {path}, it must match exactly once")
            self.sb.write_file(path, text.replace(old, new, 1))
            return f"edited {path}"

        if command == "insert":
            line = action.get("insert_line")
            new_text = action.get("insert_text", action.get("new_str"))
            if not isinstance(line, int) or not isinstance(new_text, str):
                raise ActionError("insert needs insert_line and insert_text")
            lines = self._read(path).splitlines(keepends=True)
            if not 0 <= line <= len(lines):
                raise ActionError(f"insert_line must be between 0 and {len(lines)}")
            lines.insert(line, new_text if new_text.endswith("\n") else new_text + "\n")
            self.sb.write_file(path, "".join(lines))
            return f"inserted into {path}"

        raise ActionError(f"unknown edit command {command!r}")

    # ------------------------------------------------------------------ the end

    def _finish(self, *, truncated: bool) -> tuple[str, float, bool, bool, dict[str, Any]]:
        assert self.sb is not None
        sb, task = self.sb, self.task
        traj = sb.trajectory
        agent_end = len(traj) if traj else 0
        try:
            patch = extract_patch(sb, task.to_repo2env())
        finally:
            self.close()

        agent_traj = None
        if traj is not None:
            agent_traj = Trajectory(traj.header)
            agent_traj.steps = traj.steps[self._agent_start : agent_end]
        cheat = scan(agent_traj, patch, protected_paths=task.test_files)

        grade = self._grade(task, patch)
        items = task.rubric or default_rubric(task)
        rubric = score_rubric(
            task, patch, grade, cheat, items, client=self.judge_client, model=self.judge_model
        )
        reward, breakdown = compute_reward(
            grade, rubric, cheat.verdict, patch, task.gold_patch, self.reward_config, task=task
        )
        self.result = EpisodeResult(
            episode_id=uuid.uuid4().hex[:12],
            instance_id=task.instance_id,
            policy="",
            steps=self.steps,
            seconds=round(time.time() - self._started, 2),
            patch=patch,
            grade=grade,
            rubric=rubric,
            cheat_verdict=cheat.verdict,
            cheat_findings=[f.model_dump() for f in cheat.findings],
            reward=reward,
            reward_breakdown=breakdown,
            truncated=truncated,
            trajectory_path=str(self.trajectory_path) if self.trajectory_path else None,
            merged_at=task.pr.merged_at,
        )
        summary = (
            f"submitted. resolved={grade.resolved} reward={reward:.3f} "
            f"cheat={cheat.verdict} rubric={rubric.score:.2f}"
        )
        return summary, reward, True, truncated, {"result": self.result}
