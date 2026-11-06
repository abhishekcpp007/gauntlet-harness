"""What the cheat detector does to honest work, measured on real merged PRs.

A detector that flags real fixes is worse than no detector, because an episode it calls
cheating earns -1 whatever the tests did. The repo's own tests cover two hand-written honest
examples, which checks the idea but cannot give a rate. Every gold patch in the shipped live
set is a fix a human wrote and a maintainer merged, so 53 of them is a real sample.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet_harness.stats import wilson

cheatcatcher = pytest.importorskip("gauntlet_cheatcatcher")

DATASET = Path(__file__).resolve().parent.parent / "datasets" / "gauntlet-live-2026-11" / "tasks.jsonl"


def gold_patches():
    if not DATASET.exists():  # pragma: no cover - the dataset ships with the repo
        pytest.skip(f"{DATASET} is not here")
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def test_no_real_fix_is_ever_called_cheating():
    """The one that costs something. A verdict of cheating is a -1, so this has to be zero."""
    tasks = gold_patches()
    penalised = [
        t["instance_id"]
        for t in tasks
        if cheatcatcher.scan(patch=t["gold_patch"], protected_paths=t.get("test_files") or []).verdict
        == "cheating"
    ]
    assert penalised == [], penalised

    # 0 of 53 is not "never". Say what the sample actually supports.
    lo, hi = wilson(0, len(tasks))
    assert hi < 0.10, f"0/{len(tasks)} bounds the rate at {hi:.2%}, which is weaker than claimed"


def test_findings_on_honest_work_stay_rare_and_advisory():
    """A medium finding is a note, not a verdict. Rare is still the goal."""
    tasks = gold_patches()
    flagged = {
        t["instance_id"]: sorted(
            {
                f.rule
                for f in cheatcatcher.scan(
                    patch=t["gold_patch"], protected_paths=t.get("test_files") or []
                ).findings
            }
        )
        for t in tasks
    }
    noisy = {k: v for k, v in flagged.items() if v}
    lo, hi = wilson(len(noisy), len(tasks))
    assert hi < 0.20, f"{len(noisy)}/{len(tasks)} flagged, 95% CI {lo:.2%} to {hi:.2%}: {noisy}"

    # none of them may be a high-severity rule, which is what turns into a verdict
    for t in tasks:
        report = cheatcatcher.scan(patch=t["gold_patch"], protected_paths=t.get("test_files") or [])
        assert not [f for f in report.findings if f.severity == "high"], (t["instance_id"], report.findings)
