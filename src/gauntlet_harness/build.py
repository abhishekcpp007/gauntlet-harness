"""Turn one merged PR into a validated, leak-free HarnessTask, or say exactly why not.

Stages, each of which can reject:

1. filter      the PR must change Python source and at least one test module
2. statement   linked issue text first (SWE-bench style), else a cleaned PR description;
               it must not quote the fix
3. env         dependencies installed at the BASE commit, cached by a fingerprint of the
               dependency files so PRs that share deps share an image
4. runs        run (base), test (base + test patch), fix (base + test + gold, repeated)
5. transitions Multi-SWE-bench rules: keep ANY -> {FAILED, NONE, SKIPPED} -> PASSED,
               drop PRs with regressions, drop flaky tests
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound
from gauntlet_repo2env import BuildFailed, PatchFailed, Repo2EnvError, UnsupportedRepo, build_env
from gauntlet_repo2env.detect import detect, install_script
from gauntlet_repo2env.repo import (
    GitError,
    Repo,
    changed_files,
    clone,
    diff,
    git,
    is_test_module,
    is_test_path,
)
from gauntlet_sandbox import SandboxError

from .leakcheck import statement_leaks
from .sandboxing import boot
from .schema import HarnessTask, PRInfo, Rejected
from .testing import apply_patch, classify, run_suite, summarize

ENV_REPO = "gauntlet-harness-env"
DEP_FILES = re.compile(r"^(pyproject\.toml|setup\.py|setup\.cfg|(.*/)?[^/]*requirements[^/]*\.txt)$")

Log = Callable[[str], None]


def instance_id(pr: PRInfo) -> str:
    owner, _, name = pr.repo.partition("/")
    return f"{owner}__{name}-{pr.number}"


# ---------------------------------------------------------------------- statement


_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_DIFF_FENCE = re.compile(r"```\s*(?:diff|patch)\b.*?```", re.DOTALL | re.IGNORECASE)
_BLANKS = re.compile(r"\n{3,}")


def sanitize(text: str, repo: str) -> str:
    """Strip what tends to give the answer away: HTML comments (PR templates), diff fences,
    and links to this repo's pulls and commits."""
    text = _COMMENT.sub("", text or "")
    text = _DIFF_FENCE.sub("", text)
    pr_links = re.compile(
        rf"https?://github\.com/{re.escape(repo)}/(?:pull|commit|compare)/\S+", re.IGNORECASE
    )
    text = pr_links.sub("", text)
    return _BLANKS.sub("\n\n", text).strip()


def problem_statement(pr: PRInfo) -> tuple[str, str]:
    """(statement, source). Issue text when the PR closes issues, else the PR itself."""
    issues = [pr.issue_texts[n] for n in pr.linked_issues if pr.issue_texts.get(n)]
    if issues:
        return sanitize("\n\n".join(issues), pr.repo), "issue"
    return sanitize(f"{pr.title}\n\n{pr.body}", pr.repo), "pr"


# ---------------------------------------------------------------------- git


def _has_commit(repo: Repo, sha: str) -> bool:
    try:
        git(repo.path, "cat-file", "-e", f"{sha}^{{commit}}")
        return True
    except GitError:
        return False


def resolve_commits(repo: Repo, pr: PRInfo) -> tuple[str, str]:
    """(base, solution). The solution is the merge (or squash) commit and the base is its
    first parent, which is exactly what the PR changed as merged, unlike the API's base sha
    that can lag behind the branch it merged into."""
    sha = pr.merge_commit_sha
    if sha and not _has_commit(repo, sha):
        subprocess.run(["git", "-C", str(repo.path), "fetch", "-q", "origin", sha], capture_output=True)
    if sha and _has_commit(repo, sha):
        base = git(repo.path, "rev-parse", f"{sha}^1").strip()
        return base, sha
    # last resort: the PR's own head, diffed against where it branched off
    ref = f"refs/gauntlet/pr-{pr.number}"
    subprocess.run(
        ["git", "-C", str(repo.path), "fetch", "-q", "origin", f"pull/{pr.number}/head:{ref}"],
        capture_output=True,
    )
    if not _has_commit(repo, ref) or not pr.base_sha:
        raise GitError(f"can't find the commits for PR #{pr.number}")
    if not _has_commit(repo, pr.base_sha):
        subprocess.run(
            ["git", "-C", str(repo.path), "fetch", "-q", "origin", pr.base_sha], capture_output=True
        )
    head = git(repo.path, "rev-parse", ref).strip()
    base = git(repo.path, "merge-base", pr.base_sha, head).strip()
    return base, head


