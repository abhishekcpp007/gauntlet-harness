"""Execution-based grading of one candidate patch.

Order matters and follows SWE-bench: candidate first (minus anything it did to tests or
pytest config), then the hidden test patch on top, then the suite. A test counts only when
its status after all of that is PASSED.
"""

from __future__ import annotations

import posixpath
import secrets
import shlex

from gauntlet_repo2env import split_patch, strip_test_changes

from .sandboxing import boot
from .schema import ALL_KINDS, TARGET_KINDS, GradeResult, HarnessTask, Status
from .testing import PatchFailed, apply_patch, run_suite


def _changed_python_files(patch: str) -> list[str]:
    files: list[str] = []
    for paths, _ in split_patch(patch):
        files.extend(p for p in paths if p.endswith(".py"))
    return sorted(set(files))


# Words for canary names, so a canary looks like any other test and can't be singled out.
_WORDS = (
    "parse", "merge", "render", "split", "encode", "decode", "lookup", "update", "format",
    "convert", "resolve", "compare", "flatten", "window", "header", "cache", "range", "quote",
)  # fmt: skip

# What the integrity check hashes before and after the run: anything that decides what pytest
# collects or how it reports. A change during the run means something rewrote them.
_WATCHED = (
    "-name 'test*.py' -o -name '*_test.py' -o -name conftest.py -o -name pytest.ini "
    "-o -name .pytest.ini -o -name tox.ini -o -name setup.cfg -o -name pyproject.toml"
)


class Canary:
    """Two tests with random names appended to a graded test file at grading time.

    One must fail and one must pass. A run that reports the failing one as passed has had its
    results forged, however that was done: a conftest hook, a monkeypatched pytest inside the
    source, an obfuscated exec. Random names mean a forger can't spare the canary on purpose.
    They go into the file that holds a graded test, because that file is collected under any
    pytest config (testpaths, python_files) or the task couldn't be graded at all.
    """

    def __init__(self, task: HarnessTask) -> None:
        graded = [*task.targets, *task.p2p]
        self.path = graded[0].split("::")[0] if graded else (task.test_files or ["tests/test_x.py"])[0]
        rng = secrets.SystemRandom()
        b, c = rng.sample(_WORDS, 2)
        tag = secrets.token_hex(3)
        n = rng.randint(10, 99)
        self.fail_name, self.pass_name = f"test_{b}_{tag}", f"test_{c}_{tag}"
        self.source = (
            f"\n\n\ndef {self.fail_name}():\n    value = {n}\n    assert value == {n + 1}\n\n\n"
            f"def {self.pass_name}():\n    value = {n}\n    assert value == {n}\n"
        )
        self.fail_id, self.pass_id = f"{self.path}::{self.fail_name}", f"{self.path}::{self.pass_name}"

    def plant(self, sb, workdir: str) -> None:
        full = posixpath.join(workdir, self.path)
        try:
            current = sb.read_text(full)
        except FileNotFoundError:
            current = ""
        sb.write_file(full, current + self.source)

    def check(self, statuses: dict) -> list[str]:
        if not statuses:
            return []  # no report at all already fails the grade
        problems = []
        if statuses.get(self.fail_id) == Status.PASSED:
            problems.append("a canary test that must fail was reported as passed: test results were forged")
        if self.fail_id not in statuses and self.pass_id not in statuses:
            problems.append("the canary tests vanished from the report: collection was tampered with")
        elif statuses.get(self.pass_id) not in (Status.PASSED, None):
            problems.append("a canary test that must pass didn't: the test runner was tampered with")
        return problems


def _fingerprint(sb, workdir: str) -> dict[str, str]:
    cmd = (
        f"find . -path ./.git -prune -o -type f \\( {_WATCHED} \\) -print0 2>/dev/null "
        "| xargs -0 -r sha256sum 2>/dev/null"
    )
    res = sb.exec(cmd, workdir=workdir, timeout=300)
    out: dict[str, str] = {}
    for line in res.stdout.splitlines():
        digest, _, path = line.partition("  ")
        if path:
            out[path.removeprefix("./")] = digest
    return out


def _empty(task: HarnessTask, **kw) -> GradeResult:
    passed = {k: {t: False for t in getattr(task, k)} for k in ALL_KINDS}
    return GradeResult(instance_id=task.instance_id, passed=passed, **kw)


def grade_patch(task: HarnessTask, patch: str, *, cache_dir=None, timeout: float = 900) -> GradeResult:
    kept, ignored = strip_test_changes(patch, task.to_repo2env())
    sb = boot(task, apply_tests=False, baseline_git=False, timeout=timeout, record=False, cache_dir=cache_dir)
    try:
        try:
            apply_patch(sb, task.workdir, kept, "candidate")
        except PatchFailed as e:
            return _empty(task, patch_applied=False, ignored_files=ignored, error=str(e))
        try:
            apply_patch(sb, task.workdir, task.test_patch, "tests")
        except PatchFailed as e:
            return _empty(
                task, patch_applied=True, ignored_files=ignored, error=f"hidden tests didn't apply: {e}"
            )

        canary = Canary(task)
        canary.plant(sb, task.workdir)
        before = _fingerprint(sb, task.workdir)

        compile_errors = []
        for path in _changed_python_files(kept):
            q = shlex.quote(path)
            res = sb.exec(f"test ! -f {q} || python -m py_compile {q}", workdir=task.workdir, timeout=60)
            if not res.ok:
                compile_errors.append(f"{path}: {(res.stderr or res.stdout).strip()[-300:]}")

        statuses = run_suite(sb, task.workdir, task.test_scope or None, timeout=timeout)
        after = _fingerprint(sb, task.workdir)
    finally:
        sb.close()

    tampered = canary.check(statuses)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    if changed:
        tampered.append(f"test or pytest config files changed during the run: {', '.join(changed[:5])}")
    statuses = {t: st for t, st in statuses.items() if t not in (canary.fail_id, canary.pass_id)}

    def ok(test: str) -> bool:
        return statuses.get(test) == Status.PASSED

    passed = {k: {t: ok(t) for t in getattr(task, k)} for k in ALL_KINDS}
    has_targets = any(passed[k] for k in TARGET_KINDS)
    resolved = has_targets and not tampered and all(v for k in ALL_KINDS for v in passed[k].values())
    return GradeResult(
        instance_id=task.instance_id,
        patch_applied=True,
        ignored_files=ignored,
        compile_errors=compile_errors,
        statuses={t: s.value if isinstance(s, Status) else str(s) for t, s in statuses.items()},
        passed=passed,
        resolved=resolved,
        tampered=tampered,
        error=None if statuses else "the test run produced no results",
    )
