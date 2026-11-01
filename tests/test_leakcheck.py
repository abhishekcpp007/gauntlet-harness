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
