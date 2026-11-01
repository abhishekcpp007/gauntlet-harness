"""End to end on real containers: PRs from a local repo become tasks, or get rejected for the right reason."""

import pytest

from gauntlet_harness.schema import HarnessTask, Rejected

from .fakerepo import BASE_CALC, GOLD_LINE, ISSUE_10
from .pipeline import pipeline

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def p():
    return pipeline()


def test_good_pr_becomes_a_task(p):
    task = p.results[1]
    assert isinstance(task, HarnessTask), task
    assert task.instance_id == "acme__calc-1"
    assert task.f2p == ["tests/test_calc.py::test_div_gives_exact_quotient"]
    assert task.n2p == ["tests/test_mul.py::test_mul_multiplies_numbers"]
    assert task.s2p == ["tests/test_calc.py::test_version_marker"]
    assert task.p2p == ["tests/test_calc.py::test_add"]
    assert task.flaky == []
    assert task.statement_source == "issue" and task.problem_statement == ISSUE_10
    assert task.test_scope == ["tests/test_calc.py", "tests/test_mul.py"]
    assert task.test_files == ["tests/test_calc.py", "tests/test_mul.py"]
    assert "calc/__init__.py" in task.gold_patch and "tests/" not in task.gold_patch
    assert GOLD_LINE in task.gold_patch
    assert task.image.startswith("gauntlet-harness-env:")
    assert task.validation["env_reused"] is True  # PR 2 built it first
    assert task.validation["fix_runs"] == 2
    assert task.validation["runs"]["fix"][0]["PASSED"] == 4


def test_base_commit_is_the_merge_parent(p):
    import subprocess

    task = p.results[1]
    base_calc = subprocess.run(
        ["git", "-C", str(p.repo), "show", f"{task.base_commit}:calc/__init__.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert base_calc == BASE_CALC
    assert task.solution_commit == p.prs[1].merge_commit_sha


def test_regression_is_rejected(p):
    rej = p.results[2]
    assert isinstance(rej, Rejected)
    assert rej.stage == "transitions"
    assert "breaks 1 passing test" in rej.reason
    assert rej.detail["regressions"] == ["tests/test_calc.py::test_add"]


def test_docs_only_is_rejected(p):
    rej = p.results[3]
    assert isinstance(rej, Rejected) and rej.stage == "filter"


def test_statement_that_quotes_the_fix(p):
    assert isinstance(p.leaky_strict, Rejected)
    assert p.leaky_strict.stage == "statement" and GOLD_LINE in p.leaky_strict.detail["lines"]
    assert isinstance(p.leaky, HarnessTask)
    assert p.leaky.validation["statement_leak"] == [GOLD_LINE]


def test_shared_env_fingerprint(p):
    from gauntlet_harness.build import env_fingerprint
    from gauntlet_harness.sandboxing import cached_clone

    repo = cached_clone(str(p.repo), p.cache)
    task = p.results[1]
    fp_base, _ = env_fingerprint(repo, task.base_commit, "3.12")
    fp_head, _ = env_fingerprint(repo, task.solution_commit, "3.12")
    fp_311, _ = env_fingerprint(repo, task.base_commit, "3.11")
    assert fp_base == fp_head == task.env_fingerprint  # same deps, same image
    assert fp_311 != fp_base
