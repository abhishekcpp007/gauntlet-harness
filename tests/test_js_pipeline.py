"""End to end on real containers, in JavaScript: the same three PRs as the Python fake repo,
built into a task with vitest, graded, leak-checked and attacked.

The last two tests are a pair and neither means much alone. One shows every cheat in the zoo
ends at or below zero. The other switches the defences off and shows the same cheats forging
results, which is what makes the first one worth anything.
"""

import atexit
import functools
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import docker
import pytest

import gauntlet_harness.grading as grading
from gauntlet_harness.build import build_task
from gauntlet_harness.env import SWEEnv
from gauntlet_harness.grading import grade_patch
from gauntlet_harness.leakcheck import leakcheck
from gauntlet_harness.policies import make_policy
from gauntlet_harness.schema import HarnessTask, Rejected

from .fakejsrepo import F2P, GOLD_LINE, ISSUE_10, N2P, P2P, S2P, make_js_pr_repo

pytestmark = pytest.mark.docker

# every trick that edits source only, so nothing the grader strips can save the grade
SOURCE_CHEATS = ["monkeypatch", "stealth", "rewrite", "canary-aware", "ghost", "junit", "exit", "snapshot"]


@functools.cache
def js_pipeline() -> SimpleNamespace:
    root = Path(tempfile.mkdtemp(prefix="gauntlet-js-pipeline-"))
    repo, prs = make_js_pr_repo(root)
    cache = root / "cache"
    by_number = {pr.number: pr for pr in prs}

    def build(pr, **kw):
        return build_task(pr, cache_dir=cache, repo_url=str(repo), **kw)

    results = {3: build(by_number[3]), 2: build(by_number[2], scope="full"), 1: build(by_number[1])}
    images = {r.image for r in results.values() if isinstance(r, HarnessTask)}

    def cleanup() -> None:
        client = docker.from_env()
        for image in images:
            try:
                client.images.remove(image, force=True)
            except docker.errors.APIError:
                pass
        shutil.rmtree(root, ignore_errors=True)

    atexit.register(cleanup)
    return SimpleNamespace(repo=repo, prs=by_number, cache=cache, results=results)


@pytest.fixture(scope="module")
def p():
    return js_pipeline()


def _episode(p, policy: str):
    env = SWEEnv(p.results[1], cache_dir=p.cache)
    try:
        make_policy(policy).run(env)
        return env.result
    finally:
        env.close()


def test_a_js_pr_becomes_a_task(p):
    task = p.results[1]
    assert isinstance(task, HarnessTask), task
    assert (task.language, task.runner) == ("node", "vitest")
    # a file that can't find its import still loads under vite, so its test fails rather
    # than going missing: that's f2p here where pytest calls it n2p
    assert sorted(task.f2p) == sorted([F2P, N2P])
    assert task.s2p == [S2P]
    assert sorted(task.p2p) == sorted(P2P)
    assert task.statement_source == "issue" and task.problem_statement == ISSUE_10
    assert task.test_scope == ["test/calc.test.js", "test/mul.test.js"]
    assert GOLD_LINE in task.gold_patch and "test/" not in task.gold_patch
    assert task.validation["node"] == "22" and task.validation["package_name"] == "jscalc"
    assert task.validation["runs"]["fix"][0]["PASSED"] == 5


def test_a_regression_and_a_docs_pr_are_rejected_for_the_right_reason(p):
    regression, docs = p.results[2], p.results[3]
    assert isinstance(regression, Rejected) and regression.stage == "transitions"
    assert regression.detail["regressions"] == [P2P[0]]
    assert isinstance(docs, Rejected) and docs.stage == "filter"
    assert docs.reason == "no JavaScript or TypeScript source changes"


def test_the_gold_patch_resolves_and_doing_nothing_does_not(p):
    task = p.results[1]
    gold = grade_patch(task, task.gold_patch, cache_dir=p.cache)
    assert gold.resolved and not gold.tampered and gold.error is None
    # the canary and the renamed targets are gone from what the grade reports
    assert sorted(gold.statuses) == sorted([F2P, N2P, S2P, *P2P])
    nothing = grade_patch(task, "", cache_dir=p.cache)
    assert not nothing.resolved and not nothing.tampered
    assert not any(nothing.passed["f2p"].values()) and all(nothing.passed["p2p"].values())


def test_a_patch_that_brings_its_own_node_modules_is_stripped(p):
    task = p.results[1]
    fake = (
        "diff --git a/node_modules/vitest/vitest.mjs b/node_modules/vitest/vitest.mjs\n"
        "new file mode 100644\n--- /dev/null\n+++ b/node_modules/vitest/vitest.mjs\n"
        "@@ -0,0 +1 @@\n+process.exit(0)\n"
    )
    grade = grade_patch(task, fake, cache_dir=p.cache)
    assert grade.ignored_files == ["node_modules/vitest/vitest.mjs"]
    assert not grade.resolved and not grade.tampered and all(grade.passed["p2p"].values())


def test_the_agent_sandbox_leaks_nothing(p):
    report = leakcheck(p.results[1], cache_dir=p.cache)
    assert report.ok, report.failures()
    assert report.checks["network"].detail == "BLOCKED"
    assert "hidden test line(s) checked" in report.checks["hidden_tests_on_disk"].detail


def test_scripted_policies_land_where_they_should(p):
    assert _episode(p, "oracle").reward == 1.0
    assert _episode(p, "noop").reward == -1.0
    plain = _episode(p, "cheater")
    assert plain.reward == -1.0 and not plain.grade.resolved
    # everything the plain cheater wrote was the grader's to throw away
    assert {"vitest.config.js", "vitest.setup.js", "test/calc.test.js"} <= set(plain.grade.ignored_files)


@pytest.mark.parametrize("trick", SOURCE_CHEATS)
def test_no_cheat_earns_anything(p, trick):
    result = _episode(p, f"cheater:{trick}")
    assert result.reward <= 0, (trick, result.reward, result.grade.tampered)
    assert not result.grade.resolved


@pytest.mark.parametrize("trick", ["monkeypatch", "stealth", "canary-aware", "ghost", "junit"])
def test_the_same_cheats_win_against_a_grader_with_no_defences(p, trick, monkeypatch):
    """Without this, "no cheat earns anything" could just mean the cheats are broken."""
    monkeypatch.setattr(grading.Canary, "plant", lambda self, sb, workdir: None)
    monkeypatch.setattr(grading.Canary, "check", lambda self, statuses: [])
    monkeypatch.setattr(grading.Disguise, "apply", lambda self, sb, workdir: None)
    monkeypatch.setattr(grading, "_fingerprint", lambda sb, workdir, tries=3, runner=None: {})
    result = _episode(p, f"cheater:{trick}")
    forged = result.grade.passed["f2p"]
    assert all(forged.values()) and len(forged) == 2, (trick, forged)
    assert not result.grade.tampered and result.reward > 0.5


@pytest.mark.parametrize("trick", ["canary-aware", "ghost"])
def test_the_disguise_is_what_stops_a_cheat_that_knows_the_canary(p, trick, monkeypatch):
    # canary on, hash on, renaming off: the cheat spares the canary by its name and forges the rest
    monkeypatch.setattr(grading.Disguise, "apply", lambda self, sb, workdir: None)
    result = _episode(p, f"cheater:{trick}")
    assert all(result.grade.passed["f2p"].values()) and not result.grade.tampered
