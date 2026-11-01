"""Boot a fresh sandbox for a task at its base commit. Used for building, grading and agents."""

from __future__ import annotations

import shlex
from pathlib import Path

from gauntlet_repo2env.repo import Repo, archive, clone, default_cache, slug_for
from gauntlet_sandbox import Sandbox, SandboxConfig

from .schema import HarnessTask
from .testing import apply_patch

SCRATCH_GIT = "git -c user.name=workspace -c user.email=workspace@localhost"

# The env image may have been built at a different commit with the same dependencies, and
# the workdir gets replaced, so anything the build generated (setuptools-scm / hatch-vcs
# _version.py files, editable install metadata) has to be regenerated for THIS commit.
# Offline and dependency-free: the env already has every dependency and the build backend.
REINSTALL = (
    "if [ -f pyproject.toml ] || [ -f setup.py ]; then "
    'SETUPTOOLS_SCM_PRETEND_VERSION="${SETUPTOOLS_SCM_PRETEND_VERSION:-0.0.0}" '
    "python -m pip install -q --disable-pip-version-check --no-cache-dir --no-index --no-deps "
    "--no-build-isolation -e . >/tmp/.reinstall.log 2>&1 || echo REINSTALL_FAILED; "
    "rm -f /tmp/.reinstall.log; fi"
)


def cached_clone(url: str, cache_dir: Path | None = None) -> Repo:
    """The cached clone, without a network fetch if it already exists."""
    dest = (cache_dir or default_cache()) / "repos" / slug_for(url)
    if (dest / ".git").exists():
        return Repo(dest, url, slug_for(url))
    return clone(url, cache_dir)


def boot(
    task: HarnessTask,
    *,
    apply_tests: bool,
    baseline_git: bool,
    timeout: float = 120,
    trajectory_path: str | Path | None = None,
    record: bool = True,
    cache_dir: Path | None = None,
) -> Sandbox:
    """A started sandbox with the repo at the task's base commit. The caller closes it.

    No network. The workdir is emptied first so nothing from the env image's build commit
    survives. `apply_tests` adds the hidden tests (grading and validation only, never for an
    agent). `baseline_git` commits the tree to a throwaway one-commit repo so whatever the
    agent changes can be diffed later.
    """
    repo = cached_clone(task.repo, cache_dir)
    cfg = SandboxConfig(
        image=task.image,
        workdir=task.workdir,
        default_timeout=timeout,
        max_output_bytes=200_000,
    )
    sb = Sandbox(
        cfg,
        trajectory_path=trajectory_path,
        record=record,
        metadata={"instance_id": task.instance_id},
    ).start()
    wd = shlex.quote(task.workdir)
    try:
        sb.exec(f"find {wd} -mindepth 1 -delete")
        sb.put_archive(task.workdir, archive(repo, task.base_commit))
        res = sb.exec(REINSTALL, workdir=task.workdir, timeout=max(timeout, 300))
        sb.metadata["reinstall_ok"] = "REINSTALL_FAILED" not in res.stdout
        if apply_tests:
            apply_patch(sb, task.workdir, task.test_patch, "test")
        if baseline_git:
            res = sb.exec(
                f"git init -q && git add -A && {SCRATCH_GIT} commit -q -m base", workdir=task.workdir
            )
            if not res.ok:
                raise RuntimeError(f"couldn't create the scratch git repo: {res.stderr.strip()}")
    except Exception:
        sb.close()
        raise
    return sb
