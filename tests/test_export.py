"""Exports: SWE-bench instances from tasks, SFT / GRPO / DPO rows from episodes. No Docker."""

import json

import pytest

from gauntlet_harness import export
from gauntlet_harness.cli import main
from gauntlet_harness.schema import EpisodeResult, GradeResult, HarnessTask, PRInfo

TASK = HarnessTask(
    instance_id="o__r-7",
    repo="https://github.com/o/r",
    pr=PRInfo(repo="o/r", number=7, title="t", url="https://github.com/o/r/pull/7", merged_at="2026-10-01"),
    base_commit="abc",
    solution_commit="def",
    problem_statement="rows can be short",
    statement_source="issue",
    image="img",
    env_fingerprint="f",
    gold_patch="--- a/x\n+++ b/x\n",
    test_patch="--- a/t\n+++ b/t\n",
    test_files=["tests/test_x.py"],
    f2p=["tests/test_x.py::test_a"],
    n2p=["tests/test_x.py::test_new"],
    p2p=["tests/test_x.py::test_old"],
)


def episode(tmp_path, eid, policy, reward, *, resolved=True, cheat="clean", steps=True, error=None):
    rec = {"prompt": "fix it", "steps": []}
    if steps:
        rec["steps"] = [
            {"action": {"type": "bash", "command": "pytest -q"}, "obs": "1 failed"},
            {"action": {"type": "edit", "command": "str_replace", "path": "x.py",
                        "old_str": "a", "new_str": "b"}, "obs": "edited"},
            {"action": {"type": "submit"}, "obs": "GRADER SECRET OUTPUT"},
        ]  # fmt: skip
    path = tmp_path / f"{eid}.json"
    path.write_text(json.dumps(rec))
    return EpisodeResult(
        episode_id=eid,
        instance_id="o__r-7",
        policy=policy,
        reward=reward,
        grade=GradeResult(instance_id="o__r-7", patch_applied=True, resolved=resolved),
        cheat_verdict=cheat,
        rl_path=str(path),
        error=error,
    )


def test_swebench_instance():
    row = export.to_swebench(TASK)
    assert row["repo"] == "o/r" and row["patch"] == TASK.gold_patch and row["test_patch"] == TASK.test_patch
    assert json.loads(row["FAIL_TO_PASS"]) == ["tests/test_x.py::test_a", "tests/test_x.py::test_new"]
    assert json.loads(row["PASS_TO_PASS"]) == ["tests/test_x.py::test_old"]
    assert row["created_at"] == "2026-10-01"


def test_chat_has_tool_calls_and_hides_grader_output(tmp_path):
    r = episode(tmp_path, "e", "claude:m", 0.9)
    msgs = export.to_chat(export.load_record(r))
    assert [m["role"] for m in msgs] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "bash"
    assert json.loads(msgs[4]["tool_calls"][0]["function"]["arguments"])["new_str"] == "b"
    assert msgs[-1]["tool_calls"][0]["function"]["name"] == "submit"
    assert "GRADER" not in json.dumps(msgs)


def test_sft_keeps_only_clean_solved_runs(tmp_path):
    results = [
        episode(tmp_path, "good", "claude:m", 0.95),
        episode(tmp_path, "low", "claude:m", 0.2),
        episode(tmp_path, "unsolved", "claude:m", 0.4, resolved=False),
        episode(tmp_path, "cheat", "claude:m", 0.99, cheat="cheating"),
        episode(tmp_path, "crash", "claude:m", 0.99, error="boom"),
        episode(tmp_path, "nosteps", "claude:m", 0.99, steps=False),
        episode(tmp_path, "oracle", "oracle", 1.0),
    ]
    assert [r["episode_id"] for r in export.sft_rows(results)] == ["good"]
    with_cal = [r["episode_id"] for r in export.sft_rows(results, include_calibration=True)]
    assert with_cal == ["good", "oracle"]


def test_grpo_advantages_are_group_normalized(tmp_path):
    results = [episode(tmp_path, f"e{i}", "claude:m", r) for i, r in enumerate([1.0, 0.0, -1.0, 0.0])]
    rows = list(export.grpo_rows(results))
    advs = [r["advantage"] for r in rows]
    assert sum(advs) == pytest.approx(0.0, abs=1e-6)
    assert advs[0] > 0 > advs[2] and rows[0]["group_size"] == 4
    # identical rewards carry no signal
    flat = [episode(tmp_path, f"f{i}", "claude:m", 0.5) for i in range(3)]
    assert list(export.grpo_rows(flat)) == []


def test_dpo_pairs_best_clean_against_worst(tmp_path):
    results = [
        episode(tmp_path, "win", "claude:a", 0.97),
        episode(tmp_path, "meh", "claude:b", 0.6),
        episode(tmp_path, "hack", "claude:c", -1.0, resolved=True, cheat="cheating"),
    ]
    (row,) = export.dpo_rows(results)
    assert row["chosen_episode"] == "win" and row["rejected_episode"] == "hack"
    assert row["prompt"][1]["content"] == "fix it" and row["chosen"][0]["role"] == "assistant"
    assert list(export.dpo_rows(results[:2], margin=0.5)) == []  # gap too small


def test_cli_export(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(TASK.model_dump_json() + "\n")
    out = tmp_path / "swe.jsonl"
    assert main(["export", "--format", "swebench", "--tasks", str(tasks), "-o", str(out)]) == 0
    assert json.loads(out.read_text())["instance_id"] == "o__r-7"

    res = tmp_path / "results.jsonl"
    res.write_text(episode(tmp_path, "good", "claude:m", 0.95).model_dump_json() + "\n")
    out = tmp_path / "sft.jsonl"
    assert main(["export", "--format", "sft", "--results", str(res), "-o", str(out)]) == 0
    assert json.loads(out.read_text())["tools"][0]["function"]["name"] == "bash"
    assert main(["export", "--format", "sft", "-o", str(out)]) == 2
