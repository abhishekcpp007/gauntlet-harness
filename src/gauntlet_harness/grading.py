"""Execution-based grading of one candidate patch.

Order matters and follows SWE-bench: candidate first (minus anything it did to tests or
pytest config), then the hidden test patch on top, then the suite. A test counts only when
its status after all of that is PASSED.
"""

from __future__ import annotations

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

        compile_errors = []
        for path in _changed_python_files(kept):
            q = shlex.quote(path)
            res = sb.exec(f"test ! -f {q} || python -m py_compile {q}", workdir=task.workdir, timeout=60)
            if not res.ok:
                compile_errors.append(f"{path}: {(res.stderr or res.stdout).strip()[-300:]}")

        statuses = run_suite(sb, task.workdir, task.test_scope or None, timeout=timeout)
    finally:
        sb.close()

    def ok(test: str) -> bool:
        return statuses.get(test) == Status.PASSED

    passed = {k: {t: ok(t) for t in getattr(task, k)} for k in ALL_KINDS}
    has_targets = any(passed[k] for k in TARGET_KINDS)
    resolved = has_targets and all(v for k in ALL_KINDS for v in passed[k].values())
    return GradeResult(
        instance_id=task.instance_id,
        patch_applied=True,
        ignored_files=ignored,
        compile_errors=compile_errors,
        statuses={t: s.value if isinstance(s, Status) else str(s) for t, s in statuses.items()},
        passed=passed,
        resolved=resolved,
        error=None if statuses else "the test run produced no results",
    )
