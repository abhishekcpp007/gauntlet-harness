#!/usr/bin/env python3
"""The verifier a Gauntlet task carries into Harbor. Standard library only: it runs inside the
task's container, where the harness isn't installed.

Order matters. The agent's edits to the graded test files are noted and then discarded, the
hidden tests go in, the suite runs once, and the reward is 1 only if every target test passed
and nothing that passed before broke. The rubric is scored last, from the same results.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
LOGS = Path(os.environ.get("GAUNTLET_VERIFIER_LOGS", "/logs/verifier"))
SUITE_TIMEOUT_SEC = 1500
SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".rb", ".java")
PASS_MARK = 0.5


def sh(cmd: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def test_id(case: ET.Element, suite_file: str | None) -> str:
    """The same id the harness gives a test, so a task's lists mean here what they meant there."""
    name = case.get("name", "?")
    classname = case.get("classname", "")
    file = case.get("file") or suite_file
    if not file:
        return name if not classname or classname == name else f"{classname}::{name}"
    module = str(PurePosixPath(file).with_suffix("")).replace("/", ".")
    if classname.startswith(module + "."):
        return f"{file}::{classname[len(module) + 1 :]}::{name}"
    return f"{file}::{name}"


def parse_junit(xml_text: str) -> dict[str, str]:
    results: dict[str, str] = {}
    root = ET.fromstring(xml_text)
    for suite in list(root.iter("testsuite")) or [root]:
        name = suite.get("name")
        suite_file = suite.get("file") or (name if name and name.endswith(SOURCE_SUFFIXES) else None)
        for case in suite.findall("testcase"):
            status = "passed"
            for child in case:
                if child.tag == "failure":
                    status = "failed"
                elif child.tag == "error":
                    status = "error"
                elif child.tag == "skipped":
                    status = "skipped"
            results[test_id(case, suite_file)] = status
    return results


def changed_paths(base: str) -> set[str] | None:
    diff = sh(f"git diff --name-only {base}")
    new = sh("git ls-files --others --exclude-standard")
    if diff.returncode or new.returncode:
        return None
    return {p for p in (diff.stdout + new.stdout).splitlines() if p}


def restore(paths: list[str], base: str) -> None:
    for path in paths:
        tracked = sh(f"git cat-file -e {base}:{json.dumps(path)}").returncode == 0
        if tracked:
            sh(f"git checkout -q {base} -- {json.dumps(path)}")
        elif not Path(path).is_dir():
            # a path can be a submodule, which the base commit records as a folder and not a file
            Path(path).unlink(missing_ok=True)


def score_rubric(items: list[dict], facts: dict[str, float]) -> dict:
    """Rubrics as Rewards over the items the verifier can see. A pitfall subtracts when met."""
    scored, waiting = [], []
    positive = penalty = denom = 0.0
    for item in items:
        good = facts.get(item.get("rule") or "")
        if item.get("scored_by") != "verifier" or good is None:
            waiting.append(item["id"])
            continue
        if item["category"] == "pitfall":
            met = good < PASS_MARK
            penalty += item["weight"] * (1.0 if met else 0.0)
            scored.append({"id": item["id"], "category": "pitfall", "met": met, "score": 1.0 - good})
        else:
            positive += item["weight"] * good
            denom += item["weight"]
            scored.append(
                {"id": item["id"], "category": item["category"], "met": good >= PASS_MARK, "score": good}
            )
    total = min(1.0, max(0.0, (positive - penalty) / denom)) if denom else 0.0
    return {"score": round(total, 4), "items": scored, "left_to_the_harness": waiting}


def finish(reward: int, **detail: object) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "gauntlet.json").write_text(json.dumps({"reward": reward, **detail}, indent=1) + "\n")
    (LOGS / "reward.txt").write_text(f"{reward}\n")
    print(json.dumps({"reward": reward, **{k: v for k, v in detail.items() if k != "rubric"}}))
    sys.exit(0)


def rate(ids: list[str], results: dict[str, str]) -> float:
    return sum(results.get(t) == "passed" for t in ids) / len(ids) if ids else 1.0


def main() -> None:
    task = json.loads((HERE / "task.json").read_text())
    rubric = json.loads((HERE / "rubric.json").read_text())
    base, targets, p2p = task["base_commit"], task["targets"], task["p2p"]
    os.chdir(task["workdir"])

    changed = changed_paths(base)
    if changed is None:
        finish(0, resolved=False, error="the repository's git metadata is gone, so the run can't be checked")
    tampered = sorted(changed & set(task["test_paths"]))

    restore(task["test_paths"], base)
    applied = sh(f"git apply --whitespace=nowarn {HERE / 'test.patch'}")
    if applied.returncode:
        finish(
            0,
            resolved=False,
            tampered=tampered,
            error="the hidden tests did not apply: " + applied.stderr[-300:],
        )

    try:
        sh(task["suite_command"], timeout=SUITE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        finish(0, resolved=False, tampered=tampered, error="the test suite timed out")
    try:
        results = parse_junit(Path(task["junit_path"]).read_text())
    except (OSError, ET.ParseError) as exc:
        finish(0, resolved=False, tampered=tampered, error=f"the suite left no readable report: {exc}")

    failed_targets = [t for t in targets if results.get(t) != "passed"]
    broken = [t for t in p2p if results.get(t) != "passed"]
    resolved = bool(targets) and not failed_targets and not broken
    reward = 1 if resolved and not (task["strict"] and tampered) else 0
    facts = {
        "targets_fixed": rate(targets, results),
        "no_regressions": rate(p2p, results),
        "no_test_tampering": 0.0 if tampered else 1.0,
    }
    finish(
        reward,
        resolved=resolved,
        strict=task["strict"],
        tampered=tampered,
        targets={"total": len(targets), "failed": failed_targets},
        p2p={"total": len(p2p), "broken": broken},
        rubric=score_rubric(rubric, facts),
    )


if __name__ == "__main__":
    main()
