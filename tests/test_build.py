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


def test_build_cli_survives_a_crashing_pr(tmp_path, monkeypatch):
    import gauntlet_harness.build as build
    from gauntlet_harness.cli import main
    from gauntlet_harness.schema import PRInfo

    def boom(*a, **k):
        raise ValueError("no element found")

    monkeypatch.setattr(build, "build_task", boom)
    prs = tmp_path / "prs.jsonl"
    prs.write_text(PRInfo(repo="o/r", number=3, title="t", url="u").model_dump_json() + "\n")
    rej = tmp_path / "rej.jsonl"
    args = ["build", "--prs", str(prs), "-o", str(tmp_path / "t.jsonl"), "--rejected", str(rej)]
    assert main(args) == 1  # no tasks, but no traceback either
    assert '"stage":"crash"' in rej.read_text() and "o__r-3" in rej.read_text()


def test_full_suite_with_no_report_falls_back_to_changed_tests(p, monkeypatch):
    import gauntlet_harness.build as build

    real = build.run_suite

    def huge_suite(sb, workdir, scope, **kw):
        return {} if not scope else real(sb, workdir, scope, **kw)  # the full suite "times out"

    monkeypatch.setattr(build, "run_suite", huge_suite)
    task = build.build_task(p.prs[1], cache_dir=p.cache, repo_url=str(p.repo), scope="full")
    assert isinstance(task, HarnessTask), task
    assert task.validation["scope"] == "changed" and "no report" in task.validation["scope_fallback"]
    assert task.f2p == ["tests/test_calc.py::test_div_gives_exact_quotient"]


def test_a_task_rebuilds_its_missing_env_image(p):
    """Task files travel (Gauntlet Live, CI runners), images don't. Booting must rebuild, not pull."""
    import docker

    from gauntlet_harness.sandboxing import ensure_image

    task = p.results[1].model_copy(update={"image": "gauntlet-harness-env-test:moved"})
    client = docker.from_env()
    try:
        ensure_image(task, p.cache)  # same deps, so it lands on the validated fingerprint
        assert client.images.get("gauntlet-harness-env-test:moved")
        drifted = task.model_copy(
            update={"image": "gauntlet-harness-env-test:drift", "env_fingerprint": "0" * 64}
        )
        with pytest.raises(RuntimeError, match="drifted"):
            ensure_image(drifted, p.cache)
    finally:
        for tag in ("gauntlet-harness-env-test:moved", "gauntlet-harness-env-test:drift"):
            try:
                client.images.remove(tag)
            except docker.errors.ImageNotFound:
                pass


def test_parallel_workers_share_one_clone(p, tmp_path):
    """Seen on the first nightly run: three workers cloned at once and two died on 'File exists'."""
    from concurrent.futures import ThreadPoolExecutor

    from gauntlet_harness.sandboxing import cached_clone

    with ThreadPoolExecutor(6) as pool:
        repos = list(pool.map(lambda _: cached_clone(str(p.repo), tmp_path), range(6)))
    assert len({r.path for r in repos}) == 1 and (repos[0].path / ".git").exists()
