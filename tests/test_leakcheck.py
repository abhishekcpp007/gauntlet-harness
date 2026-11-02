import pytest
from gauntlet_sandbox import Sandbox, SandboxConfig

from gauntlet_harness.leakcheck import distinctive_added_lines, leakcheck
from gauntlet_harness.sandboxing import boot

from .pipeline import pipeline

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def p():
    return pipeline()


def test_clean_task_passes(p):
    report = leakcheck(p.results[1], cache_dir=p.cache)
    assert report.ok, report.failures()
    assert set(report.checks) == {
        "statement",
        "grader_paths",
        "git_history",
        "network",
        "env_vars",
        "hidden_tests_on_disk",
        "gold_on_disk",
        "new_test_names_on_disk",
    }
    assert "none found" in report.checks["gold_on_disk"].detail


def test_leaky_statement_fails(p):
    report = leakcheck(p.leaky, cache_dir=p.cache)
    assert not report.ok
    assert list(report.failures()) == ["statement"]


def test_answer_baked_into_the_image_is_found(p):
    task = p.results[1]
    secret = "\n".join(distinctive_added_lines(task.gold_patch) + distinctive_added_lines(task.test_patch))
    with Sandbox(SandboxConfig(image=task.image), record=False) as sb:
        sb.write_file("/opt/notes/answer.txt", secret)
        image = sb.snapshot("leaky")
    try:
        report = leakcheck(task.model_copy(update={"image": image}), cache_dir=p.cache)
        failures = report.failures()
        assert {"gold_on_disk", "hidden_tests_on_disk"} <= set(failures)
        assert "/opt/notes/answer.txt" in failures["gold_on_disk"]
    finally:
        sb.client.images.remove(image)


def test_agent_sandbox_shape(p):
    task = p.results[1]
    sb = boot(task, apply_tests=False, baseline_git=True, record=False, cache_dir=p.cache)
    try:
        assert sb.exec("git status --porcelain", workdir=task.workdir).stdout == ""
        assert "test_mul" not in sb.exec("ls tests", workdir=task.workdir).stdout
        assert (
            sb.read_text("calc/__init__.py")
            == "def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a // b\n"
        )
    finally:
        sb.close()

    graded = boot(task, apply_tests=True, baseline_git=False, record=False, cache_dir=p.cache)
    try:
        assert graded.exec("test -f tests/test_mul.py", workdir=task.workdir).ok
        assert graded.exec("test -d .git", workdir=task.workdir).exit_code != 0
        assert graded.exec("ls /tmp/gauntlet-patches 2>/dev/null").stdout.strip() == ""
    finally:
        graded.close()


def test_stdlib_hits_are_not_leaks():
    from gauntlet_harness.leakcheck import from_base_image

    assert from_base_image("/usr/local/lib/python3.12/unittest/mock.py")
    assert from_base_image("/usr/lib/python3.11/idlelib/rpc.py")
    assert not from_base_image("/usr/local/lib/python3.12/site-packages/cachetools/__init__.py")
    assert not from_base_image("/workspace/repo/src/cachetools/__init__.py")
    assert not from_base_image("/root/.cache/pip/x.py")


def test_base_lookup_fails_loudly_on_a_missing_commit(tmp_path):
    import subprocess

    from gauntlet_harness.leakcheck import _present_at_base

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("def test_old(\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout
    assert _present_at_base(tmp_path, sha.strip(), ["def test_old(", "def test_new("]) == {"def test_old("}
    assert _present_at_base(tmp_path, sha.strip(), ["def test_new("]) == set()
    with pytest.raises(RuntimeError):
        _present_at_base(tmp_path, "0" * 40, ["def test_old("])


def test_installed_packages_only_count_when_they_are_the_project():
    from gauntlet_harness.leakcheck import counts_as_leak, project_packages
    from gauntlet_harness.schema import HarnessTask, PRInfo

    task = HarnessTask(
        instance_id="pypa__packaging-1",
        repo="r",
        pr=PRInfo(repo="pypa/packaging", number=1, title="t", url="u"),
        base_commit="a",
        solution_commit="b",
        problem_statement="s",
        statement_source="pr",
        image="i",
        env_fingerprint="f",
        gold_patch="--- a/src/packaging/specifiers.py\n+++ b/src/packaging/specifiers.py\n"
        "--- a/tool.py\n+++ b/tool.py\n",
        test_patch="",
        test_files=[],
    )
    pkgs = project_packages(task)
    assert pkgs == {"packaging", "tool"}
    site = "/usr/local/lib/python3.12/site-packages"
    # real packaging bugs seen on packaging PRs 1457 and 1458: idioms in other libraries
    assert not counts_as_leak(f"{site}/_pytest/_code/code.py", pkgs)
    assert not counts_as_leak(f"{site}/hypothesis/internal/reflection.py", pkgs)
    # a copy of the project itself does count, installed or vendored
    assert counts_as_leak(f"{site}/packaging/specifiers.py", pkgs)
    assert counts_as_leak(f"{site}/pip/_vendor/packaging/specifiers.py", pkgs)
    assert counts_as_leak(f"{site}/packaging-26.1.dist-info/RECORD", pkgs)
    # anywhere outside installed packages always counts
    assert counts_as_leak("/workspace/repo/src/packaging/x.py", pkgs)
    assert counts_as_leak("/root/.cache/pip/http/x", pkgs)


def test_imports_are_not_distinctive():
    from gauntlet_harness.leakcheck import distinctive_added_lines

    patch = (
        "+from typing import TYPE_CHECKING, TypeVar\n"
        "+import collections.abc as cabc\n"
        "+    return _canonical_spec(self._spec)\n"
    )
    assert distinctive_added_lines(patch) == ["return _canonical_spec(self._spec)"]


def test_base_lookup_sees_needles_nested_in_other_needles(tmp_path):
    """cattrs PR 777: the new test name is a substring of the hidden def line, and both exist at base."""
    import subprocess

    from gauntlet_harness.leakcheck import _present_at_base

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "t.py").write_text("def test_type_names_with_quotes():\n    pass\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
        check=True,
    )
    needles = [
        "def test_type_names_with_quotes():",
        "def test_type_names_with_quotes(",
        "def test_brand_new(",
    ]
    assert _present_at_base(tmp_path, "HEAD", needles) == set(needles[:2])
