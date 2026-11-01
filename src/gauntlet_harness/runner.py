"""Run policies x tasks x attempts. Every episode writes three things:

- a sandbox trajectory (`trajectories/<episode>.jsonl`, viewable with `timemachine view`)
- an RL record (`rl/<episode>.json`: transcript, actions, reward and its breakdown)
- one line in `results.jsonl`

Runs are resumable: episodes already in results.jsonl are skipped.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .env import SWEEnv
from .policies import make_policy
from .reward import RewardConfig
from .schema import EpisodeResult, HarnessTask

log = logging.getLogger(__name__)


def episode_id(task: HarnessTask, policy: str, attempt: int) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", policy)
    return f"{task.instance_id}__{slug}__{attempt}"


def load_results(path: Path) -> list[EpisodeResult]:
    if not path.exists():
        return []
    return [EpisodeResult.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def run(
    tasks: list[HarnessTask],
    policies: list[str],
    *,
    out_dir: Path,
    attempts: int = 1,
    workers: int = 2,
    max_steps: int = 60,
    cache_dir: Path | None = None,
    reward_config: RewardConfig | None = None,
    judge_client: Any = None,
    judge_model: str = "claude-opus-5-5",
    policy_client: Any = None,
    on_result: Callable[[EpisodeResult], None] | None = None,
    env_kwargs: dict[str, Any] | None = None,
) -> list[EpisodeResult]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trajectories").mkdir(exist_ok=True)
    (out_dir / "rl").mkdir(exist_ok=True)
    results_path = out_dir / "results.jsonl"
    done = {r.episode_id for r in load_results(results_path)}
    lock = threading.Lock()

    jobs = [
        (task, spec, k)
        for task in tasks
        for spec in policies
        for k in range(attempts)
        if episode_id(task, spec, k) not in done
    ]
    log.info("%d episodes to run, %d already done", len(jobs), len(done))

    def one(task: HarnessTask, spec: str, attempt: int) -> EpisodeResult:
        eid = episode_id(task, spec, attempt)
        policy = make_policy(spec, client=policy_client)
        env = SWEEnv(
            task,
            max_steps=max_steps,
            trajectory_path=out_dir / "trajectories" / f"{eid}.jsonl",
            cache_dir=cache_dir,
            reward_config=reward_config,
            judge_client=judge_client,
            judge_model=judge_model,
            **(env_kwargs or {}),
        )
        try:
            policy.run(env)
            result = env.result
            if result is None:  # the policy stopped without submitting
                env.step({"type": "submit"})
                result = env.result
            assert result is not None
        except Exception as e:
            log.exception("episode %s failed", eid)
            result = EpisodeResult(
                episode_id=eid, instance_id=task.instance_id, policy=spec, error=f"{type(e).__name__}: {e}"
            )
        finally:
            env.close()

        result.episode_id, result.policy, result.attempt = eid, policy.name, attempt
        result.usage = dict(policy.usage)
        result.merged_at = task.pr.merged_at
        rl_path = out_dir / "rl" / f"{eid}.json"
        rl_path.write_text(
            json.dumps(
                {
                    "episode_id": eid,
                    "instance_id": task.instance_id,
                    "policy": policy.name,
                    "messages": policy.messages,
                    "actions": env.actions,
                    "reward": result.reward,
                    "reward_breakdown": result.reward_breakdown,
                    "resolved": bool(result.grade and result.grade.resolved),
                },
                default=str,
                indent=1,
            )
        )
        result.rl_path = str(rl_path)
        with lock:
            with results_path.open("a") as f:
                f.write(result.model_dump_json() + "\n")
        if on_result:
            on_result(result)
        return result

    out: list[EpisodeResult] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, *job) for job in jobs]
        for fut in as_completed(futures):
            out.append(fut.result())
    return out
