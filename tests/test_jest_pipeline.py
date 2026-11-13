"""The same fake repo and the same three PRs, with jest as the test runner.

jest shares the parser, the canary and the disguise with vitest. What is its own is the command,
the JSON report, and how a snapshot is keyed, so those are what this file holds to account. The
cheats in jscheats.py are written against vitest's internals and are not run here: on jest they
would fail to load and prove nothing.
"""

import atexit
import functools
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import docker
import pytest

from gauntlet_harness.build import build_task
from gauntlet_harness.env import SWEEnv
from gauntlet_harness.grading import grade_patch
from gauntlet_harness.leakcheck import leakcheck
from gauntlet_harness.policies import make_policy
from gauntlet_harness.schema import HarnessTask, Rejected

from .fakejsrepo import F2P, F2P_SNAP, GOLD_LINE, ISSUE_10, N2P, P2P, S2P, make_js_pr_repo

pytestmark = pytest.mark.docker


@functools.cache
def jest_pipeline() -> SimpleNamespace:
    root = Path(tempfile.mkdtemp(prefix="gauntlet-jest-pipeline-"))
    repo, prs = make_js_pr_repo(root, runner="jest")
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
    return jest_pipeline()


def test_a_jest_pr_becomes_a_task(p):
    task = p.results[1]
    assert isinstance(task, HarnessTask), task
    assert (task.language, task.runner) == ("node", "jest")
    assert sorted(task.f2p) == sorted([F2P, F2P_SNAP])
    # jest reports a file that can't load as one failed entry under the file's name, so the
    # test inside it is new once the fix lets the file load
    assert task.n2p == [N2P]
    assert task.s2p == [S2P]
    assert sorted(task.p2p) == sorted(P2P)
    assert task.statement_source == "issue" and task.problem_statement == ISSUE_10
    assert GOLD_LINE in task.gold_patch and "test/" not in task.gold_patch
    assert "__snapshots__/calc.test.js.snap" in task.test_patch
    assert task.validation["runs"]["fix"][0]["PASSED"] == 6


def test_a_regression_and_a_docs_pr_are_rejected_for_the_right_reason(p):
    regression, docs = p.results[2], p.results[3]
    assert isinstance(regression, Rejected) and regression.stage == "transitions"
    assert regression.detail["regressions"] == [P2P[0]]
    assert isinstance(docs, Rejected) and docs.stage == "filter"


@pytest.mark.parametrize("seed", ["1", "2", "3"])
def test_the_gold_patch_resolves_under_canaries_and_new_names(p, seed, monkeypatch):
    """Three seeds, so three sets of names: the snapshot has to follow its test every time."""
    monkeypatch.setenv("GAUNTLET_GRADING_SEED", seed)
    task = p.results[1]
    gold = grade_patch(task, task.gold_patch, cache_dir=p.cache)
    assert gold.resolved and not gold.tampered and gold.error is None
    assert sorted(gold.statuses) == sorted([F2P, F2P_SNAP, N2P, S2P, *P2P])


def test_doing_nothing_resolves_nothing(p):
    task = p.results[1]
    nothing = grade_patch(task, "", cache_dir=p.cache)
    assert not nothing.resolved and not nothing.tampered
    assert not any(nothing.passed["f2p"].values()) and all(nothing.passed["p2p"].values())


def test_a_patch_that_brings_its_own_jest_is_stripped(p):
    task = p.results[1]
    fake = (
        "diff --git a/node_modules/jest/bin/jest.js b/node_modules/jest/bin/jest.js\n"
        "new file mode 100644\n--- /dev/null\n+++ b/node_modules/jest/bin/jest.js\n"
        "@@ -0,0 +1 @@\n+process.exit(0)\n"
    )
    grade = grade_patch(task, fake, cache_dir=p.cache)
    assert grade.ignored_files == ["node_modules/jest/bin/jest.js"]
    assert not grade.resolved and not grade.tampered and all(grade.passed["p2p"].values())


def test_the_agent_sandbox_leaks_nothing(p):
    report = leakcheck(p.results[1], cache_dir=p.cache)
    assert report.ok, report.failures()
    assert report.checks["network"].detail == "BLOCKED"


@pytest.mark.parametrize("policy, reward", [("oracle", 1.0), ("noop", -1.0)])
def test_the_honest_policies_land_where_they_should(p, policy, reward):
    env = SWEEnv(p.results[1], cache_dir=p.cache)
    try:
        make_policy(policy).run(env)
        assert env.result.reward == reward
    finally:
        env.close()
