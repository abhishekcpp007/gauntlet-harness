"""Run pytest in a sandbox, and classify how each test moves across the three validation runs.

The rules follow Multi-SWE-bench (Zan et al., 2025):

* three runs: `run` (base), `test` (base + test patch), `fix` (base + test patch + gold patch)
* a test is a target when it goes ANY -> {FAILED, NONE, SKIPPED} -> PASSED (f2p / n2p / s2p)
* p2p is PASSED -> PASSED between `test` and `fix`
* the PR is dropped when any test regresses: PASSED in `test` (or in `run`) and FAILED in `fix`
  (that covers the paper's ANY -> PASSED -> FAILED rule and its abnormal
  PASSED -> NONE/SKIPPED -> FAILED case)
* the PR is dropped when nothing flips to PASSED

On top of that, `fix` can run more than once. Tests whose status changes between those
repeats are flaky and get dropped from every list, so they never decide a grade.
"""

from __future__ import annotations

import shlex

from gauntlet_repo2env import PatchFailed, parse_junit
from gauntlet_sandbox import Sandbox
from pydantic import BaseModel, Field

from .schema import Status

JUNIT_PATH = "/tmp/gauntlet-junit.xml"
PATCH_DIR = "/tmp/gauntlet-patches"

_STATUS = {
    "passed": Status.PASSED,
    "failed": Status.FAILED,
    "error": Status.FAILED,
    "skipped": Status.SKIPPED,
}

__all__ = ["JUNIT_PATH", "PatchFailed", "Transitions", "apply_patch", "classify", "run_suite", "summarize"]


def _test_file(target: str) -> str:
    return target.split("::", 1)[0]


def run_suite(
    sb: Sandbox, workdir: str, scope: list[str] | None, *, timeout: float = 900
) -> dict[str, Status]:
    """Run pytest (whole suite, or just `scope`) and return each test's status.

    Scope entries whose file doesn't exist yet are dropped, so a test module that only the
    test patch adds simply reports nothing in the base run instead of failing the command.
    An empty dict means pytest produced no report at all.
    """
    targets: list[str] = []
    if scope:
        listing = " ".join(shlex.quote(_test_file(t)) for t in scope)
        res = sb.exec(f'for f in {listing}; do [ -e "$f" ] && echo "$f"; done', workdir=workdir)
        present = set(res.stdout.split())
        targets = [t for t in scope if _test_file(t) in present]
        if not targets:
            return {}

    cmd = (
        f"rm -f {JUNIT_PATH}; python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 "
        f"--continue-on-collection-errors --junitxml={JUNIT_PATH} "
        + " ".join(shlex.quote(t) for t in targets)
    )
    sb.exec(cmd, workdir=workdir, timeout=timeout)
    try:
        xml_text = sb.read_text(JUNIT_PATH)
    except FileNotFoundError:
        return {}
    finally:
        sb.exec(f"rm -f {JUNIT_PATH}")
    return {test_id: _STATUS[status] for test_id, status in parse_junit(xml_text).items()}


def apply_patch(sb: Sandbox, workdir: str, patch: str, name: str) -> None:
    """git apply a patch inside the sandbox. Raises PatchFailed if it doesn't apply cleanly."""
    if not patch.strip():
        return
    path = f"{PATCH_DIR}/{name}.diff"
    sb.write_file(path, patch if patch.endswith("\n") else patch + "\n")
    try:
        res = sb.exec(f"git apply --whitespace=nowarn {shlex.quote(path)}", workdir=workdir)
    finally:
        sb.exec(f"rm -f {shlex.quote(path)}")
    if not res.ok:
        raise PatchFailed(f"{name} patch didn't apply: {res.stderr.strip()[:2000]}")


def summarize(statuses: dict[str, Status]) -> dict[str, int]:
    out = {s.value: 0 for s in Status if s is not Status.NONE}
    for status in statuses.values():
        out[status.value] += 1
    out["total"] = len(statuses)
    return out


class Transitions(BaseModel):
    f2p: list[str] = Field(default_factory=list)
    n2p: list[str] = Field(default_factory=list)
    s2p: list[str] = Field(default_factory=list)
    p2p: list[str] = Field(default_factory=list)
    flaky: list[str] = Field(default_factory=list)
    rejects: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejects

    @property
    def targets(self) -> list[str]:
        return [*self.f2p, *self.n2p, *self.s2p]


def classify(
    run: dict[str, Status], test: dict[str, Status], fix_runs: list[dict[str, Status]]
) -> Transitions:
    if not fix_runs:
        raise ValueError("classify needs at least one fix run")
    fix = fix_runs[0]
    every = set(run) | set(test) | {t for f in fix_runs for t in f}

    def get(results: dict[str, Status], test_id: str) -> Status:
        return results.get(test_id, Status.NONE)

    flaky = sorted(t for t in every if len({get(f, t) for f in fix_runs}) > 1)
    out = Transitions(flaky=flaky)
    stable = sorted(every - set(flaky))

    for t in stable:
        before, after = get(test, t), get(fix, t)
        if after is Status.FAILED and Status.PASSED in (before, get(run, t)):
            out.regressions.append(t)
            continue
        if after is not Status.PASSED:
            continue
        if before is Status.PASSED:
            out.p2p.append(t)
        elif before is Status.FAILED:
            out.f2p.append(t)
        elif before is Status.NONE:
            out.n2p.append(t)
        elif before is Status.SKIPPED:
            out.s2p.append(t)

    if out.regressions:
        shown = ", ".join(out.regressions[:5])
        out.rejects.append(f"the fix breaks {len(out.regressions)} passing test(s): {shown}")
    if not fix:
        out.rejects.append("the fix run produced no test results")
    elif not out.targets:
        out.rejects.append("no test goes from failing, skipped or missing to passing")
    return out