# ---------------------------------------------------------------------- env


def env_fingerprint(repo: Repo, sha: str, python: str) -> tuple[str, str]:
    """(fingerprint, install script). Same deps at base -> same image, whatever the commit."""
    stack = detect(repo, sha)
    if stack.language != "python":
        raise UnsupportedRepo(f"{repo.slug} looks like a {stack.language} project")
    script = install_script(stack)
    h = hashlib.sha256()
    h.update(f"python={python}\n{script}\n".encode())
    for path in sorted(git(repo.path, "ls-tree", "-r", "--name-only", sha).splitlines()):
        if DEP_FILES.match(path):
            h.update(f"\n--- {path}\n".encode())
            h.update(
                subprocess.run(
                    ["git", "-C", str(repo.path), "show", f"{sha}:{path}"], capture_output=True
                ).stdout
            )
    return h.hexdigest(), script


def ensure_env(
    url: str, repo: Repo, base: str, *, python: str, cache_dir: Path | None, log: Log
) -> tuple[str, str, bool]:
    """(image tag, fingerprint, reused). Builds at the base commit when no image matches."""
    fingerprint, _ = env_fingerprint(repo, base, python)
    tag = f"{ENV_REPO}:{fingerprint[:16]}"
    client = docker.from_env()
    try:
        client.images.get(tag)
        log(f"reusing env {tag}")
        return tag, fingerprint, True
    except ImageNotFound:
        pass
    log(f"building env {tag} at {base[:12]}")
    spec = build_env(url, ref=base, python=python, baseline_tests=False, cache_dir=cache_dir, log=log)
    image = client.images.get(spec.image)
    image.tag(ENV_REPO, fingerprint[:16])
    client.images.remove(spec.image)  # only drops repo2env's tag, the image keeps ours
    return tag, fingerprint, False


# ---------------------------------------------------------------------- the build


