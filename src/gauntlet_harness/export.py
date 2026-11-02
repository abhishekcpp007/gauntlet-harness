"""Turn tasks and episodes into the formats other tools already read.

    swebench  tasks -> SWE-bench instances (FAIL_TO_PASS / PASS_TO_PASS as JSON strings), so a task
              set built here drops into any SWE-bench style harness
    sft       episodes -> chat transcripts with tool calls, keeping only clean, resolved runs
              (rejection sampling, as in SWE-Gym and R2E-Gym)
    grpo      episodes -> one row per episode with a group-relative advantage,
              (r - mean(group)) / std(group), groups being all attempts on one task (Shao et al., 2024)
    dpo       episodes -> chosen / rejected pairs on the same task, the best clean run against the worst

Every format drops episodes that Cheat Catcher flagged, errored, or don't have a recorded transcript.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .schema import EpisodeResult, HarnessTask

CALIBRATION = frozenset({"oracle", "noop", "cheater"})

SYSTEM = (
    "You are a software engineer fixing an issue in a Python repository. Use the bash and edit tools to "
    "explore and change the code, then call submit."
)

# The env's action space as function-calling tool schemas.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the repository. No network.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "View, create or edit a file (same fields as Anthropic's text editor tool).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"]},
                    "path": {"type": "string"},
                    "file_text": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                    "insert_line": {"type": "integer"},
                    "insert_text": {"type": "string"},
                    "view_range": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["command", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit",
            "description": "Finish. The working tree is graded.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ------------------------------------------------------------------------------ tasks


def to_swebench(task: HarnessTask) -> dict[str, Any]:
    """One SWE-bench instance. Extra keys (image, transitions) ride along and are ignored by SWE-bench."""
    return {
        "repo": task.pr.repo,
        "instance_id": task.instance_id,
        "base_commit": task.base_commit,
        "patch": task.gold_patch,
        "test_patch": task.test_patch,
        "problem_statement": task.problem_statement,
        "hints_text": "",
        "created_at": task.pr.merged_at or "",
        "version": "",
        "environment_setup_commit": task.base_commit,
        "FAIL_TO_PASS": json.dumps(task.targets),
        "PASS_TO_PASS": json.dumps(task.p2p),
        # gauntlet extras
        "pr_url": task.pr.url,
        "image": task.image,
        "statement_source": task.statement_source,
        "transitions": {"f2p": task.f2p, "n2p": task.n2p, "s2p": task.s2p, "p2p": len(task.p2p)},
    }


# --------------------------------------------------------------------------- episodes


def clean(r: EpisodeResult) -> bool:
    return r.error is None and r.cheat_verdict != "cheating"


def solved(r: EpisodeResult) -> bool:
    return clean(r) and bool(r.grade and r.grade.resolved)


def load_record(r: EpisodeResult) -> dict[str, Any] | None:
    if not r.rl_path or not Path(r.rl_path).exists():
        return None
    rec = json.loads(Path(r.rl_path).read_text())
    return rec if rec.get("steps") else None


def to_chat(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The episode as system/user/assistant/tool messages with function calls, one call per step."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": record.get("prompt", "")},
    ]
    for i, step in enumerate(record["steps"]):
        action = dict(step["action"])
        name = action.pop("type", "unknown")
        call_id = f"call_{i}"
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(action)},
                    }
                ],
            }
        )
        if name == "submit":
            break  # what comes back from submit is grader output, not something to learn from
        messages.append({"role": "tool", "tool_call_id": call_id, "content": step["obs"]})
    return messages


def _keep_policy(policy: str, include_calibration: bool) -> bool:
    return include_calibration or policy not in CALIBRATION


def sft_rows(
    results: Iterable[EpisodeResult], *, min_reward: float = 0.5, include_calibration: bool = False
) -> Iterator[dict[str, Any]]:
    for r in results:
        if not (_keep_policy(r.policy, include_calibration) and solved(r) and r.reward >= min_reward):
            continue
        record = load_record(r)
        if record is None:
            continue
        yield {
            "episode_id": r.episode_id,
            "instance_id": r.instance_id,
            "policy": r.policy,
            "reward": r.reward,
            "messages": to_chat(record),
            "tools": TOOLS,
        }


def _groups(results: Iterable[EpisodeResult], include_calibration: bool) -> dict[str, list[EpisodeResult]]:
    groups: dict[str, list[EpisodeResult]] = defaultdict(list)
    for r in results:
        if r.error is None and _keep_policy(r.policy, include_calibration):
            groups[r.instance_id].append(r)
    return groups


def grpo_rows(
    results: Iterable[EpisodeResult], *, include_calibration: bool = False, eps: float = 1e-6
) -> Iterator[dict[str, Any]]:
    """Group-relative advantages. A group where every reward is equal carries no signal and is skipped."""
    for instance_id, group in sorted(_groups(results, include_calibration).items()):
        rewards = [r.reward for r in group]
        if len(group) < 2 or max(rewards) - min(rewards) < eps:
            continue
        mean, std = statistics.fmean(rewards), statistics.pstdev(rewards)
        for r in group:
            record = load_record(r)
            yield {
                "episode_id": r.episode_id,
                "instance_id": instance_id,
                "policy": r.policy,
                "reward": r.reward,
                "advantage": (r.reward - mean) / (std + eps),
                "group_size": len(group),
                "messages": to_chat(record) if record else None,
            }


def dpo_rows(
    results: Iterable[EpisodeResult], *, margin: float = 0.5, include_calibration: bool = False
) -> Iterator[dict[str, Any]]:
    """Per task: the best solved clean run as chosen, the lowest-reward run as rejected."""
    for instance_id, group in sorted(_groups(results, include_calibration).items()):
        winners = [r for r in group if solved(r)]
        if not winners:
            continue
        best = max(winners, key=lambda r: r.reward)
        worst = min(group, key=lambda r: r.reward)
        if best.reward - worst.reward < margin:
            continue
        chosen, rejected = load_record(best), load_record(worst)
        if chosen is None or rejected is None:
            continue
        prompt = to_chat(chosen)[:2]
        yield {
            "instance_id": instance_id,
            "prompt": prompt,
            "chosen": to_chat(chosen)[2:],
            "rejected": to_chat(rejected)[2:],
            "chosen_reward": best.reward,
            "rejected_reward": worst.reward,
            "chosen_episode": best.episode_id,
            "rejected_episode": worst.episode_id,
        }


def write_jsonl(rows: Iterable[dict[str, Any]], path: Path) -> int:
    n = 0
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
            n += 1
    return n