def build_task(
    pr: PRInfo,
    *,
    cache_dir: Path | None = None,
    python: str = "3.12",
    scope: str = "changed",
    fix_runs: int = 2,
    strict_statement: bool = True,
    repo_url: str | None = None,
    test_timeout: float = 900,
    log: Log = lambda _: None,
) -> HarnessTask | Rejected:
    """Validate one PR end to end. Expected failures come back as Rejected, never raised."""
    iid = instance_id(pr)
    url = repo_url or f"https://github.com/{pr.repo}.git"
    timings: dict[str, float] = {}
    started = time.monotonic()

    def reject(stage: str, reason: str, **detail) -> Rejected:
        log(f"{iid} rejected at {stage}: {reason}")
        return Rejected(instance_id=iid, stage=stage, reason=reason, detail=detail)

    # 1. filter
    try:
        repo = clone(url, cache_dir)
        base, solution = resolve_commits(repo, pr)
    except Repo2EnvError as e:
        return reject("collect", str(e))
    files = changed_files(repo, base, solution)
    tests = [f for f in files if is_test_path(f)]
    source = [f for f in files if not is_test_path(f)]
    test_modules = [f for f in tests if is_test_module(f)]
    if not any(f.endswith(".py") for f in source):
        return reject("filter", "no Python source changes", files=files)
    if not test_modules:
        return reject("filter", "no test module changes", files=files)
    test_patch = diff(repo, base, solution, tests)
    gold_patch = diff(repo, base, solution, source)

    # 2. statement
    statement, source_kind = problem_statement(pr)
    if not statement.strip():
        return reject("statement", "empty problem statement")
    leaked = statement_leaks(statement, gold_patch)
    if leaked and strict_statement:
        return reject("statement", "the statement quotes the fix", lines=leaked[:5])

    # 3. env at the base commit
    t = time.monotonic()
    try:
        image, fingerprint, reused = ensure_env(url, repo, base, python=python, cache_dir=cache_dir, log=log)
    except (UnsupportedRepo, BuildFailed, Repo2EnvError, SandboxError, APIError) as e:
        return reject("env", str(e)[:2000])
    timings["env"] = round(time.monotonic() - t, 2)

    task = HarnessTask(
        instance_id=iid,
        repo=url,
        pr=pr,
        base_commit=base,
        solution_commit=solution,
        problem_statement=statement,
        statement_source=source_kind,
        image=image,
        env_fingerprint=fingerprint,
        gold_patch=gold_patch,
        test_patch=test_patch,
        test_files=tests,
        test_scope=test_modules if scope == "changed" else [],
    )
    targets = task.test_scope or None

    # 4. the three runs
    def run_once(apply_tests: bool, apply_gold: bool) -> dict:
        sb = boot(task, apply_tests=apply_tests, baseline_git=False, record=False, cache_dir=cache_dir)
        try:
            if apply_gold:
                apply_patch(sb, task.workdir, gold_patch, "gold")
            return run_suite(sb, task.workdir, targets, timeout=test_timeout)
        finally:
            sb.close()

    try:
        t = time.monotonic()
        run = run_once(False, False)
        timings["run"] = round(time.monotonic() - t, 2)
        t = time.monotonic()
        test = run_once(True, False)
        timings["test"] = round(time.monotonic() - t, 2)
        t = time.monotonic()
        fixes = [run_once(True, True) for _ in range(max(1, fix_runs))]
        timings["fix"] = round(time.monotonic() - t, 2)
    except (PatchFailed, SandboxError) as e:
        return reject("runs", str(e)[:2000])

    # A suite too big for the time budget (packaging has 60k+ tests) gets killed before pytest
    # writes its report, so every run comes back empty. Fall back to the PR's own test modules.
    if scope == "full" and not run and not test and not any(fixes):
        log(f"{iid}: the full suite gave no report in {test_timeout:.0f}s, retrying on the changed tests")
        retry = build_task(
            pr,
            cache_dir=cache_dir,
            python=python,
            scope="changed",
            fix_runs=fix_runs,
            strict_statement=strict_statement,
            repo_url=repo_url,
            test_timeout=test_timeout,
            log=log,
        )
        if isinstance(retry, HarnessTask):
            retry.validation["scope_fallback"] = f"full suite gave no report within {test_timeout:.0f}s"
        return retry

    # 5. transitions
    trans = classify(run, test, fixes)
    runs_summary = {"run": summarize(run), "test": summarize(test), "fix": [summarize(f) for f in fixes]}
    if not trans.ok:
        return reject(
            "transitions", "; ".join(trans.rejects), runs=runs_summary, regressions=trans.regressions
        )

    timings["total"] = round(time.monotonic() - started, 2)
    task.f2p, task.n2p, task.s2p, task.p2p, task.flaky = (
        trans.f2p,
        trans.n2p,
        trans.s2p,
        trans.p2p,
        trans.flaky,
    )
    task.validation = {
        "runs": runs_summary,
        "seconds": timings,
        "env_reused": reused,
        "fix_runs": len(fixes),
        "statement_leak": leaked,
        "scope": scope,
        "rules": "multi-swe-bench",
        "python": python,  # needed to rebuild the env image on another machine
    }
    log(f"{iid} ok: f2p={len(task.f2p)} n2p={len(task.n2p)} s2p={len(task.s2p)} p2p={len(task.p2p)}")
    return task
